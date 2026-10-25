"""Periodic room checks and owned recording processes, independent of Tk widgets."""

import os
import re
import shutil
import subprocess
import threading
import time
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from message_collector import room_session_id


QUALITY_ORDER = ['origin', 'FULL_HD1', 'HD1', 'SD2', 'SD1', 'audio']
QUALITY_NAMES = {'origin': '原画', 'FULL_HD1': '蓝光', 'HD1': '超清',
                 'SD2': '高清', 'SD1': '标清', 'audio': '仅音频'}
DEFAULT_SETTINGS = {'interval': 60, 'directory': 'recordings',
                    'ffmpeg': '', 'potplayer': '', 'anchors': {}}
DEFAULT_ANCHOR = {'enabled': False, 'auto_play': False,
                  'auto_record': False, 'auto_collect': False, 'quality': 'best'}
POTPLAYER_PATHS = [r'C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe',
                   r'C:\Program Files (x86)\DAUM\PotPlayer\PotPlayerMini.exe',
                   r'C:\Program Files\PotPlayer\PotPlayerMini64.exe']


def executable_path(configured, name, candidates=()):
    if configured:
        found = shutil.which(configured)
        if found:
            return found
        raise FileNotFoundError(f'找不到 {name}，请检查设置中的程序路径')
    candidates = list(candidates)
    if name == 'ffmpeg':
        candidates.append(Path(__file__).resolve().parent / '.tools' / 'ffmpeg' / 'ffmpeg.exe')
    if name == 'ffmpeg' and os.environ.get('LOCALAPPDATA'):
        candidates.append(os.path.join(os.environ['LOCALAPPDATA'], 'Microsoft', 'WinGet', 'Links', 'ffmpeg.exe'))
    for candidate in [shutil.which(name), *candidates]:
        if candidate and os.path.isfile(candidate):
            return str(candidate)
    raise FileNotFoundError(f'找不到 {name}，请安装并在监控设置中指定程序路径')


def launch_player(url, configured='', new=False):
    path = executable_path(configured, 'PotPlayerMini64.exe', POTPLAYER_PATHS)
    return subprocess.Popen([path, url] + (['/new'] if new else []))


def select_stream(streams, quality='best'):
    # A requested quality falls back to the highest available stream.
    if quality != 'best' and streams.get(quality):
        return streams[quality]
    return next((streams[q] for q in QUALITY_ORDER if streams.get(q)), None)


def safe_component(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:80]
    if not value or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I):
        value = '_' + value
    return value


def recording_command(ffmpeg, url, output):
    return [ffmpeg, '-hide_banner', '-loglevel', 'error', '-n',
            '-progress', 'pipe:1', '-stats_period', '2',
            '-rw_timeout', '15000000', '-referer', 'https://live.douyin.com/',
            '-i', url, '-map', '0:v?', '-map', '0:a?', '-c', 'copy',
            '-f', 'matroska', str(output)]


class Recording:
    STALL_SECONDS = 60

    def __init__(self, ffmpeg, url, output, session_id=None):
        self.path = Path(output)
        self.started = time.monotonic()
        self.started_at = datetime.now()
        self.ended_at = None
        self.session_id = room_session_id(session_id)
        self.stopping = None
        self.forced = False
        self.confirmed = False
        self.last_size = 0
        self.last_growth = self.started
        self.last_progress = self.started
        self.output_time = 0
        self.stalled = False
        self.message_log_path = self.path.with_suffix('.jsonl')
        self.log_path = self.path.with_suffix('.ffmpeg.log')
        self.message_log_path.touch(exist_ok=False)
        self.log = self.log_path.open('wb')
        try:
            self.process = subprocess.Popen(
                recording_command(ffmpeg, url, self.path), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception:
            self.log.close()
            raise
        threading.Thread(target=self._read_progress, daemon=True, name='recording-progress').start()

    def _read_progress(self):
        try:
            for line in self.process.stdout:
                key, _, value = line.decode('ascii', errors='replace').strip().partition('=')
                if key == 'out_time_us' and value.isdigit():
                    timestamp = int(value)
                    if timestamp > self.output_time:
                        self.output_time = timestamp
                        self.last_progress = time.monotonic()
        finally:
            self.process.stdout.close()

    def stop(self):
        if self.stopping is not None:
            return
        self.stopping = time.monotonic()
        if self.process.poll() is None:
            try:
                self.process.stdin.write(b'q\n')
                self.process.stdin.flush()
            except (OSError, ValueError):
                # The process may have exited between poll() and writing stdin.
                pass

    def poll(self):
        now = time.monotonic()
        try:
            size = self.path.stat().st_size
        except OSError:
            # Files may disappear or become temporarily unavailable on removable drives.
            size = 0
        if size > self.last_size:
            self.confirmed = size > 1024
            self.last_size = size
            self.last_growth = now
        if (self.stopping is None and self.process.poll() is None
                and now - max(self.last_growth, self.last_progress) >= self.STALL_SECONDS):
            self.stalled = True
            self.stop()
        if self.stopping is not None and now - self.stopping > 10 and self.process.poll() is None:
            self.process.kill()
            self.forced = True
        code = self.process.poll()
        if code is not None:
            if self.ended_at is None:
                self.ended_at = datetime.now()
            self.log.close()
            self.process.stdin.close()
        return code

    @property
    def status(self):
        if self.stopping is not None:
            return '录制停滞，正在结束旧进程' if self.stalled else '正在停止录制'
        if time.monotonic() - max(self.last_growth, self.last_progress) > 30:
            return '录制无数据，正在等待'
        return '录制中' if self.confirmed else '录制启动中'


@dataclass
class AnchorState:
    live: bool = False
    offline_count: int = 0
    played: bool = False
    suppressed: bool = False
    next_check: float = 0
    retry_at: float = 0
    status: str = '尚未检测'
    recording_status: str = ''
    last_check: str = ''


class LiveMonitor:
    """Call tick from the GUI thread; only HTTP requests run in the worker pool."""

    def __init__(self, fetch, notify, result, settings=None, collector=None):
        self.fetch = fetch
        self.notify = notify
        self.result = result
        self.collector = collector
        self.settings = {**DEFAULT_SETTINGS, **(settings or {})}
        self.anchors = {}
        self.cookie = ''
        self.states = {}
        self.recordings = {}
        self.recording_history = {}
        self.recording_started = {}
        self.pending = {}
        self.enabled = False
        self.closed = False
        self.generation = 0
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='room-check')
        self.external_pending = set()

    def options(self, rid):
        return {**DEFAULT_ANCHOR, **self.settings.get('anchors', {}).get(rid, {})}

    def configure(self, anchors, cookie, settings):
        self.anchors = dict(anchors)
        self.cookie = cookie
        self.settings = {**DEFAULT_SETTINGS, **settings}
        self.generation += 1
        for rid, state in self.states.items():
            state.next_check = 0
        for rid in set(self.states) - set(anchors):
            self.stop_recording(rid)
            del self.states[rid]
        if self.collector:
            for rid in list(self.collector.connections):
                if rid not in anchors:
                    self.collector.remove(rid)
            for rid in list(self.collector.automatic):
                if rid not in anchors or not self.options(rid)['enabled'] or not self.options(rid)['auto_collect']:
                    self.collector.set_automatic(rid, False)

    def start(self):
        if self.closed:
            return
        self.enabled = True
        for state in self.states.values():
            state.next_check = 0

    def stop(self):
        self.enabled = False
        self.generation += 1
        if self.collector:
            for rid in list(self.collector.automatic):
                self.collector.set_automatic(rid, False)

    def close(self):
        self.stop()
        self.closed = True
        if self.collector:
            self.collector.close()
        for rid in list(self.recordings):
            self.stop_recording(rid)
        for future, _ in self.pending.values():
            future.cancel()
        self.pool.shutdown(wait=False)

    def stop_recording(self, rid, manual=True):
        if manual:
            self.states.setdefault(rid, AnchorState()).suppressed = True
        recording = self.recordings.get(rid)
        if recording:
            recording.stop()

    def play(self, rid, url):
        launch_player(url, self.settings.get('potplayer', ''), new=True)
        self.states.setdefault(rid, AnchorState()).played = True

    def recording_for_message(self, rid, session_id, received_at):
        history = self.recording_history.get(rid, [])
        if not history:
            return None
        try:
            timestamp = datetime.fromisoformat(received_at)
        except (TypeError, ValueError):
            return None
        index = bisect_right(self.recording_started[rid], timestamp) - 1
        if index < 0:
            return None
        recording = history[index]
        if recording.ended_at and timestamp > recording.ended_at:
            return None
        if recording.session_id and recording.session_id != session_id:
            return None
        return recording

    def start_recording(self, rid, url, room_id=None):
        if self.closed:
            raise OSError('程序正在退出，不能启动录制')
        if rid in self.recordings:
            return
        ffmpeg = executable_path(self.settings.get('ffmpeg', ''), 'ffmpeg')
        folder = Path(self.settings['directory']).expanduser() / safe_component(
            f'{self.anchors.get(rid, rid)}（{rid}）')
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / (datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f') + '.mkv')
        recording = Recording(ffmpeg, url, output, room_id)
        self.recordings[rid] = recording
        history = self.recording_history.setdefault(rid, [])
        started = self.recording_started.setdefault(rid, [])
        index = bisect_right(started, recording.started_at)
        started.insert(index, recording.started_at)
        history.insert(index, recording)
        state = self.states.setdefault(rid, AnchorState())
        state.suppressed = False
        state.recording_status = '录制启动中'
        self.notify(f'{self.anchors.get(rid, rid)}：正在启动录制 → {output}；消息：{recording.message_log_path}')

    def _accept(self, rid, info):
        state = self.states.setdefault(rid, AnchorState())
        state.last_check = datetime.now().strftime('%H:%M:%S')
        state.next_check = time.monotonic() + self.settings['interval']
        if info is None:
            state.status = '检测失败，稍后重试'
            state.offline_count = 0
            self.result(rid, None)
            return
        self.result(rid, info)
        if not info['is_live']:
            state.offline_count += 1
            if state.offline_count >= 2:
                if state.live:
                    self.notify(f'{self.anchors[rid]}：已确认下播')
                state.live = state.played = state.suppressed = False
                state.retry_at = 0
                state.status = '未开播'
                self.stop_recording(rid, manual=False)
                if self.collector:
                    self.collector.room_offline(rid)
            else:
                state.status = '疑似下播，等待复核' if state.live else '未开播'
            return
        if not state.live:
            self.notify(f'{self.anchors[rid]}：检测到开播')
        state.live = True
        state.offline_count = 0
        state.status = '直播中'
        options = self.options(rid)
        if self.collector:
            self.collector.room_live(rid, info.get('room_id'))
            self.collector.set_automatic(rid, options['auto_collect'], self.cookie)
        url = select_stream(info.get('streams', {}), options['quality'])
        if not url:
            state.status = '直播中，未获取到流地址'
            return
        if time.monotonic() < state.retry_at:
            return
        if options['auto_play'] and not state.played:
            try:
                self.play(rid, url)
            except OSError as exc:
                state.status = '直播中，播放器启动失败'
                state.retry_at = time.monotonic() + max(60, self.settings['interval'])
                self.notify(f'{self.anchors[rid]}：{exc}')
        if options['auto_record'] and not state.suppressed and rid not in self.recordings:
            try:
                if info.get('room_id'):
                    self.start_recording(rid, url, info['room_id'])
                else:
                    self.start_recording(rid, url)
            except OSError as exc:
                state.recording_status = '录制启动失败'
                state.retry_at = time.monotonic() + max(60, self.settings['interval'])
                self.notify(f'{self.anchors[rid]}：录制失败：{exc}')

    def tick(self):
        for rid, recording in list(self.recordings.items()):
            state = self.states.get(rid)
            code = recording.poll()
            if code is None:
                if state:
                    state.recording_status = recording.status
                continue
            del self.recordings[rid]
            stopped = recording.stopping is not None and not recording.forced and not recording.stalled
            message = '录制已停止' if stopped else '录制中断'
            if recording.stalled:
                message = '录制连续 60 秒无进展，已结束旧进程'
            if not recording.confirmed:
                message += '（未收到有效视频数据）'
            if recording.forced:
                message += '（进程超时强制结束，请检查文件）'
            if state:
                state.recording_status = message
                state.retry_at = time.monotonic() + max(60, self.settings['interval'])
                state.next_check = state.retry_at
                # Do not reuse a check started before this recording failed.
                if rid in self.pending:
                    future, _ = self.pending[rid]
                    self.pending[rid] = (future, -1)
            self.notify(f'{self.anchors.get(rid, rid)}：{message}，文件：{recording.path}；'
                        f'消息：{recording.message_log_path}；FFmpeg 日志：{recording.log_path}')

        for rid, (future, generation) in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[rid]
            if (not self.enabled or generation != self.generation or rid not in self.anchors
                    or not self.options(rid)['enabled']):
                continue
            try:
                info = future.result()
            except Exception:
                info = None
            self._accept(rid, info)

        if not self.enabled or self.closed:
            return
        for rid in sorted(self.anchors, key=lambda item: self.states.get(item, AnchorState()).next_check):
            if not self.options(rid)['enabled']:
                continue
            state = self.states.setdefault(rid, AnchorState())
            if (rid not in self.pending and rid not in self.external_pending
                    and time.monotonic() >= state.next_check and len(self.pending) < 3):
                self.pending[rid] = (self.pool.submit(self.fetch, rid, self.cookie), self.generation)

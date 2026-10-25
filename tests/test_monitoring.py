import tempfile
import io
import threading
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from monitoring import (AnchorState, DEFAULT_SETTINGS, LiveMonitor, Recording,
                        recording_command, safe_component, select_stream)


LIVE = {'is_live': True, 'streams': {'HD1': 'https://example.test/live.flv'},
        'title': 'test', 'nickname': '主播', 'user_count': '1'}
OFFLINE = {**LIVE, 'is_live': False, 'streams': {}}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.results = []
        self.monitor = LiveMonitor(Mock(return_value=LIVE), self.events.append,
                                   lambda rid, info: self.results.append((rid, info)))
        self.monitor.configure({'1': '主播'}, 'cookie', {
            **DEFAULT_SETTINGS, 'anchors': {'1': {'enabled': True, 'auto_play': True, 'auto_record': True}}})
        self.monitor.start()

    def tearDown(self):
        self.monitor.close()

    def fake_recording(self):
        recording = Mock()
        recording.poll.return_value = None
        recording.status = '录制中'
        recording.stopping = None
        recording.forced = False
        recording.stalled = False
        recording.confirmed = True
        return recording

    def test_actions_once_per_session_and_again_after_confirmed_offline(self):
        def start(rid, url):
            self.monitor.recordings[rid] = self.fake_recording()
        with patch('monitoring.launch_player') as player, patch.object(
                self.monitor, 'start_recording', side_effect=start) as record:
            self.monitor._accept('1', LIVE)
            self.monitor._accept('1', LIVE)
            self.assertEqual(player.call_count, 1)
            self.assertEqual(record.call_count, 1)
            self.monitor._accept('1', OFFLINE)
            self.assertTrue(self.monitor.states['1'].live)
            self.monitor._accept('1', OFFLINE)
            self.assertFalse(self.monitor.states['1'].live)
            self.monitor.recordings.pop('1')
            self.monitor._accept('1', LIVE)
            self.assertEqual(player.call_count, 2)
            self.assertEqual(record.call_count, 2)

    def test_failure_is_not_offline_and_breaks_offline_streak(self):
        self.monitor.states['1'] = AnchorState(live=True)
        self.monitor.recordings['1'] = self.fake_recording()
        self.monitor._accept('1', OFFLINE)
        self.monitor._accept('1', None)
        self.monitor._accept('1', OFFLINE)
        self.assertTrue(self.monitor.states['1'].live)
        self.monitor.recordings['1'].stop.assert_not_called()

    def test_manual_stop_suppresses_restart_until_next_session(self):
        self.monitor.states['1'] = AnchorState(live=True, played=True)
        self.monitor.recordings['1'] = self.fake_recording()
        self.monitor.stop_recording('1')
        self.monitor.recordings.pop('1')
        with patch.object(self.monitor, 'start_recording') as record:
            self.monitor._accept('1', LIVE)
            record.assert_not_called()
            self.monitor._accept('1', OFFLINE)
            self.monitor._accept('1', OFFLINE)
            with patch('monitoring.launch_player'):
                self.monitor._accept('1', LIVE)
            record.assert_called_once()

    def test_stop_monitoring_leaves_recording_running(self):
        recorder = self.fake_recording()
        self.monitor.recordings['1'] = recorder
        self.monitor.stop()
        self.monitor.tick()
        recorder.stop.assert_not_called()
        self.monitor.fetch.assert_not_called()

    def test_stale_request_after_restart_does_not_launch(self):
        future = Future()
        self.monitor.pending['1'] = (future, self.monitor.generation)
        self.monitor.stop()
        self.monitor.start()
        future.set_result(LIVE)
        with patch('monitoring.launch_player') as player:
            self.monitor.tick()
            player.assert_not_called()
            self.assertEqual(self.results, [])

    def test_only_one_request_in_flight_per_anchor(self):
        future = Future()
        self.monitor.pending['1'] = (future, self.monitor.generation)
        for _ in range(5):
            self.monitor.tick()
        self.monitor.fetch.assert_not_called()
        self.assertEqual(len(self.monitor.pending), 1)

    def test_manual_request_blocks_overlapping_monitor_request(self):
        self.monitor.external_pending.add('1')
        self.monitor.tick()
        self.monitor.fetch.assert_not_called()

    def test_disabled_anchor_is_not_checked(self):
        self.monitor.settings['anchors']['1']['enabled'] = False
        self.monitor.tick()
        self.monitor.fetch.assert_not_called()

    def test_only_explicitly_selected_anchors_are_checked(self):
        self.monitor.configure({'1': '已选择', '2': '未选择', '3': '未勾选监控'}, '', {
            **DEFAULT_SETTINGS, 'anchors': {'1': {'enabled': True}, '3': {'auto_record': True}}})
        self.monitor.tick()
        for future, _ in self.monitor.pending.values():
            future.result(timeout=2)
        self.monitor.fetch.assert_called_once_with('1', '')
        self.assertFalse(self.monitor.options('2')['enabled'])
        self.assertFalse(self.monitor.options('3')['enabled'])

    def test_removing_from_monitoring_ignores_inflight_result(self):
        future = Future()
        self.monitor.pending['1'] = (future, self.monitor.generation)
        settings = {**self.monitor.settings, 'anchors': {'1': {'enabled': False}}}
        self.monitor.configure(self.monitor.anchors, '', settings)
        future.set_result(LIVE)
        with patch('monitoring.launch_player') as player:
            self.monitor.tick()
            player.assert_not_called()
        self.assertEqual(self.results, [])
        self.monitor.fetch.assert_not_called()

    def test_deleted_anchor_ignores_pending_result(self):
        future = Future()
        self.monitor.pending['1'] = (future, self.monitor.generation)
        self.monitor.configure({}, '', self.monitor.settings)
        future.set_result(LIVE)
        self.monitor.tick()
        self.assertEqual(self.results, [])

    def test_failed_recording_retry_is_delayed(self):
        self.monitor.states['1'] = AnchorState(live=True, played=True)
        recording = self.fake_recording()
        recording.poll.return_value = 1
        self.monitor.recordings['1'] = recording
        self.monitor.tick()
        with patch.object(self.monitor, 'start_recording') as record:
            self.monitor._accept('1', LIVE)
            record.assert_not_called()
            self.monitor.states['1'].retry_at = 0
            self.monitor._accept('1', LIVE)
            record.assert_called_once()

    def test_close_stops_owned_recordings(self):
        recording = self.fake_recording()
        self.monitor.recordings['1'] = recording
        self.monitor.close()
        recording.stop.assert_called_once()
        with self.assertRaises(OSError):
            self.monitor.start_recording('1', LIVE['streams']['HD1'])

    def test_stalled_recording_retries_with_fresh_url_after_delay(self):
        self.monitor.states['1'] = AnchorState(live=True, played=True)
        recorder = self.fake_recording()
        recorder.stalled = True
        recorder.stopping = time.monotonic()
        recorder.poll.return_value = 0
        self.monitor.recordings['1'] = recorder
        old = Future()
        self.monitor.pending['1'] = (old, self.monitor.generation)
        self.monitor.tick()
        old.set_result(LIVE)
        self.monitor.tick()
        self.assertEqual(self.results, [])
        with patch.object(self.monitor, 'start_recording') as start:
            self.monitor._accept('1', LIVE)
            start.assert_not_called()
            self.monitor.states['1'].retry_at = 0
            self.monitor._accept('1', {**LIVE, 'streams': {'HD1': 'https://example.test/fresh.flv'}})
            start.assert_called_once_with('1', 'https://example.test/fresh.flv')

    def test_manual_stop_of_stalled_recording_suppresses_recovery(self):
        self.monitor.states['1'] = AnchorState(live=True, played=True)
        recorder = self.fake_recording()
        recorder.stalled = True
        recorder.poll.return_value = 0
        self.monitor.recordings['1'] = recorder
        self.monitor.stop_recording('1')
        self.monitor.tick()
        self.monitor.states['1'].retry_at = 0
        with patch.object(self.monitor, 'start_recording') as start:
            self.monitor._accept('1', LIVE)
            start.assert_not_called()

    def test_background_fetch_returns_to_caller_thread(self):
        self.monitor.settings['anchors']['1'] = {'enabled': True}
        thread_ids = []
        self.monitor.fetch = lambda rid, cookie: thread_ids.append(threading.get_ident()) or LIVE
        self.monitor.tick()
        for future, _ in self.monitor.pending.values():
            future.result(timeout=2)
        self.monitor.tick()
        self.assertEqual(len(self.results), 1)
        self.assertNotEqual(thread_ids[0], threading.get_ident())


class RecordingTests(unittest.TestCase):
    def test_video_uses_message_log_and_separate_ffmpeg_log(self):
        with tempfile.TemporaryDirectory() as directory, patch('monitoring.subprocess.Popen') as popen:
            popen.return_value.stdout = io.BytesIO()
            popen.return_value.poll.return_value = None
            path = Path(directory) / 'video.mkv'
            recording = Recording('ffmpeg', LIVE['streams']['HD1'], path, session_id='room_1')
            self.assertEqual(recording.message_log_path, path.with_suffix('.jsonl'))
            self.assertEqual(recording.log_path, path.with_suffix('.ffmpeg.log'))
            self.assertEqual(recording.session_id, 'room_1')
            recording.stop()
            popen.return_value.poll.return_value = 0
            recording.poll()

    def test_stall_requests_graceful_stop_before_kill(self):
        with tempfile.TemporaryDirectory() as directory, patch('monitoring.subprocess.Popen') as popen:
            process = popen.return_value
            process.stdout = io.BytesIO()
            process.poll.return_value = None
            recording = Recording('ffmpeg', LIVE['streams']['HD1'], Path(directory) / 'video.mkv')
            recording.last_growth = recording.last_progress = time.monotonic() - 61
            recording.poll()
            self.assertTrue(recording.stalled)
            process.stdin.write.assert_called_once_with(b'q\n')
            process.kill.assert_not_called()
            process.poll.return_value = 0
            recording.poll()

    def test_progress_prevents_false_stall_during_buffered_writes(self):
        with tempfile.TemporaryDirectory() as directory, patch('monitoring.subprocess.Popen') as popen:
            process = popen.return_value
            process.stdout = io.BytesIO(b'out_time_us=1000000\nprogress=continue\n')
            process.poll.return_value = None
            recording = Recording('ffmpeg', LIVE['streams']['HD1'], Path(directory) / 'video.mkv')
            recording.last_growth = time.monotonic() - 61
            recording.poll()
            self.assertFalse(recording.stalled)
            process.stdin.write.assert_not_called()
            process.poll.return_value = 0
            recording.poll()

    def test_stop_uses_quit_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory, patch('monitoring.subprocess.Popen') as popen:
            process = popen.return_value
            process.stdout = io.BytesIO()
            process.poll.return_value = None
            recording = Recording('ffmpeg', LIVE['streams']['HD1'], Path(directory) / 'video.mkv')
            recording.stop()
            recording.stop()
            process.stdin.write.assert_called_once_with(b'q\n')
            process.kill.assert_not_called()
            recording.stopping = time.monotonic() - 11
            recording.poll()
            process.kill.assert_called_once()
            process.poll.return_value = -1
            recording.poll()

    def test_process_start_is_not_recording_confirmation(self):
        with tempfile.TemporaryDirectory() as directory, patch('monitoring.subprocess.Popen') as popen:
            popen.return_value.stdout = io.BytesIO()
            popen.return_value.poll.return_value = None
            path = Path(directory) / 'video.mkv'
            recording = Recording('ffmpeg', LIVE['streams']['HD1'], path)
            recording.poll()
            self.assertEqual(recording.status, '录制启动中')
            path.write_bytes(b'x' * 2048)
            recording.poll()
            self.assertEqual(recording.status, '录制中')
            popen.return_value.poll.return_value = 0
            recording.poll()

    def test_stream_copy_without_shell_and_without_overwriting(self):
        command = recording_command('ffmpeg', 'https://example.test/?a=1&b=2', 'movie.mkv')
        self.assertIn('-n', command)
        self.assertEqual(command[command.index('-c') + 1], 'copy')
        self.assertIn('https://example.test/?a=1&b=2', command)

    def test_quality_fallback_and_safe_windows_filename(self):
        self.assertEqual(select_stream({'HD1': 'hd'}, 'origin'), 'hd')
        self.assertIsNone(select_stream({'origin': ''}))
        self.assertEqual(safe_component('a/b:c*'), 'a_b_c_')
        self.assertEqual(safe_component('CON'), '_CON')


if __name__ == '__main__':
    unittest.main()

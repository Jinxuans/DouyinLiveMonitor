"""One message connection per room, controlled by monitoring settings."""

import queue
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime


def room_session_id(room_id):
    value = str(room_id or '')
    if value.startswith('room_') and value[5:].isascii() and value[5:].isdecimal() and value[5:].strip('0'):
        return value
    if len(value) <= 32 and value.isascii() and value.isdecimal() and value.strip('0'):
        return f'room_{value}'
    return None


@dataclass
class MessageConnection:
    fetcher: object
    stopping: bool = False


class MessageCollector:
    RETRY_SECONDS = 30

    def __init__(self, factory, on_message, notify):
        self.factory = factory
        self.on_message = on_message
        self.notify = notify
        self.automatic = set()
        self.connections = {}
        self.statuses = {}
        self.retry_at = {}
        self.ended = set()
        self.cookies = {}
        self.sessions = {}
        self.events = queue.Queue()
        self.closed = False

    def wanted(self, rid):
        return not self.closed and rid in self.automatic and rid not in self.ended

    def set_automatic(self, rid, enabled, cookie=''):
        if enabled and not self.closed:
            self.automatic.add(rid)
            self.cookies[rid] = cookie
            self._ensure(rid)
        else:
            self.automatic.discard(rid)
            if not self.wanted(rid):
                self._stop(rid)

    def room_offline(self, rid):
        self.automatic.discard(rid)
        self.ended.add(rid)
        self._stop(rid)
        self.sessions.pop(rid, None)
        self.statuses[rid] = '直播已结束'

    def room_live(self, rid, room_id=None):
        session_id = room_session_id(room_id)
        if session_id and session_id != self.sessions.get(rid):
            self._stop(rid)
            self.sessions[rid] = session_id
            self.retry_at.pop(rid, None)
        self.ended.discard(rid)

    def remove(self, rid):
        self.automatic.discard(rid)
        self._stop(rid)
        self.cookies.pop(rid, None)
        self.sessions.pop(rid, None)
        self.retry_at.pop(rid, None)
        self.ended.discard(rid)

    def _ensure(self, rid):
        if not self.wanted(rid) or rid in self.connections or time.monotonic() < self.retry_at.get(rid, 0):
            return

        if rid not in self.sessions:
            self.sessions[rid] = f'local_{datetime.now():%Y-%m-%d_%H-%M-%S}_{uuid.uuid4().hex}'
        session_id = self.sessions[rid]

        def on_message(kind, data):
            received = datetime.now()
            self.events.put((rid, connection, 'message', {
                **data, 'type': kind, 'rid': rid,
                # Bind queued messages to their connection's session, never the current room state.
                'session_id': room_session_id(data.get('room_id')) or session_id,
                'received_at': received.isoformat(), 'received_date': received.date().isoformat()}))

        def on_status(status):
            self.events.put((rid, connection, status, None))

        try:
            fetcher = self.factory(rid, on_message=on_message, on_status=on_status,
                                   cookie=self.cookies.get(rid, ''))
        except Exception as exc:
            self.statuses[rid] = '连接失败，等待重试'
            self.retry_at[rid] = time.monotonic() + self.RETRY_SECONDS
            self.notify(f'{rid}：消息连接启动失败（{type(exc).__name__}）')
            return
        connection = MessageConnection(fetcher)
        self.connections[rid] = connection
        self.statuses[rid] = '正在连接消息'

        def run():
            error = None
            try:
                fetcher.start()
            except Exception as exc:
                error = type(exc).__name__
            finally:
                self.events.put((rid, connection, 'finished', error))

        threading.Thread(target=run, name=f'messages-{rid}', daemon=True).start()

    def _stop(self, rid):
        connection = self.connections.get(rid)
        if connection and not connection.stopping:
            connection.stopping = True
            self.statuses[rid] = '正在停止消息'

            def stop():
                try:
                    connection.fetcher.stop()
                except Exception as exc:
                    self.events.put((rid, connection, 'stop_error', type(exc).__name__))

            threading.Thread(target=stop, name=f'stop-messages-{rid}', daemon=True).start()

    def status(self, rid):
        return self.statuses.get(rid, '未采集')

    def tick(self):
        for _ in range(500 if not self.closed else self.events.qsize()):
            try:
                rid, connection, kind, data = self.events.get_nowait()
            except queue.Empty:
                break
            if self.connections.get(rid) is not connection:
                continue
            if kind == 'message':
                self.on_message(data)
            elif kind == 'connected' and not connection.stopping:
                self.statuses[rid] = '采集中'
            elif kind == 'ended' and not connection.stopping:
                self.room_offline(rid)
            elif kind == 'stop_error':
                self.notify(f'{rid}：消息连接停止失败（{data}）')
            elif kind == 'finished':
                del self.connections[rid]
                if connection.stopping or rid in self.ended or not self.wanted(rid):
                    self.statuses[rid] = '直播已结束' if rid in self.ended else '已停止采集'
                else:
                    self.statuses[rid] = '消息断开，等待重连'
                    self.retry_at[rid] = time.monotonic() + self.RETRY_SECONDS
                    self.notify(f'{rid}：消息连接已断开，{self.RETRY_SECONDS} 秒后重试')
        for rid in self.automatic:
            self._ensure(rid)

    def close(self):
        self.closed = True
        self.automatic.clear()
        for rid in list(self.connections):
            self._stop(rid)

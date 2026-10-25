import threading
import time
import unittest
from unittest.mock import Mock, patch

from message_collector import MessageCollector
from monitoring import DEFAULT_SETTINGS, LiveMonitor


class FakeFetcher:
    def __init__(self, rid, on_message, on_status, cookie=''):
        self.rid = rid
        self.on_message = on_message
        self.on_status = on_status
        self.stopped = threading.Event()
        self.started = threading.Event()

    def start(self):
        self.started.set()
        if not self.stopped.is_set():
            self.on_status('connected')
            self.stopped.wait(3)

    def stop(self):
        self.stopped.set()


class MessageCollectorTests(unittest.TestCase):
    def setUp(self):
        self.messages = []
        self.factory = Mock(side_effect=FakeFetcher)
        self.collector = MessageCollector(self.factory, self.messages.append, Mock())

    def drain_until(self, condition):
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            self.collector.tick()
            time.sleep(0.005)
        self.assertTrue(condition())

    def tearDown(self):
        self.collector.close()
        self.drain_until(lambda: not self.collector.connections)

    def test_repeated_start_reuses_connection(self):
        self.collector.set_automatic('1', True)
        first = self.collector.connections['1']
        self.collector.set_automatic('1', True)
        self.collector.set_automatic('2', True)
        self.assertIs(self.collector.connections['1'], first)
        self.assertFalse(first.stopping)
        self.assertEqual(self.factory.call_count, 2)

    def test_reconnect_and_pause_keep_session_but_new_room_splits_it(self):
        self.collector.room_live('1', '100')
        self.collector.set_automatic('1', True)
        first = self.collector.connections['1']
        first.fetcher.on_message('chat', {'text': 'before'})
        first.fetcher.stop()
        self.drain_until(lambda: not self.collector.connections)
        self.collector.retry_at['1'] = 0
        self.collector.tick()
        self.collector.connections['1'].fetcher.on_message('chat', {'text': 'reconnected'})
        self.collector.set_automatic('1', False)
        self.drain_until(lambda: not self.collector.connections)
        self.collector.room_live('1', '100')
        self.collector.set_automatic('1', True)
        old = self.collector.connections['1']
        old.fetcher.on_message('chat', {'text': 'resumed'})
        # A new platform room can appear between polls without observing offline.
        self.collector.room_live('1', '200')
        old.fetcher.on_status('ended')
        self.drain_until(lambda: self.collector.connections.get('1') is not old)
        self.collector.tick()
        self.collector.connections['1'].fetcher.on_message('chat', {'text': 'new'})
        self.collector.tick()
        self.assertEqual([m['session_id'] for m in self.messages],
                         ['room_100', 'room_100', 'room_100', 'room_200'])

    def test_local_session_rolls_only_after_confirmed_end(self):
        self.collector.set_automatic('1', True)
        first_id = self.collector.sessions['1']
        self.collector.set_automatic('1', False)
        self.drain_until(lambda: not self.collector.connections)
        self.collector.room_live('1')
        self.collector.set_automatic('1', True)
        self.assertEqual(self.collector.sessions['1'], first_id)
        self.collector.room_offline('1')
        self.drain_until(lambda: not self.collector.connections)
        self.collector.room_live('1')
        self.collector.set_automatic('1', True)
        self.assertNotEqual(self.collector.sessions['1'], first_id)

    def test_actual_socket_room_takes_priority_over_previous_poll(self):
        self.collector.room_live('1', '100')
        self.collector.set_automatic('1', True)
        self.collector.connections['1'].fetcher.on_message('chat', {'room_id': '200'})
        self.collector.tick()
        self.assertEqual(self.messages[0]['session_id'], 'room_200')

    def test_messages_keep_room_identity(self):
        self.collector.set_automatic('1', True)
        self.collector.set_automatic('2', True)
        for rid, connection in self.collector.connections.items():
            connection.fetcher.on_message('gift', {'text': 'gift-' + rid, 'rid': 'untrusted'})
        self.collector.tick()
        self.assertEqual({msg['rid']: msg['text'] for msg in self.messages}, {'1': 'gift-1', '2': 'gift-2'})
        self.assertTrue(all(msg['received_date'] == msg['received_at'][:10] for msg in self.messages))

    def test_disconnect_reconnects_after_delay_only_when_wanted(self):
        self.collector.set_automatic('1', True)
        self.collector.connections['1'].fetcher.stop()
        self.drain_until(lambda: '1' not in self.collector.connections)
        self.collector.tick()
        self.assertEqual(self.factory.call_count, 1)
        self.collector.retry_at['1'] = 0
        self.collector.tick()
        self.assertEqual(self.factory.call_count, 2)
        self.collector.set_automatic('1', False)
        self.drain_until(lambda: not self.collector.connections)
        self.collector.retry_at['1'] = 0
        self.collector.tick()
        self.assertEqual(self.factory.call_count, 2)

    def test_confirmed_offline_stops_connection(self):
        self.collector.set_automatic('1', True)
        self.collector.room_offline('1')
        self.drain_until(lambda: not self.collector.connections)
        self.collector.tick()
        self.assertEqual(self.factory.call_count, 1)
        self.assertEqual(self.collector.status('1'), '直播已结束')

    def test_disabling_auto_collection_stops_connection_and_retries(self):
        self.collector.set_automatic('1', True)
        self.collector.set_automatic('1', False)
        self.assertTrue(self.collector.connections['1'].stopping)
        self.drain_until(lambda: not self.collector.connections)
        self.collector.tick()
        self.assertEqual(self.factory.call_count, 1)

    def test_close_during_connect_does_not_restart(self):
        self.collector.set_automatic('1', True)
        self.collector.close()
        self.drain_until(lambda: not self.collector.connections)
        self.collector.set_automatic('1', True)
        self.collector.tick()
        self.assertEqual(self.factory.call_count, 1)

    def test_monitor_controls_stop_collection_and_prevent_reconnect(self):
        for action in ('stop', 'enabled', 'auto_collect'):
            with self.subTest(action=action):
                settings = {**DEFAULT_SETTINGS, 'anchors': {
                    '1': {'enabled': True, 'auto_collect': True}}}
                monitor = LiveMonitor(Mock(), Mock(), Mock(), collector=self.collector)
                monitor.configure({'1': '主播'}, '', settings)
                monitor.start()
                try:
                    monitor._accept('1', {'is_live': True, 'streams': {}})
                    count = self.factory.call_count
                    if action == 'stop':
                        monitor.stop()
                    else:
                        settings['anchors']['1'][action] = False
                        monitor.configure(monitor.anchors, '', settings)
                    self.assertTrue(self.collector.connections['1'].stopping)
                    self.drain_until(lambda: not self.collector.connections)
                    self.collector.retry_at['1'] = 0
                    self.collector.tick()
                    self.assertEqual(self.factory.call_count, count)
                finally:
                    monitor.stop()
                    monitor.pool.shutdown(wait=True)

    def test_monitor_starts_message_collection_without_video_url(self):
        monitor = LiveMonitor(Mock(), Mock(), Mock(), collector=self.collector)
        monitor.configure({'1': '主播'}, '', {**DEFAULT_SETTINGS,
                          'anchors': {'1': {'enabled': True, 'auto_collect': True}}})
        monitor.start()
        try:
            monitor._accept('1', {'is_live': True, 'streams': {}, 'room_id': '123'})
            self.assertIn('1', self.collector.connections)
            self.assertEqual(self.collector.sessions['1'], 'room_123')
            monitor._accept('1', None)
            self.assertFalse(self.collector.connections['1'].stopping)
            monitor._accept('1', {'is_live': False})
            self.assertFalse(self.collector.connections['1'].stopping)
            monitor._accept('1', {'is_live': False})
            self.assertTrue(self.collector.connections['1'].stopping)
        finally:
            monitor.close()


if __name__ == '__main__':
    unittest.main()

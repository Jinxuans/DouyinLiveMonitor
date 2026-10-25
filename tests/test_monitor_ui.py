import unittest
import json
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from chat_window import ChatWindow, fetch_room_info


class MonitorUITests(unittest.TestCase):
    def setUp(self):
        with patch('chat_window.load_config', return_value={
                'anchors': {'1': '主播一', '2': '主播二'}, 'cookie': ''}):
            self.window = ChatWindow()
        self.window.root.withdraw()
        self.window._check_status = Mock()

    def tearDown(self):
        self.window.monitor.close()
        for callback in self.window.root.tk.call('after', 'info'):
            self.window.root.after_cancel(callback)
        self.window.root.destroy()

    def test_monitor_does_not_switch_selected_chat(self):
        window = self.window
        window._current_rid = '1'
        window.anchor_listbox.selection_set(0)
        window._on_monitor_result('2', {'is_live': True, 'streams': {}, 'title': '直播'})
        window.root.update()
        self.assertEqual(window._get_selected_rid(), '1')
        window._check_status.assert_not_called()
        self.assertIn('[直播]', window.anchor_listbox.get(1))

    def test_settings_can_open_and_monitor_rows_refresh(self):
        self.window.monitor_panel.refresh()
        self.window.monitor_panel.show_settings()
        self.window.root.update()
        self.assertEqual(len(self.window.monitor_panel.tree.get_children()), 2)

    def test_monitor_selection_is_saved_without_enabling_other_anchors(self):
        panel = self.window.monitor_panel
        panel.tree.selection_set('1')
        panel.sync_selected_monitoring()
        self.assertFalse(panel.anchor_enabled.get())
        with patch('chat_window.save_config') as save:
            panel.anchor_enabled.set(True)
            panel.set_selected_monitoring()
            self.assertTrue(save.call_args[0][0]['monitor']['anchors']['1']['enabled'])
            self.assertTrue(self.window.monitor.options('1')['enabled'])
            self.assertFalse(self.window.monitor.options('2')['enabled'])
            self.assertEqual(panel.tree.set('1', 'enabled'), '已加入')
            panel.anchor_enabled.set(False)
            panel.set_selected_monitoring()
            self.assertFalse(save.call_args[0][0]['monitor']['anchors']['1']['enabled'])
            self.assertEqual(panel.tree.set('1', 'enabled'), '未加入')

    def test_empty_monitor_selection_does_not_start_monitoring(self):
        with patch('monitor_panel.messagebox.showinfo') as info:
            self.window.monitor_panel.toggle_monitor()
        self.assertFalse(self.window.monitor.enabled)
        info.assert_called_once()

    def test_selecting_unmonitored_room_does_not_collect_when_it_goes_live(self):
        window = self.window
        window.anchor_listbox.selection_set(0)
        info = {'is_live': False, 'title': '', 'user_count': 0, 'nickname': '主播一', 'streams': {}}
        with patch.object(window.collector, 'factory') as factory:
            window._on_anchor_select(None)
            window._on_status_checked('1', info)
            window._on_status_checked('1', {**info, 'is_live': True})
            window.collector.tick()
            factory.assert_not_called()

    def test_switching_to_unmonitored_live_room_does_not_collect(self):
        window = self.window
        window._current_rid = '1'
        window.anchor_listbox.selection_set(1)
        with patch.object(window.collector, 'factory') as factory:
            window._on_anchor_select(None)
            window._on_status_checked('2', {
                'is_live': True, 'title': '', 'user_count': 0, 'nickname': '主播二', 'streams': {}})
            window.collector.tick()
            factory.assert_not_called()

    def test_empty_room_response_is_failure_not_offline(self):
        with patch('chat_window.requests.Session') as session:
            session.return_value.get.return_value.json.return_value = {'data': {'data': []}}
            self.assertIsNone(fetch_room_info('1', 'test-cookie'))

    def test_room_info_keeps_platform_room_id_separate_from_anchor_id(self):
        with patch('chat_window.requests.Session') as session:
            session.return_value.get.return_value.json.return_value = {
                'data': {'data': [{'status': 2, 'id_str': '1234567890123456789'}]}}
            self.assertEqual(fetch_room_info('1', 'test-cookie')['room_id'], '1234567890123456789')

    def test_room_info_adds_fresh_ttwid_when_cookie_omits_it(self):
        with patch('chat_window.requests.Session') as session_factory:
            session = session_factory.return_value
            session.cookies.get.return_value = 'fresh-ttwid'
            response = session.get.return_value
            response.json.return_value = {
                'data': {'data': [{'status': 2, 'id_str': '1234567890123456789'}]}}

            info = fetch_room_info('1', '"sessionid=test"')

        self.assertEqual(info['room_id'], '1234567890123456789')
        self.assertEqual(session.get.call_count, 2)
        request_headers = session.get.call_args_list[1].kwargs['headers']
        self.assertEqual(request_headers['Cookie'], 'sessionid=test; ttwid=fresh-ttwid')

    def test_background_messages_are_saved_to_source_room_across_midnight(self):
        window = self.window
        window._current_rid = '1'
        window._filter_vars['gift'].set(True)
        with tempfile.TemporaryDirectory() as directory:
            window._data_dir = directory
            for rid, day, kind in [('2', '2026-10-16', 'gift'), ('1', '2026-10-17', 'chat')]:
                window._enqueue_message({'rid': rid, 'type': kind, 'text': 'message-' + rid,
                                         'session_id': 'room_' + rid,
                                         'received_date': day, 'received_at': day + 'T00:00:01'})
            window._poll_queue(reschedule=False)
            files = list(Path(directory).glob('*/*.jsonl'))
            self.assertEqual(len(files), 2)
            records = {json.loads(path.read_text(encoding='utf-8'))['rid']: path for path in files}
            self.assertEqual(records['2'].parent.name, '主播二（2）')
            self.assertEqual(records['2'].name, 'room_2.jsonl')
            self.assertEqual(records['1'].name, 'room_1.jsonl')
            visible = window.chat_area.get('1.0', 'end')
            self.assertIn('[主播一（1）] message-1', visible)
            self.assertIn('[主播二（2）] message-2', visible)

    def test_background_messages_display_without_manual_selection(self):
        self.assertIsNone(self.window._current_rid)
        with patch.object(self.window, '_save_message') as save:
            self.window._enqueue_message({'rid': '2', 'type': 'chat', 'text': 'background-chat'})
            self.window._poll_queue(reschedule=False)
        self.assertIn('[主播二（2）] background-chat', self.window.chat_area.get('1.0', 'end'))
        save.assert_called_once()

    def test_background_stats_do_not_replace_selected_room_viewers(self):
        self.window._current_rid = '1'
        with patch.object(self.window, '_save_message'):
            self.window._enqueue_message({'rid': '1', 'type': 'stats', 'current': 10})
            self.window._enqueue_message({'rid': '2', 'type': 'stats', 'current': 900})
            self.window._poll_queue(reschedule=False)
        self.assertIn('10', self.window.viewer_label.cget('text'))
        self.assertNotIn('900', self.window.viewer_label.cget('text'))

    def test_selecting_room_keeps_collected_message_history(self):
        self.window._append_line('existing-background-message')
        self.window.anchor_listbox.selection_set(1)
        self.window._on_anchor_select(None)
        self.assertEqual(self.window._current_rid, '2')
        self.assertIn('existing-background-message', self.window.chat_area.get('1.0', 'end'))

    def test_filtered_messages_are_saved_and_archive_failures_are_reported(self):
        self.window._current_rid = '1'
        with tempfile.TemporaryDirectory() as directory:
            self.window._data_dir = directory
            self.window._enqueue_message({'rid': '1', 'session_id': 'room_1', 'type': 'gift', 'text': 'hidden-gift'})
            self.window._poll_queue(reschedule=False)
            self.assertNotIn('hidden-gift', self.window.chat_area.get('1.0', 'end'))
            self.assertEqual(len(list(Path(directory).glob('*/*.jsonl'))), 1)
        with patch('chat_window.os.makedirs', side_effect=OSError('disk full')), patch.object(self.window, '_append_system') as log:
            self.window._save_message({'rid': '1', 'session_id': 'room_1', 'type': 'chat'})
            self.window._save_message({'rid': '1', 'session_id': 'room_1', 'type': 'chat'})
            log.assert_called_once()

    def test_messages_are_saved_next_to_the_active_video(self):
        window = self.window
        with tempfile.TemporaryDirectory() as directory:
            window._data_dir = directory
            first_path = Path(directory) / '2026-10-25_00-07-41_496891.jsonl'
            second_path = Path(directory) / '2026-10-25_00-08-41_496891.jsonl'
            first = SimpleNamespace(
                session_id='room_1', message_log_path=first_path,
                started_at=datetime.fromisoformat('2026-10-25T00:00:00'),
                ended_at=datetime.fromisoformat('2026-10-25T00:02:00'))
            second = SimpleNamespace(
                session_id='room_1', message_log_path=second_path,
                started_at=datetime.fromisoformat('2026-10-25T00:03:00'), ended_at=None)
            window.monitor.recording_history['1'] = [first, second]
            window.monitor.recording_started['1'] = [first.started_at, second.started_at]
            for received_at, text in [('2026-10-25T00:01:02', '第一段弹幕'),
                                      ('2026-10-25T00:02:30', '不属于任何录像的消息'),
                                      ('2026-10-25T00:03:02', '第二段礼物')]:
                window._save_message({'rid': '1', 'session_id': 'room_1', 'type': 'chat',
                                      'time_str': received_at[-8:], 'text': text,
                                      'received_at': received_at})
            first_lines = first_path.read_text(encoding='utf-8').splitlines()
            second_lines = second_path.read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(first_lines), 1)
            self.assertIn('第一段弹幕', first_lines[0])
            self.assertEqual(len(second_lines), 1)
            self.assertIn('第二段礼物', second_lines[0])
            self.assertIsNone(window.monitor.recording_for_message(
                '1', 'room_1', '2026-10-25T00:02:30'))
            self.assertIsNone(window.monitor.recording_for_message(
                '2', 'room_1', '2026-10-25T00:01:02'))

    def test_session_archive_survives_midnight_and_restart_without_touching_daily_file(self):
        window = self.window
        with tempfile.TemporaryDirectory() as directory:
            window._data_dir = directory
            folder = Path(directory) / '主播一（1）'
            folder.mkdir()
            legacy = folder / '2026-10-16.jsonl'
            legacy.write_text('legacy\n', encoding='utf-8')
            for index, (session, received) in enumerate([
                    ('room_100', '2026-10-16T23:59:59'),
                    ('room_100', '2026-10-17T00:00:01'),
                    ('room_200', '2026-10-17T00:01:00')]):
                # Recreate the GUI to exercise a process restart with no cached paths.
                if index == 1:
                    self.tearDown()
                    self.setUp()
                    window = self.window
                    window._data_dir = directory
                    window.anchors['1'] = '改名后的主播'
                window._save_message({'rid': '1', 'session_id': session, 'type': 'chat',
                                      'text': str(index), 'received_at': received})
            files = list(Path(directory).glob('*/room_*.jsonl'))
            self.assertEqual(len(files), 2)
            first = next(path for path in files if path.name == 'room_100.jsonl')
            messages = [json.loads(line) for line in first.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([msg['text'] for msg in messages], ['0', '1'])
            self.assertEqual({msg['session_id'] for msg in messages}, {'room_100'})
            self.assertEqual(legacy.read_text(encoding='utf-8'), 'legacy\n')


if __name__ == '__main__':
    unittest.main()

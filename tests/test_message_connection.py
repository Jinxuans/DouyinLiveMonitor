import gzip
import unittest
from unittest.mock import Mock, patch

import websocket

from liveMan import DouyinLiveWebFetcher
from protobuf.douyin import PushFrame, Response, ControlMessage


class MessageConnectionTests(unittest.TestCase):
    def setUp(self):
        self.status = Mock()
        self.fetcher = DouyinLiveWebFetcher('1', cookie='test', on_status=self.status)
        self.fetcher._DouyinLiveWebFetcher__room_id = '123'

    def tearDown(self):
        self.fetcher.stop()
        self.fetcher.session.close()

    def test_heartbeat_and_response_timeout_are_enabled(self):
        with patch('liveMan.generateSignature', return_value='test'), patch('liveMan.websocket.WebSocketApp') as app:
            self.fetcher.start()
            args = app.return_value.run_forever.call_args.kwargs
            self.assertEqual(args['ping_interval'], 5)
            self.assertEqual(args['ping_timeout'], 4)
            self.assertEqual(PushFrame().parse(args['ping_payload']).payload_type, 'hb')
            self.status.assert_not_called()

    def test_stop_before_start_prevents_connection(self):
        self.fetcher.stop()
        with patch('liveMan.websocket.WebSocketApp') as app:
            self.fetcher.start()
            app.assert_not_called()

    def test_late_open_after_stop_is_closed_not_reported_connected(self):
        self.fetcher.stop()
        ws = Mock()
        self.fetcher._wsOnOpen(ws)
        ws.close.assert_called_once()
        self.status.assert_not_called()

    def test_ack_still_sent_without_chat_messages(self):
        ws = Mock()
        response = Response(need_ack=True, internal_ext='ack-payload')
        package = PushFrame(log_id=123, payload=gzip.compress(bytes(response)))
        self.fetcher._wsOnMessage(ws, bytes(package))
        payload, opcode = ws.send.call_args.args
        ack = PushFrame().parse(payload)
        self.assertEqual((ack.log_id, ack.payload_type, ack.payload), (123, 'ack', b'ack-payload'))
        self.assertEqual(opcode, websocket.ABNF.OPCODE_BINARY)

    def test_end_notification_stops_connection(self):
        self.fetcher._parseControlMsg(bytes(ControlMessage(status=3)))
        self.status.assert_called_once_with('ended')
        self.assertTrue(self.fetcher._stopped.is_set())

    def test_messages_include_actual_socket_room_id_without_network_lookup(self):
        self.fetcher.on_message = Mock()
        with patch.object(self.fetcher.session, 'get') as request:
            self.fetcher._emit('chat', '23:59:59', 'message')
            request.assert_not_called()
        data = self.fetcher.on_message.call_args.args[1]
        self.assertEqual(data['room_id'], '123')
        self.assertEqual(data['time_str'], '23:59:59')


if __name__ == '__main__':
    unittest.main()

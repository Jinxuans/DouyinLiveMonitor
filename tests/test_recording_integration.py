"""Use installed FFmpeg against a local HTTP stream, with no Douyin account required."""

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from monitoring import Recording, executable_path


class RecordingIntegrationTests(unittest.TestCase):
    def test_http_stream_can_be_stopped_and_result_is_playable(self):
        self.record_http(stall=False)

    def test_stalled_http_stream_is_detected_and_old_process_exits(self):
        self.record_http(stall=True)

    def record_http(self, stall):
        try:
            ffmpeg = executable_path(os.environ.get('TEST_FFMPEG', ''), 'ffmpeg')
        except FileNotFoundError:
            self.skipTest('FFmpeg is not installed')
        ffprobe = str(Path(ffmpeg).with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe'))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.flv'
            subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=25',
                            '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=44100',
                            '-t', '30', '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac',
                            '-f', 'flv', str(source)], check=True, timeout=30,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            payload = source.read_bytes()
            shutdown = threading.Event()
            pause_stream = threading.Event()

            class Stream(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    self.send_response(200)
                    self.send_header('Content-Type', 'video/x-flv')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    try:
                        for offset in range(0, len(payload), 8192):
                            if shutdown.wait(0.025):
                                break
                            if pause_stream.is_set():
                                shutdown.wait(20)
                                break
                            self.wfile.write(payload[offset:offset + 8192])
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass

            server = ThreadingHTTPServer(('127.0.0.1', 0), Stream)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            recording = Recording(ffmpeg, f'http://127.0.0.1:{server.server_port}/live.flv',
                                  Path(directory) / 'recorded.mkv')
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and not recording.confirmed:
                    code = recording.poll()
                    if code is not None:
                        self.fail(f'FFmpeg exited: {recording.log_path.read_text()}')
                    time.sleep(0.1)
                self.assertTrue(recording.confirmed, recording.log_path.read_text())
                if stall:
                    pause_stream.set()
                    # Shorten only the watchdog threshold; exercise real HTTP/FFmpeg IO.
                    recording.STALL_SECONDS = 1
                else:
                    recording.stop()
                deadline = time.monotonic() + 18
                while recording.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.1)
                if stall:
                    self.assertTrue(recording.stalled)
                    self.assertIsNotNone(recording.process.poll())
                else:
                    self.assertEqual(recording.process.poll(), 0, recording.log_path.read_text())
                    self.assertFalse(recording.forced)
                result = subprocess.run([ffprobe, '-v', 'error', '-show_streams', '-show_format',
                                         '-of', 'json', str(recording.path)], capture_output=True,
                                        text=True, check=True, timeout=10,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                metadata = json.loads(result.stdout)
                self.assertEqual({stream['codec_name'] for stream in metadata['streams']}, {'h264', 'aac'})
                if not recording.forced:
                    self.assertGreater(float(metadata['format']['duration']), 0)
            finally:
                recording.stop()
                if recording.process.poll() is None:
                    recording.process.kill()
                    recording.process.wait(timeout=5)
                recording.poll()
                shutdown.set()
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()

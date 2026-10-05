from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from simple_websocket import ConnectionClosed

from tests import test_tasks


class FrameChunkTests(unittest.TestCase):
    setUp = test_tasks.TaskTests.setUp
    tearDown = test_tasks.TaskTests.tearDown
    new_task = test_tasks.TaskTests.new_task
    wait_state = test_tasks.TaskTests.wait_state

    def prepare(self):
        frame = bytes(range(256)) * 256
        capture = patch.object(test_tasks.FakeBrowser, "snapshot", return_value=(frame, []))
        capture.start()
        self.addCleanup(capture.stop)
        task, guest, base, headers = self.new_task()
        runtime = self.manager.runtimes[task["id"]]
        runtime.publish_frame(frame)
        return guest, base, headers, frame

    def channel(self, guest, base, messages):
        class Socket:
            mode = "werkzeug"
            connected = True

            def __init__(self):
                self.sent, self.frame_id, self.binary_count = [], None, 0
                self.messages = iter(messages)

            def receive(self, timeout=None):
                try:
                    value = next(self.messages)
                except StopIteration:
                    raise ConnectionClosed(1000)
                value = value(self) if callable(value) else value
                return None if value is None else json.dumps(value)

            def send(self, value):
                self.sent.append(value)
                if isinstance(value, bytes):
                    self.binary_count += 1
                else:
                    message = json.loads(value)
                    if message["type"] == "frame_start":
                        self.frame_id = message["id"]

            def close(self, reason=None, message=None):
                self.connected = False

        socket = Socket()
        with patch("flask_sock.Server", return_value=socket):
            response = guest.get(base + "/control", headers={"Origin": "http://localhost"},
                                 environ_overrides={"wsgi.url_scheme": "ws"})
        self.assertEqual(response.status_code, 200)
        return socket

    def test_large_frame_does_not_block_input_ack_and_window_is_bounded(self):
        guest, base, headers, frame = self.prepare()
        socket = self.channel(guest, base, [
            {"csrf": headers["X-CSRF-Token"], "frame_chunks": True},
            None,
            {"type": "input", "id": 91, "action": {"type": "key", "key": "ArrowRight"}},
            None, None,
        ])
        binaries = [value for value in socket.sent if isinstance(value, bytes)]
        self.assertEqual([len(value) for value in binaries], [16384, 16384])
        self.assertEqual(b"".join(binaries), frame[:32768])
        messages = [json.loads(value) for value in socket.sent if isinstance(value, str)]
        self.assertIn({"type": "input_ack", "id": 91}, messages)
        ack_index = next(index for index, value in enumerate(socket.sent)
                         if isinstance(value, str) and json.loads(value).get("type") == "input_ack")
        self.assertEqual(sum(isinstance(value, bytes) for value in socket.sent[:ack_index]), 1)

    def test_wrong_and_duplicate_chunk_acks_cannot_expand_window(self):
        guest, base, headers, frame = self.prepare()
        counts = []

        def ack(socket, offset, wrong_id=False):
            counts.append(socket.binary_count)
            return {"type": "frame_ack", "id": socket.frame_id + int(wrong_id), "offset": offset}

        socket = self.channel(guest, base, [
            {"csrf": headers["X-CSRF-Token"], "frame_chunks": True}, None, None,
            lambda socket: ack(socket, 16384, True),
            lambda socket: ack(socket, 1),
            lambda socket: ack(socket, 16384),
            lambda socket: ack(socket, 16384),
            lambda socket: ack(socket, 32768),
        ])
        self.assertEqual(counts, [2, 2, 2, 3, 3])
        self.assertEqual(b"".join(value for value in socket.sent if isinstance(value, bytes)), frame)

    def test_no_new_frame_header_until_previous_frame_is_fully_acked(self):
        guest, base, headers, frame = self.prepare()
        socket = self.channel(guest, base, [
            {"csrf": headers["X-CSRF-Token"], "frame_chunks": True}, None, None,
            lambda socket: {"type": "frame_ack", "id": socket.frame_id, "offset": 16384},
            lambda socket: {"type": "frame_ack", "id": socket.frame_id, "offset": 32768},
            None, None,
        ])
        starts = [json.loads(value) for value in socket.sent if isinstance(value, str)
                  and json.loads(value).get("type") == "frame_start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(b"".join(value for value in socket.sent if isinstance(value, bytes)), frame)

    def test_legacy_client_still_receives_original_jpeg_message(self):
        guest, base, headers, frame = self.prepare()
        socket = self.channel(guest, base, [{"csrf": headers["X-CSRF-Token"]}, None])
        self.assertIn(frame, socket.sent)
        self.assertFalse(any(isinstance(value, str) and json.loads(value).get("type") == "frame_start"
                             for value in socket.sent))


if __name__ == "__main__":
    unittest.main()

"""Control receipts and state must not wait for browser execution."""
from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

from simple_websocket import ConnectionClosed

from tests import test_tasks


class ControlTimingTests(unittest.TestCase):
    setUp = test_tasks.TaskTests.setUp
    tearDown = test_tasks.TaskTests.tearDown
    new_task = test_tasks.TaskTests.new_task
    wait_state = test_tasks.TaskTests.wait_state

    def during_browser_input(self, operation):
        task, guest, base, headers = self.new_task()
        runtime = self.manager.runtimes[task["id"]]
        browser = self.browsers[-1]
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        errors, results = [], []
        original_input = browser.input

        def slow_input(action):
            if action.get("text") == "owned slow input":
                entered.set()
                release.wait(3)
            original_input(action)

        browser.input = slow_input
        runtime.input(self.store.get(task["id"])["controller"],
                      {"type": "text", "text": "owned slow input"})
        self.assertTrue(entered.wait(1))

        def request():
            try:
                results.append(operation(task, guest, base, headers))
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=request, daemon=True)
        thread.start()
        try:
            quick = finished.wait(0.5)
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
        self.assertTrue(quick, "Control request waited for the slow browser operation")
        return results[0]

    def test_websocket_accepted_receipt_during_slow_browser_execution(self):
        def request(task, guest, base, headers):
            class Socket:
                mode = "werkzeug"
                connected = True

                def __init__(self):
                    self.sent = []
                    self.messages = iter([
                        {"csrf": headers["X-CSRF-Token"], "frame_chunks": True},
                        {"type": "input", "id": 77,
                         "action": {"type": "key", "key": "ArrowRight"}},
                    ])

                def receive(self, timeout=None):
                    try:
                        return json.dumps(next(self.messages))
                    except StopIteration:
                        raise ConnectionClosed(1000)

                def send(self, value):
                    self.sent.append(value)

                def close(self, reason=None, message=None):
                    self.connected = False

            socket = Socket()
            with patch("flask_sock.Server", return_value=socket):
                response = guest.get(base + "/control", headers={"Origin": "http://localhost"},
                                     environ_overrides={"wsgi.url_scheme": "ws"})
            self.assertEqual(response.status_code, 200)
            return [json.loads(value) for value in socket.sent if isinstance(value, str)]

        messages = self.during_browser_input(request)
        self.assertIn({"type": "input_ack", "id": 77}, messages)

    def test_owner_state_during_slow_browser_execution(self):
        response = self.during_browser_input(lambda task, guest, base, headers: guest.get(base))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["task"]["mine"])

    def test_admin_state_during_slow_browser_execution(self):
        response = self.during_browser_input(lambda task, guest, base, headers:
            self.admin.get(f"/api/admin/tasks/{task['id']}/browser"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["task"]["can_control"])


if __name__ == "__main__":
    unittest.main()

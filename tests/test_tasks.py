from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import json

from playwright.sync_api import TimeoutError as ScreenshotTimeout

from taskapp import create_app, task_values
from taskapp.browser import AdsPower
from taskapp.store import Store
from taskapp.tasks import TaskConflict, TaskRuntime


VALUES = {"title": "测试任务", "start_url": "https://example.com", "lifetime_minutes": 60}


class FakeAds:
    def __init__(self):
        self.created = []
        self.created_tasks = []
        self.started = []
        self.stopped = []
        self.deleted = []

    def create(self, task):
        self.created.append(task["code"])
        self.created_tasks.append(task)
        return "profile-" + task["code"]

    def start(self, profile, restore_tabs=False):
        self.started.append((profile, restore_tabs))
        return "ws://127.0.0.1/mock"

    def stop(self, profile):
        self.stopped.append(profile)

    def delete(self, profile):
        self.deleted.append(profile)


class FakeBrowser:
    def __init__(self, endpoint, url):
        self.inputs = []
        self.checked = []
        self.closed = False

    def input(self, action):
        self.inputs.append(action)

    def snapshot(self):
        return b"frame", [{"index": 0, "url": "https://example.com", "active": True}]

    def content(self, url, kind):
        self.checked.append((url, kind))
        raise AssertionError("Verification must not run")

    def close(self):
        self.closed = True


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.ads = FakeAds()
        self.browsers = []

        def factory(endpoint, url):
            browser = FakeBrowser(endpoint, url)
            self.browsers.append(browser)
            return browser

        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret",
                               "ADMIN_PASSWORD": "test-admin",
                               "DATABASE": str(Path(self.directory.name) / "tasks.db")},
                              ads=self.ads, browser_factory=factory)
        self.admin = self.app.test_client()
        self.admin.get("/")
        with self.admin.session_transaction() as session:
            self.admin_csrf = session["csrf"]
        self.admin.post("/api/login", json={"password": "test-admin"},
                        headers={"X-CSRF-Token": self.admin_csrf})
        self.manager = self.app.extensions["task_manager"]
        self.store = self.app.extensions["task_store"]

    def tearDown(self):
        for runtime in self.manager.runtimes.values():
            runtime.cancel()
            runtime.thread.join(6)
        self.directory.cleanup()

    def wait_state(self, task_id, states):
        for _ in range(150):
            task = self.store.get(task_id)
            if task["state"] in states:
                return task
            time.sleep(0.02)
        self.fail(f"Task never reached {states}: {self.store.get(task_id)}")

    def new_task(self):
        response = self.admin.post("/api/admin/tasks", json=VALUES,
                                   headers={"X-CSRF-Token": self.admin_csrf})
        self.assertEqual(response.status_code, 201)
        task = response.json["task"]
        task = self.wait_state(task["id"], {"active"})
        client = self.app.test_client()
        client.get("/t/" + task["token"])
        with client.session_transaction() as session:
            csrf = session["csrf"]
        base = "/api/task/" + task["token"]
        self.assertEqual(client.post(base + "/claim", json={},
                                    headers={"X-CSRF-Token": csrf}).status_code, 200)
        return task, client, base, {"X-CSRF-Token": csrf}

    def test_submit_locks_inputs_frames_reconnect_and_duplicate_submission(self):
        task, client, base, headers = self.new_task()
        browser = self.browsers[0]
        self.assertEqual(client.post(base + "/input", json={"type": "text", "text": "hello"},
                                     headers=headers).status_code, 200)
        self.assertEqual(client.post(base + "/submit", json={}, headers=headers).status_code, 200)
        self.assertEqual(self.store.get(task["id"])["state"], "completed")
        for endpoint in ("input", "claim", "submit"):
            response = client.post(base + "/" + endpoint,
                                   json={"type": "text", "text": "blocked"}, headers=headers)
            self.assertEqual(response.status_code, 409)
        self.assertEqual(client.get(base + "/frame").status_code, 409)
        client.get("/t/" + task["token"])
        self.assertEqual(client.post(base + "/claim", json={}, headers=headers).status_code, 409)
        self.assertNotIn({"type": "text", "text": "blocked"}, browser.inputs)
        result = self.wait_state(task["id"], {"completed"})
        self.assertIsNotNone(result["checked_at"])
        self.assertEqual(browser.checked, [])
        self.manager.runtimes[task["id"]].thread.join(2)
        self.assertTrue(browser.closed)
        self.assertIn(result["profile_id"], self.ads.stopped)

    def test_snapshot_failure_logs_phase_before_cleanup_without_exception_secrets(self):
        task, client, base, headers = self.new_task()
        secret = "https://private.test/?token=private-token"

        def fail_snapshot():
            raise RuntimeError(secret)

        with self.assertLogs(level="INFO") as logs:
            self.browsers[0].snapshot = fail_snapshot
            self.wait_state(task["id"], {"error"})
            self.manager.runtimes[task["id"]].thread.join(2)
        output = "\n".join(logs.output)
        self.assertIn("phase=snapshot", output)
        self.assertIn("event=profile_stop_requested", output)
        self.assertLess(output.index("phase=snapshot"), output.index("event=profile_stop_requested"))
        self.assertNotIn(secret, output)
        self.assertEqual(client.post(base + "/input", json={"type": "text", "text": "blocked"},
                                     headers=headers).status_code, 409)

    def test_snapshot_timeout_keeps_task_and_last_frame_then_recovers(self):
        task, client, base, headers = self.new_task()
        runtime = self.manager.runtimes[task["id"]]
        deadline = time.monotonic() + 2
        while not runtime.frame and time.monotonic() < deadline:
            time.sleep(0.02)
        failed = threading.Event()

        def timeout():
            failed.set()
            raise ScreenshotTimeout("private page must not be logged")

        self.browsers[0].snapshot = timeout
        self.assertTrue(failed.wait(2))
        time.sleep(0.1)
        self.assertEqual(self.store.get(task["id"])["state"], "active")
        self.assertEqual(runtime.frame, b"frame")
        self.assertEqual(self.ads.stopped, [])
        self.assertEqual(client.post(base + "/input", json={"type": "text", "text": "still works"},
                                    headers=headers).status_code, 200)
        self.browsers[0].snapshot = lambda: (b"recovered", [])
        deadline = time.monotonic() + 2
        while runtime.frame != b"recovered" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(runtime.frame, b"recovered")

    def test_slow_capture_does_not_block_completion_or_publish_after_lock(self):
        task, client, base, headers = self.new_task()
        entered, release = threading.Event(), threading.Event()

        def slow_snapshot():
            entered.set()
            release.wait(3)
            return b"late-private-frame", []

        self.browsers[0].snapshot = slow_snapshot
        self.assertTrue(entered.wait(2))
        try:
            started = time.monotonic()
            response = client.post(base + "/submit", json={}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertLess(time.monotonic() - started, 0.3)
        finally:
            release.set()
        self.manager.runtimes[task["id"]].thread.join(2)
        self.assertEqual(self.manager.runtimes[task["id"]].frame, b"")

    def test_guest_stream_revoked_on_completion_and_admin_detach_keeps_guest(self):
        task, guest, base, headers = self.new_task()
        admin_base = f"/api/admin/tasks/{task['id']}/browser"
        self.admin.post(admin_base + "/open", json={}, headers={"X-CSRF-Token": self.admin_csrf})
        response = guest.get(base + "/stream", buffered=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Content-Type: image/jpeg", next(response.response))
        self.admin.post(admin_base + "/close", json={}, headers={"X-CSRF-Token": self.admin_csrf})
        self.assertIn(b"frame", next(response.response))
        guest.post(base + "/submit", json={}, headers=headers)
        self.assertEqual(list(response.response), [])
        response.close()
        self.assertEqual(guest.get(base + "/stream").status_code, 409)

    def test_admin_logout_revokes_existing_stream(self):
        task, guest, base, headers = self.new_task()
        admin_base = f"/api/admin/tasks/{task['id']}/browser"
        self.admin.post(admin_base + "/open", json={}, headers={"X-CSRF-Token": self.admin_csrf})
        self.assertEqual(guest.get(admin_base + "/stream").status_code, 401)
        response = self.admin.get(admin_base + "/stream", buffered=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"frame", next(response.response))
        self.admin.post("/api/logout", json={}, headers={"X-CSRF-Token": self.admin_csrf})
        self.assertEqual(list(response.response), [])
        response.close()

    def test_stream_permissions_include_other_sessions_expiry_and_admin_close(self):
        task, guest, base, headers = self.new_task()
        other = self.app.test_client()
        other.get("/t/" + task["token"])
        self.assertEqual(other.get(base + "/stream").status_code, 409)
        admin_base = f"/api/admin/tasks/{task['id']}/browser"
        self.admin.post(admin_base + "/open", json={}, headers={"X-CSRF-Token": self.admin_csrf})
        admin_stream = self.admin.get(admin_base + "/stream", buffered=False)
        next(admin_stream.response)
        self.admin.post(admin_base + "/close", json={}, headers={"X-CSRF-Token": self.admin_csrf})
        self.assertEqual(list(admin_stream.response), [])
        admin_stream.close()
        guest_stream = guest.get(base + "/stream", buffered=False)
        next(guest_stream.response)
        self.store.update(task["id"], expires_at=time.time() - 1)
        self.assertEqual(list(guest_stream.response), [])
        guest_stream.close()

    def control(self, client, base, messages, origin="http://localhost"):
        from simple_websocket import ConnectionClosed

        class Socket:
            mode = "werkzeug"
            connected = True

            def __init__(self):
                self.sent, self.close_reason = [], None
                self.messages = list(messages)

            def receive(self, timeout=None):
                if not self.messages:
                    raise ConnectionClosed(1000)
                value = self.messages.pop(0)
                return value() if callable(value) else json.dumps(value)

            def send(self, value):
                self.sent.append(value)

            def close(self, reason=None, message=None):
                self.connected = False
                if self.close_reason is None:
                    self.close_reason = reason

        socket = Socket()
        with patch("flask_sock.Server", return_value=socket):
            response = client.get(base + "/control", headers={"Origin": origin},
                                  environ_overrides={"wsgi.url_scheme": "ws"})
        return response, socket

    def test_realtime_channel_requires_origin_owner_and_csrf(self):
        task, guest, base, headers = self.new_task()
        auth = {"csrf": headers["X-CSRF-Token"]}
        response, socket = self.control(guest, base, [auth], "https://other.test")
        self.assertEqual(response.status_code, 403)
        other = self.app.test_client()
        other.get("/t/" + task["token"])
        response, socket = self.control(other, base, [auth])
        self.assertEqual(response.status_code, 409)
        response, socket = self.control(guest, base, [{"csrf": "wrong"}])
        self.assertEqual(socket.close_reason, 1008)
        self.assertFalse(any(isinstance(value, bytes) for value in socket.sent))

    def test_close_tab_requires_valid_identity_and_existing_control_permission(self):
        task, guest, base, headers = self.new_task()
        action = {"type": "close_tab", "id": 1}
        self.assertEqual(guest.post(base + "/input", json=action, headers=headers).status_code, 200)
        for value in (0, -1, True, "1", None):
            self.assertEqual(guest.post(base + "/input", json={"type": "close_tab", "id": value},
                                        headers=headers).status_code, 400)
        admin_base = f"/api/admin/tasks/{task['id']}/browser"
        admin_headers = {"X-CSRF-Token": self.admin_csrf}
        self.admin.post(admin_base + "/open", json={}, headers=admin_headers)
        self.assertEqual(self.admin.post(admin_base + "/input", json=action,
                                        headers=admin_headers).status_code, 409)
        guest.post(base + "/submit", json={}, headers=headers)
        self.assertEqual(guest.post(base + "/input", json=action, headers=headers).status_code, 409)
        self.assertEqual(self.admin.post(admin_base + "/input", json=action,
                                        headers=admin_headers).status_code, 200)

    def test_explicit_close_of_all_web_tabs_erases_only_saved_page_list(self):
        task, guest, base, headers = self.new_task()
        runtime = self.manager.runtimes[task["id"]]
        self.browsers[0].snapshot = lambda: (b"frame", [{"url": "about:blank", "active": True}])
        with runtime.gate:
            runtime.save_tabs([{"url": "https://example.com/closed", "active": True}])
            runtime.save_tabs([{"url": "about:blank", "active": True}], allow_empty=True)
        self.assertEqual(self.store.get(task["id"])["browser_tabs"], "[]")
        self.assertEqual(self.store.get(task["id"])["profile_id"], task["profile_id"])

    def test_realtime_input_order_and_frame_backpressure(self):
        task, guest, base, headers = self.new_task()
        runtime = self.manager.runtimes[task["id"]]
        runtime.publish_frame(b"initial-jpeg")
        actions = [{"type": "text", "text": "a"}, {"type": "key", "key": "Backspace"},
                   {"type": "new_tab"}]
        # Freeze the fake capture source while checking unchanged-frame deduplication.
        with patch.object(runtime, "publish_frame"):
            response, socket = self.control(guest, base, [{"csrf": headers["X-CSRF-Token"]}]
                                            + [{"type": "input", "action": action} for action in actions])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sum(isinstance(value, bytes) for value in socket.sent), 1)
        deadline = time.monotonic() + 1
        while len(self.browsers[0].inputs) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.browsers[0].inputs, actions)

    def test_completion_revokes_open_realtime_channel_before_more_input(self):
        task, guest, base, headers = self.new_task()
        runtime = self.manager.runtimes[task["id"]]

        def complete_then_input():
            runtime.submit(self.store.get(task["id"])["controller"])
            return json.dumps({"type": "input", "action": {"type": "text", "text": "blocked"}})

        response, socket = self.control(guest, base, [{"csrf": headers["X-CSRF-Token"]}, complete_then_input])
        self.assertEqual(self.store.get(task["id"])["state"], "completed")
        self.assertNotIn({"type": "text", "text": "blocked"}, self.browsers[0].inputs)
        self.assertEqual(socket.close_reason, 1008)

    def test_realtime_old_generation_is_rejected_after_reopen(self):
        task, guest, base, headers = self.new_task()
        runtime = self.manager.runtimes[task["id"]]
        runtime.attach_admin()

        def reopen_then_input():
            runtime.submit(self.store.get(task["id"])["controller"])
            runtime.reopen_user()
            return json.dumps({"type": "input", "action": {"type": "text", "text": "stale"}})

        response, socket = self.control(guest, base, [{"csrf": headers["X-CSRF-Token"]}, reopen_then_input])
        self.assertEqual(self.store.get(task["id"])["state"], "active")
        self.assertEqual(self.browsers[0].inputs, [])
        self.assertEqual(socket.close_reason, 1008)

    def test_realtime_frame_window_is_bounded_and_ack_releases_capacity(self):
        original_publish = TaskRuntime.publish_frame

        def manual_publish(runtime, value):
            # This window test owns its frames, including before first capture.
            if threading.get_ident() != runtime.thread.ident:
                original_publish(runtime, value)

        with patch.object(TaskRuntime, "publish_frame", manual_publish):
            task, guest, base, headers = self.new_task()
            runtime = self.manager.runtimes[task["id"]]

            def frame(index):
                def publish():
                    runtime.publish_frame(str(index).encode())
                    return None
                return publish

            messages = [{"csrf": headers["X-CSRF-Token"]}] + [frame(i) for i in range(8)]
            messages.append({"type": "frame_ack"})
            response, socket = self.control(guest, base, messages)
            frames = [value for value in socket.sent if isinstance(value, bytes)]
            self.assertEqual(frames, [b"0", b"1", b"2", b"3", b"7"])

    def test_realtime_invalid_action_is_rejected_without_closing_valid_session(self):
        task, guest, base, headers = self.new_task()
        response, socket = self.control(guest, base, [{"csrf": headers["X-CSRF-Token"]},
                                                     {"type": "input", "action": []}])
        self.assertTrue(any('"error"' in value for value in socket.sent if isinstance(value, str)))
        self.assertEqual(self.browsers[0].inputs, [])
        response, socket = self.control(guest, base, [{"csrf": headers["X-CSRF-Token"]}])
        self.assertEqual(response.status_code, 200)

    def test_admin_realtime_observer_cannot_input_and_logout_revokes_connection(self):
        task, guest, base, headers = self.new_task()
        admin_base = f"/api/admin/tasks/{task['id']}/browser"
        self.admin.post(admin_base + "/open", json={}, headers={"X-CSRF-Token": self.admin_csrf})

        def logout_then_input():
            self.admin.post("/api/logout", json={}, headers={"X-CSRF-Token": self.admin_csrf})
            return json.dumps({"type": "input", "action": {"type": "text", "text": "after logout"}})

        response, socket = self.control(self.admin, admin_base,
                                        [{"csrf": self.admin_csrf},
                                         {"type": "input", "action": {"type": "text", "text": "observer"}},
                                         logout_then_input])
        self.assertEqual(self.browsers[0].inputs, [])
        self.assertTrue(any('"error"' in value for value in socket.sent if isinstance(value, str)))
        self.assertEqual(socket.close_reason, 1008)

    def test_old_stream_cannot_resume_after_reopen_and_stream_slots_are_released(self):
        task, guest, base, headers = self.new_task()
        admin_base = f"/api/admin/tasks/{task['id']}/browser"
        self.admin.post(admin_base + "/open", json={}, headers={"X-CSRF-Token": self.admin_csrf})
        streams = [guest.get(base + "/stream", buffered=False) for _ in range(8)]
        try:
            self.assertTrue(all(response.status_code == 200 for response in streams))
            for response in streams:
                next(response.response)  # Consume the already-buffered first frame.
            self.assertEqual(guest.get(base + "/stream").status_code, 429)
            guest.post(base + "/submit", json={}, headers=headers)
            self.admin.post(f"/api/admin/tasks/{task['id']}/reopen", json={},
                            headers={"X-CSRF-Token": self.admin_csrf})
            self.assertEqual(list(streams[0].response), [])
        finally:
            for response in streams:
                response.close()
        response = guest.get(base + "/stream", buffered=False)
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_admin_snapshot_failure_preserves_result_and_logs_admin_mode(self):
        task, guest, base, headers = self.new_task()
        admin_base = f"/api/admin/tasks/{task['id']}/browser"
        self.admin.post(admin_base + "/open", json={}, headers={"X-CSRF-Token": self.admin_csrf})
        guest.post(base + "/submit", json={}, headers=headers)
        result = self.wait_state(task["id"], {"completed"})

        def fail_snapshot():
            raise RuntimeError("Target closed")

        with self.assertLogs(level="INFO") as logs:
            self.browsers[0].snapshot = fail_snapshot
            self.manager.runtimes[task["id"]].thread.join(3)
        self.assertFalse(self.manager.runtimes[task["id"]].thread.is_alive())
        self.assertIn("admin_attached=True", "\n".join(logs.output))
        for field in ("state", "message", "checked_at", "profile_id"):
            self.assertEqual(self.store.get(task["id"])[field], result[field])

    def test_another_session_cannot_control_claimed_task(self):
        task, client, base, headers = self.new_task()
        other = self.app.test_client()
        other.get("/t/" + task["token"])
        with other.session_transaction() as session:
            other_headers = {"X-CSRF-Token": session["csrf"]}
        self.assertEqual(other.post(base + "/claim", json={}, headers=other_headers).status_code, 409)
        self.assertEqual(other.get(base + "/frame").status_code, 409)
        public = other.get(base).json["task"]
        self.assertNotIn("pattern", public)
        self.assertNotIn("profile_id", public)
        self.assertNotIn("tabs", public)

    def test_missing_admin_auth_and_csrf_are_rejected(self):
        other = self.app.test_client()
        self.assertEqual(other.get("/api/admin/tasks").status_code, 401)
        self.assertEqual(self.admin.post("/api/admin/tasks", json=VALUES).status_code, 403)

    def test_invalid_task_does_not_create_profile(self):
        for overrides in ({"title": ""}, {"start_url": "file:///etc/passwd"},
                          {"title": 123}, {"lifetime_minutes": None}):
            response = self.admin.post("/api/admin/tasks", json={**VALUES, **overrides},
                                       headers={"X-CSRF-Token": self.admin_csrf})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.ads.created, [])

    def test_profile_uses_installed_mac_desktop_kernel(self):
        ads = AdsPower("http://127.0.0.1:50325", "test-key")
        requests = []

        def call(path, payload):
            requests.append(payload)
            return {"profile_id": "test-profile"}

        ads.call = call
        ads.create({"code": "TEST", "start_url": "https://example.com"})
        fingerprint = requests[0]["fingerprint_config"]
        self.assertEqual(fingerprint["browser_kernel_config"],
                         {"type": "chrome", "version": "152"})
        self.assertEqual(fingerprint["random_ua"],
                         {"ua_system_version": ["Mac OS X"], "ua_version": ["152"]})
        self.assertNotIn("ua", fingerprint)

    def test_proxy_reaches_adspower_without_exposing_credentials(self):
        values = {**VALUES, "proxy": "socks5://test-proxy-user:test-proxy-password@127.0.0.1:1080"}
        response = self.admin.post("/api/admin/tasks", json=values,
                                   headers={"X-CSRF-Token": self.admin_csrf})
        self.assertEqual(response.status_code, 201)
        task = response.json["task"]
        self.wait_state(task["id"], {"active"})
        proxy = self.ads.created_tasks[0]["user_proxy_config"]
        self.assertEqual(proxy, {"proxy_soft": "other", "proxy_type": "socks5",
                                 "proxy_host": "127.0.0.1", "proxy_port": "1080",
                                 "proxy_user": "test-proxy-user",
                                 "proxy_password": "test-proxy-password"})
        self.assertEqual(task["proxy_label"], "socks5://127.0.0.1:1080")
        for body in (response.get_data(as_text=True),
                     self.admin.get("/api/admin/tasks").get_data(as_text=True),
                     str(self.store.get(task["id"]))):
            self.assertNotIn("test-proxy-password", body)
            self.assertNotIn("test-proxy-user", body)
        self.assertIsNone(self.manager.runtimes[task["id"]].proxy_config)
        ads = AdsPower("http://127.0.0.1:50325", "test-key")
        payloads = []
        ads.call = lambda path, payload: (payloads.append(payload)
                                         or {"profile_id": "test-profile"})
        ads.create(self.ads.created_tasks[0])
        self.assertEqual(payloads[0]["user_proxy_config"], proxy)

    def test_proxy_validation_and_direct_connection(self):
        valid = {"proxy_type": "http", "proxy_host": "proxy.example.com", "proxy_port": "8080"}
        for overrides in ({"proxy_type": "ftp"}, {"proxy_type": {}},
                          {"proxy_host": ""}, {"proxy_host": "http://proxy.example.com"},
                          {"proxy_host": "proxy.example.com:8080"}, {"proxy_port": "0"},
                          {"proxy_port": "65536"}, {"proxy_port": 80.5},
                          {"proxy_password": "password"}, {"proxy_user": "bad\nvalue"}):
            response = self.admin.post("/api/admin/tasks", json={**VALUES, **valid, **overrides},
                                       headers={"X-CSRF-Token": self.admin_csrf})
            self.assertEqual(response.status_code, 400, overrides)
        self.assertEqual(self.ads.created, [])
        self.assertEqual(task_values(VALUES)["user_proxy_config"], {"proxy_soft": "no_proxy"})
        for kind in ("http", "https", "socks5"):
            self.assertEqual(task_values({**VALUES, **valid, "proxy_type": kind})[
                "user_proxy_config"]["proxy_type"], kind)
        self.assertEqual(task_values({**VALUES, **valid, "proxy_host": "::1"})[
            "user_proxy_config"]["proxy_host"], "::1")

    def test_existing_database_migrates_without_losing_records(self):
        task = self.store.create(VALUES)
        with self.store.connect() as db:
            db.execute("ALTER TABLE tasks DROP COLUMN proxy_label")
        migrated = Store(self.store.path)
        self.assertEqual(migrated.get(task["id"])["title"], VALUES["title"])
        self.assertEqual(migrated.get(task["id"])["proxy_label"], "")

    def test_single_line_proxy_accepts_common_pasted_values(self):
        for text, kind, host, port, user, password in (
                ("127.0.0.1:8080", "http", "127.0.0.1", "8080", "", ""),
                ("http://127.0.0.1:8080/", "http", "127.0.0.1", "8080", "", ""),
                ("proxy.example.com:8080:user:pass:word", "http", "proxy.example.com", "8080", "user", "pass:word"),
                ("proxy.example.com:8080:user:pass@word", "http", "proxy.example.com", "8080", "user", "pass@word"),
                ("user:pass@proxy.example.com:8080", "http", "proxy.example.com", "8080", "user", "pass"),
                ("https://u:p@proxy.example.com:443", "https", "proxy.example.com", "443", "u", "p"),
                ("socks5://proxy.example.com:1080:u:p", "socks5", "proxy.example.com", "1080", "u", "p"),
                ("socks5://u%40name:p%3Aword%23@proxy.example.com:1080", "socks5", "proxy.example.com", "1080", "u@name", "p:word#"),
                ("127.0.0.1 8080 user password", "http", "127.0.0.1", "8080", "user", "password"),
                ("127.0.0.1,8080,user,password", "http", "127.0.0.1", "8080", "user", "password"),
                ("socks5://[::1]:1080", "socks5", "::1", "1080", "", "")):
            with self.subTest(text=text):
                proxy = task_values({**VALUES, "proxy": text})["user_proxy_config"]
                self.assertEqual(proxy, {"proxy_soft": "other", "proxy_type": kind,
                                         "proxy_host": host, "proxy_port": port,
                                         "proxy_user": user, "proxy_password": password})
        self.assertEqual(task_values({**VALUES, "proxy": "  "})["user_proxy_config"],
                         {"proxy_soft": "no_proxy"})

    def test_single_line_proxy_rejects_bad_values_without_creating_profiles(self):
        for text in (None, [], "incomplete", "127.0.0.1", "127.0.0.1:0", "host:65536",
                     "host:8080:username", "ftp://host:8080", "http://u:secret@host",
                     "http://u:secret@host:8080/path", "host:8080\nother:8080", "x" * 2049):
            response = self.admin.post("/api/admin/tasks", json={**VALUES, "proxy": text},
                                       headers={"X-CSRF-Token": self.admin_csrf})
            self.assertEqual(response.status_code, 400, text)
            self.assertNotIn("secret", response.get_data(as_text=True))
        self.assertEqual(self.ads.created, [])

    def test_admin_proxy_form_is_one_input(self):
        html = self.admin.get("/").get_data(as_text=True)
        self.assertIn('name="proxy"', html)
        for old_field in ("proxy_type", "proxy_host", "proxy_port", "proxy_user", "proxy_password"):
            self.assertNotIn(f'name="{old_field}"', html)

    def test_expiry_revokes_controller(self):
        task, client, base, headers = self.new_task()
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET expires_at=? WHERE id=?", (time.time() - 1, task["id"]))
        self.assertEqual(client.post(base + "/input", json={"type": "text", "text": "x"},
                                     headers=headers).status_code, 409)
        self.wait_state(task["id"], {"expired"})

    def test_cancel_discards_pending_input_and_keeps_task_locked(self):
        task, client, base, headers = self.new_task()
        runtime = self.manager.runtimes[task["id"]]
        with runtime.gate:
            client.post(base + "/input", json={"type": "text", "text": "pending"}, headers=headers)
            self.admin.post(f"/api/admin/tasks/{task['id']}/cancel", json={},
                            headers={"X-CSRF-Token": self.admin_csrf})
        runtime.thread.join(2)
        self.assertNotIn({"type": "text", "text": "pending"}, self.browsers[0].inputs)
        self.assertEqual(self.store.get(task["id"])["state"], "cancelled")

    def test_submit_discards_queued_actions(self):
        task = self.store.create(VALUES)
        self.store.update(task["id"], state="active", controller="owner")
        runtime = TaskRuntime(task["id"], self.store, self.ads)
        runtime.input("owner", {"type": "text", "text": "pending"})
        runtime.submit("owner")
        self.assertTrue(runtime.commands.empty())
        with self.assertRaises(TaskConflict):
            runtime.input("owner", {"type": "text", "text": "late"})

    def test_recovery_locks_interrupted_tasks(self):
        task = self.store.create(VALUES)
        self.store.update(task["id"], state="verifying", profile_id="orphan")
        self.assertEqual(self.store.recover(), ["orphan"])
        self.assertEqual(self.store.get(task["id"])["state"], "error")

    def test_only_finished_profiles_can_be_released_and_records_survive(self):
        task, client, base, headers = self.new_task()
        path = f"/api/admin/tasks/{task['id']}/release"
        response = self.admin.post(path, json={}, headers={"X-CSRF-Token": self.admin_csrf})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.ads.deleted, [])
        client.post(base + "/submit", json={}, headers=headers)
        self.wait_state(task["id"], {"completed"})
        self.manager.runtimes[task["id"]].thread.join(2)
        response = self.admin.post(path, json={}, headers={"X-CSRF-Token": self.admin_csrf})
        self.assertEqual(response.status_code, 200)
        result = self.store.get(task["id"])
        self.assertEqual(result["state"], "completed")
        self.assertIsNone(result["profile_id"])
        self.assertEqual(len(self.ads.deleted), 1)

    def test_delete_requires_auth_csrf_confirmation_and_finished_task(self):
        task, client, base, headers = self.new_task()
        path = f"/api/admin/tasks/{task['id']}/delete"
        admin_headers = {"X-CSRF-Token": self.admin_csrf}
        self.assertEqual(client.post(path, json={"confirm": True}, headers=headers).status_code, 401)
        self.assertEqual(self.admin.post(path, json={"confirm": True}).status_code, 403)
        self.assertEqual(self.admin.post(path, json={}, headers=admin_headers).status_code, 400)
        self.assertEqual(self.admin.post(path, json={"confirm": True},
                                        headers=admin_headers).status_code, 409)
        self.assertEqual(self.ads.deleted, [])
        client.post(base + "/submit", json={}, headers=headers)
        self.wait_state(task["id"], {"completed"})
        self.manager.runtimes[task["id"]].thread.join(2)
        self.assertEqual(self.admin.post(path, json={"confirm": True},
                                        headers=admin_headers).status_code, 200)
        self.assertIsNone(self.store.get(task["id"]))
        self.assertEqual(client.get("/t/" + task["token"]).status_code, 404)
        self.assertEqual(client.get(base).status_code, 404)
        self.assertEqual(self.ads.deleted, [task["profile_id"]])
        self.assertEqual(self.admin.post(path, json={"confirm": True},
                                        headers=admin_headers).status_code, 404)

    def test_delete_already_released_task_and_preserve_record_on_api_failure(self):
        task, client, base, headers = self.new_task()
        client.post(base + "/submit", json={}, headers=headers)
        self.wait_state(task["id"], {"completed"})
        self.manager.runtimes[task["id"]].thread.join(2)
        admin_headers = {"X-CSRF-Token": self.admin_csrf}
        path = f"/api/admin/tasks/{task['id']}/delete"
        original_delete = self.ads.delete

        def fail_delete(profile):
            raise RuntimeError("API unavailable")

        self.ads.delete = fail_delete
        self.assertEqual(self.admin.post(path, json={"confirm": True},
                                        headers=admin_headers).status_code, 502)
        self.assertEqual(self.store.get(task["id"])["profile_id"], task["profile_id"])
        self.ads.delete = original_delete
        self.admin.post(f"/api/admin/tasks/{task['id']}/release", json={}, headers=admin_headers)
        self.assertEqual(self.admin.post(path, json={"confirm": True},
                                        headers=admin_headers).status_code, 200)
        self.assertEqual(len(self.ads.deleted), 1)

    def test_delete_waits_for_browser_cleanup(self):
        task, client, base, headers = self.new_task()
        closing, release = threading.Event(), threading.Event()
        def slow_close():
            closing.set()
            release.wait(5)
        self.browsers[0].close = slow_close
        client.post(base + "/submit", json={}, headers=headers)
        try:
            self.assertTrue(closing.wait(2))
            response = self.admin.post(f"/api/admin/tasks/{task['id']}/delete",
                                       json={"confirm": True},
                                       headers={"X-CSRF-Token": self.admin_csrf})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.ads.deleted, [])
        finally:
            release.set()

    def wait_admin_ready(self, task_id):
        for _ in range(150):
            response = self.admin.get(f"/api/admin/tasks/{task_id}/browser")
            self.assertEqual(response.status_code, 200)
            if (response.json["task"]["browser_ready"] and self.admin.get(
                    f"/api/admin/tasks/{task_id}/browser/frame").status_code == 200):
                return response.json["task"]
            time.sleep(0.02)
        self.fail("Admin browser did not become ready")

    def test_admin_watches_then_controls_same_browser_and_guest_stays_locked(self):
        task, guest, base, headers = self.new_task()
        admin_base = f"/api/admin/tasks/{task['id']}/browser"
        admin_headers = {"X-CSRF-Token": self.admin_csrf}
        original_controller = self.store.get(task["id"])["controller"]
        self.assertEqual(self.admin.post(admin_base + "/open", json={},
                                        headers=admin_headers).status_code, 202)
        view = self.wait_admin_ready(task["id"])
        self.assertFalse(view["can_control"])
        self.assertEqual(self.admin.get(admin_base + "/frame").status_code, 200)
        self.assertEqual(self.admin.post(admin_base + "/input", json={"type": "text", "text": "admin"},
                                        headers=admin_headers).status_code, 409)
        self.assertEqual(self.store.get(task["id"])["controller"], original_controller)
        browser = self.browsers[0]
        guest.post(base + "/submit", json={}, headers=headers)
        result = self.wait_state(task["id"], {"completed"})
        self.assertTrue(self.wait_admin_ready(task["id"])["can_control"])
        self.assertEqual(self.admin.post(admin_base + "/input", json={"type": "text", "text": "admin"},
                                        headers=admin_headers).status_code, 200)
        for _ in range(100):
            if {"type": "text", "text": "admin"} in browser.inputs:
                break
            time.sleep(0.02)
        self.assertIn({"type": "text", "text": "admin"}, browser.inputs)
        self.assertEqual(len(self.browsers), 1)
        self.assertFalse(browser.closed)
        self.assertEqual(len(self.ads.created), 1)
        for endpoint in ("claim", "input", "submit"):
            self.assertEqual(guest.post(base + "/" + endpoint, json={"type": "text", "text": "guest"},
                                        headers=headers).status_code, 409)
        self.assertEqual(guest.get(base + "/frame").status_code, 409)
        stranger = self.app.test_client()
        stranger.get("/t/" + task["token"])
        with stranger.session_transaction() as session:
            other_headers = {"X-CSRF-Token": session["csrf"]}
        self.assertEqual(stranger.post(base + "/claim", json={}, headers=other_headers).status_code, 409)
        for field in ("state", "message", "checked_at", "profile_id"):
            self.assertEqual(self.store.get(task["id"])[field], result[field])

    def test_admin_browser_requires_login_csrf_and_survives_logout_without_guest_access(self):
        task, guest, base, headers = self.new_task()
        admin_base = f"/api/admin/tasks/{task['id']}/browser"
        self.assertEqual(guest.get(f"/admin/tasks/{task['id']}/browser").status_code, 401)
        for path in (admin_base, admin_base + "/frame"):
            self.assertEqual(guest.get(path).status_code, 401)
        for endpoint in ("open", "input", "close"):
            self.assertEqual(guest.post(admin_base + "/" + endpoint, json={}, headers=headers).status_code, 401)
            self.assertEqual(self.admin.post(admin_base + "/" + endpoint, json={}).status_code, 403)
        admin_headers = {"X-CSRF-Token": self.admin_csrf}
        self.assertEqual(self.admin.get(f"/admin/tasks/{task['id']}/browser").status_code, 200)
        self.assertEqual(self.admin.post(admin_base + "/open", json={}, headers=admin_headers).status_code, 202)
        self.admin.post("/api/logout", json={}, headers=admin_headers)
        self.assertEqual(self.admin.get(admin_base + "/frame").status_code, 401)
        self.assertEqual(guest.get(admin_base + "/frame").status_code, 401)
        self.assertEqual(guest.post(base + "/input", json={"type": "text", "text": "guest"},
                                   headers=headers).status_code, 200)

    def test_admin_reopens_original_finished_profile_without_changing_result(self):
        task, guest, base, headers = self.new_task()
        guest.post(base + "/submit", json={}, headers=headers)
        result = self.wait_state(task["id"], {"completed"})
        self.manager.runtimes[task["id"]].thread.join(2)
        admin_base = f"/api/admin/tasks/{task['id']}/browser"
        admin_headers = {"X-CSRF-Token": self.admin_csrf}
        for _ in range(2):
            self.assertEqual(self.admin.post(admin_base + "/open", json={}, headers=admin_headers).status_code, 202)
        self.assertTrue(self.wait_admin_ready(task["id"])["can_control"])
        self.assertEqual(len(self.ads.created), 1)
        self.assertEqual(self.ads.started, [(task["profile_id"], False), (task["profile_id"], True)])
        self.assertEqual(len(self.browsers), 2)
        self.assertEqual(self.admin.post(f"/api/admin/tasks/{task['id']}/release", json={},
                                        headers=admin_headers).status_code, 409)
        self.assertEqual(self.admin.post(f"/api/admin/tasks/{task['id']}/delete", json={"confirm": True},
                                        headers=admin_headers).status_code, 409)
        self.assertEqual(guest.post(base + "/input", json={"type": "text", "text": "guest"},
                                   headers=headers).status_code, 409)
        self.admin.post(admin_base + "/close", json={}, headers=admin_headers)
        self.manager.runtimes[task["id"]].thread.join(2)
        for field in ("state", "message", "checked_at", "profile_id"):
            self.assertEqual(self.store.get(task["id"])[field], result[field])
        self.assertEqual(self.store.get(task["id"])["admin_open"], 0)
        self.admin.post(f"/api/admin/tasks/{task['id']}/release", json={}, headers=admin_headers)
        self.assertEqual(self.admin.post(admin_base + "/open", json={}, headers=admin_headers).status_code, 409)

    def test_closing_admin_observer_does_not_close_user_browser(self):
        task, guest, base, headers = self.new_task()
        admin_base = f"/api/admin/tasks/{task['id']}/browser"
        admin_headers = {"X-CSRF-Token": self.admin_csrf}
        self.admin.post(admin_base + "/open", json={}, headers=admin_headers)
        runtime = self.manager.runtimes[task["id"]]
        original = self.store.get(task["id"])
        pending = {"type": "text", "text": "queued-before-observer-exit"}
        with runtime.gate:
            self.assertEqual(guest.post(base + "/input", json=pending, headers=headers).status_code, 200)
            self.assertEqual(self.admin.post(admin_base + "/close", json={}, headers=admin_headers).status_code, 200)
        self.assertEqual(guest.post(base + "/input", json={"type": "text", "text": "guest"},
                                   headers=headers).status_code, 200)
        self.assertEqual(self.store.get(task["id"])["state"], "active")
        for _ in range(100):
            if pending in self.browsers[0].inputs and {"type": "text", "text": "guest"} in self.browsers[0].inputs:
                break
            time.sleep(0.02)
        self.assertIn(pending, self.browsers[0].inputs)
        self.assertIn({"type": "text", "text": "guest"}, self.browsers[0].inputs)
        self.assertTrue(runtime.thread.is_alive())
        self.assertEqual(guest.get(base + "/frame").status_code, 200)
        self.assertEqual(self.ads.stopped, [])
        self.assertEqual(len(self.ads.started), 1)
        for field in ("controller", "profile_id", "expires_at"):
            self.assertEqual(self.store.get(task["id"])[field], original[field])
        self.assertFalse(self.browsers[0].closed)
        self.assertEqual(self.admin.get(admin_base + "/frame").status_code, 409)

    def test_admin_observer_button_does_not_say_close_browser(self):
        task, guest, base, headers = self.new_task()
        html = self.admin.get(f"/admin/tasks/{task['id']}/browser").get_data(as_text=True)
        self.assertRegex(html, r'<button id="submit"[^>]* disabled>结束观察</button>')
        self.assertNotIn("结束观察 / 关闭浏览器", html)

    def test_admin_idle_closes_browser_without_reopening_guest_permissions(self):
        task, guest, base, headers = self.new_task()
        admin_base = f"/api/admin/tasks/{task['id']}/browser"
        self.admin.post(admin_base + "/open", json={}, headers={"X-CSRF-Token": self.admin_csrf})
        guest.post(base + "/submit", json={}, headers=headers)
        self.wait_state(task["id"], {"completed"})
        runtime = self.manager.runtimes[task["id"]]
        with runtime.gate:
            runtime.last_admin_seen = time.monotonic() - 901
        runtime.thread.join(2)
        self.assertFalse(runtime.thread.is_alive())
        self.assertEqual(self.store.get(task["id"])["state"], "completed")
        self.assertEqual(self.store.get(task["id"])["admin_open"], 0)
        self.assertEqual(guest.post(base + "/claim", json={}, headers=headers).status_code, 409)

    def test_recovery_closes_admin_browser_but_preserves_finished_task_result(self):
        task = self.store.create(VALUES)
        self.store.update(task["id"], state="passed", message="任务检查通过", checked_at=123,
                          profile_id="admin-profile", admin_open=1)
        self.assertEqual(self.store.recover(), ["admin-profile"])
        result = self.store.get(task["id"])
        self.assertEqual((result["state"], result["message"], result["checked_at"], result["admin_open"]),
                         ("passed", "任务检查通过", 123, 0))


if __name__ == "__main__":
    unittest.main()

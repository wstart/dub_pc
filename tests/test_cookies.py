from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from taskapp import create_app
from taskapp.browser import RemoteBrowser
from tests.test_tasks import FakeAds, FakeBrowser, VALUES


COOKIES = [{"name": "session", "value": "<fixture-secret>", "domain": ".example.test",
            "path": "/private", "expires": -1, "httpOnly": True, "secure": True,
            "sameSite": "Lax", "partitionKey": "https://example.test"}]


class CookieTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.ads = FakeAds()
        self.read_thread = None
        self.cookie_error = None

        class Browser(FakeBrowser):
            def cookies(browser):
                self.read_thread = threading.get_ident()
                if self.cookie_error:
                    raise self.cookie_error
                return COOKIES

        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret",
                               "ADMIN_PASSWORD": "test-admin",
                               "DATABASE": str(Path(self.directory.name) / "tasks.db")},
                              ads=self.ads, browser_factory=Browser)
        self.manager = self.app.extensions["task_manager"]
        self.store = self.app.extensions["task_store"]
        self.admin = self.app.test_client()
        self.admin.get("/")
        with self.admin.session_transaction() as session:
            self.headers = {"X-CSRF-Token": session["csrf"]}
        self.admin.post("/api/login", json={"password": "test-admin"}, headers=self.headers)
        task = self.manager.create(VALUES)
        self.task_id = task["id"]
        deadline = time.monotonic() + 3
        while (self.store.get(self.task_id)["state"] == "starting"
               or self.store.get(self.task_id)["browser_tabs"] == "[]") and time.monotonic() < deadline:
            time.sleep(0.01)
        self.base = f"/api/admin/tasks/{self.task_id}/cookies"

    def tearDown(self):
        for runtime in self.manager.runtimes.values():
            runtime.cancel()
            runtime.thread.join(5)
        self.directory.cleanup()

    def test_only_admin_with_csrf_can_view_or_download(self):
        guest = self.app.test_client()
        for suffix in ("", "/download"):
            self.assertEqual(guest.post(self.base + suffix, json={}, headers=self.headers).status_code, 401)
            self.assertEqual(self.admin.post(self.base + suffix, json={}).status_code, 403)
            self.assertEqual(self.admin.post(self.base + suffix, json={},
                                            headers={"X-CSRF-Token": "wrong"}).status_code, 403)
            self.assertEqual(self.admin.get(self.base + suffix).status_code, 405)
        self.assertIsNone(self.read_thread)

    def test_view_reads_all_fields_on_actor_thread_without_changing_task_or_permissions(self):
        before = self.store.get(self.task_id)
        response = self.admin.post(self.base, json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["cookies"], COOKIES)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.store.get(self.task_id), before)
        self.assertNotEqual(self.read_thread, threading.get_ident())
        for value in (self.ads.started, self.ads.stopped, self.ads.deleted):
            self.assertEqual(len(value), 1 if value is self.ads.started else 0)
        self.assertNotIn("fixture-secret", self.admin.get("/api/admin/tasks").get_data(as_text=True))
        guest = self.app.test_client()
        self.assertNotIn("fixture-secret", guest.get("/api/task/" + before["token"]).get_data(as_text=True))

    def test_download_is_full_json_attachment_not_a_server_file(self):
        response = self.admin.post(self.base + "/download", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.data), COOKIES)
        self.assertEqual(response.mimetype, "application/json")
        self.assertIn("attachment;", response.headers["Content-Disposition"])
        self.assertIn(self.store.get(self.task_id)["code"], response.headers["Content-Disposition"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_missing_released_and_closed_profiles_do_not_get_automatically_opened(self):
        self.assertEqual(self.admin.post("/api/admin/tasks/999/cookies", json={},
                                         headers=self.headers).status_code, 404)
        self.store.update(self.task_id, profile_id=None)
        self.assertEqual(self.admin.post(self.base, json={}, headers=self.headers).status_code, 409)
        self.store.update(self.task_id, profile_id="fixture")
        runtime = self.manager.runtimes[self.task_id]
        runtime.cancel()
        runtime.thread.join(5)
        started = len(self.ads.started)
        response = self.admin.post(self.base, json={}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIn("管理员打开", response.json["error"])
        self.assertEqual(len(self.ads.started), started)

    def test_read_failure_is_sanitized_and_does_not_lock_or_close_browser(self):
        self.cookie_error = RuntimeError("fixture-secret " + json.dumps(COOKIES))
        before = self.store.get(self.task_id)
        response = self.admin.post(self.base, json={}, headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("fixture-secret", response.get_data(as_text=True))
        self.assertEqual(self.store.get(self.task_id), before)
        self.assertTrue(self.manager.runtimes[self.task_id].thread.is_alive())
        self.assertEqual(self.ads.stopped, [])

    def test_completed_admin_browser_remains_locked_to_user_after_cookie_read(self):
        runtime = self.manager.runtimes[self.task_id]
        runtime.claim("owner")
        runtime.attach_admin()
        runtime.submit("owner")
        before = self.store.get(self.task_id)
        response = self.admin.post(self.base, json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["cookies"], COOKIES)
        self.assertEqual(self.store.get(self.task_id), before)
        self.assertEqual(before["state"], "completed")

    def test_logout_revokes_cookie_access(self):
        self.admin.post("/api/logout", json={}, headers=self.headers)
        self.assertEqual(self.admin.post(self.base, json={}, headers=self.headers).status_code, 401)

    def test_browser_reads_context_not_current_page_or_url_subset(self):
        browser = RemoteBrowser.__new__(RemoteBrowser)
        browser.context = Mock()
        browser.context.cookies.return_value = COOKIES
        self.assertEqual(browser.cookies(), COOKIES)
        browser.context.cookies.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

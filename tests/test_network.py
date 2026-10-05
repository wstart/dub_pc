from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock

from playwright.sync_api import Error

from taskapp import create_app
from taskapp.browser import RemoteBrowser
from tests.test_tasks import FakeAds, FakeBrowser, VALUES


class NetworkDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.browser = RemoteBrowser.__new__(RemoteBrowser)
        self.browser.browser = Mock()
        self.browser.browser.is_connected.return_value = True
        self.browser.context = Mock()
        self.page = Mock()
        self.page.is_closed.return_value = False
        self.browser.context.pages = [self.page]
        self.browser.page = self.page
        self.browser.bind_diagnostics(7, "owned-test-profile")
        self.browser.enable_diagnostics()
        self.events = dict(call.args for call in self.page.on.call_args_list)

    def request(self, kind="script", main=False, url="https://cdn.example.com/main.js?token=secret"):
        request = Mock()
        request.url = url
        request.resource_type = kind
        request.frame = self.page.main_frame
        request.is_navigation_request.return_value = main
        request.failure = "net::ERR_SOCKS_CONNECTION_FAILED"
        self.events["request"](request)
        return request

    def test_script_failure_warns_without_exposing_url_or_claiming_business_impact(self):
        request = self.request()
        with self.assertLogs("taskapp.browser", level="WARNING") as logs:
            self.events["requestfailed"](request)
        warning = self.browser.network_state()
        self.assertEqual(warning["level"], "warning")
        self.assertIn("可能影响", warning["message"])
        self.assertEqual(warning["details"][0]["host"], "cdn.example.com")
        self.assertEqual(warning["details"][0]["reason"], "net::ERR_SOCKS_CONNECTION_FAILED")
        self.assertNotIn("secret", json.dumps(warning) + "\n".join(logs.output))
        self.page.reload.assert_not_called()
        self.page.goto.assert_not_called()
        self.browser.context.clear_cookies.assert_not_called()

    def test_main_document_failure_is_error_and_does_not_remain_loading(self):
        request = self.request("document", main=True)
        self.events["requestfailed"](request)
        self.assertEqual(self.browser.network_state()["level"], "error")
        self.assertNotIn(self.page, self.browser._loading_pages)

    def test_http_error_warns_even_without_requestfailed_event_and_recovery_clears_it(self):
        request = self.request("fetch")
        response = Mock(request=request, status=503)
        self.events["response"](response)
        self.assertEqual(self.browser.network_state()["details"][0]["reason"], "HTTP 503")
        response.status = 200
        self.events["response"](response)
        self.assertIsNone(self.browser.network_state())

    def test_iframe_document_failure_is_partial_not_main_page_failure(self):
        request = self.request("document")
        request.is_navigation_request.return_value = True
        request.frame = Mock()
        self.events["requestfailed"](request)
        self.assertEqual(self.browser.network_state()["level"], "warning")

    def test_images_and_fonts_are_info_and_failures_are_deduplicated(self):
        request = self.request("image")
        self.events["requestfailed"](request)
        self.events["requestfailed"](request)
        self.assertEqual(self.browser.network_state()["level"], "info")
        self.assertEqual(self.browser.network_state()["count"], 1)

    def test_abort_is_not_an_alert_and_untrusted_error_text_is_not_exposed(self):
        request = self.request()
        request.failure = "net::ERR_ABORTED"
        self.events["requestfailed"](request)
        self.assertIsNone(self.browser.network_state())
        request.failure = "secret cookie https://private.example/path?password=secret"
        self.events["requestfailed"](request)
        self.assertNotIn("secret", json.dumps(self.browser.network_state()))

    def test_navigation_clears_warning_and_ignores_late_failure_from_previous_document(self):
        previous = self.request()
        self.events["requestfailed"](previous)
        self.request("document", main=True, url="https://example.com/new")
        self.assertIsNone(self.browser.network_state())
        self.events["requestfailed"](previous)
        self.assertIsNone(self.browser.network_state())

    def test_switch_and_close_keep_alerts_scoped_to_their_tab(self):
        request = self.request()
        self.events["requestfailed"](request)
        other = Mock()
        other.is_closed.return_value = False
        self.browser.context.pages.append(other)
        self.browser._track_page(other)
        self.browser.page = other
        self.assertIsNone(self.browser.network_state())
        self.browser.page = self.page
        self.assertIsNotNone(self.browser.network_state())
        self.events["close"](self.page)
        self.assertNotIn(self.page, self.browser._network_pages)

    def test_failure_details_are_bounded_and_main_failure_is_not_evicted_by_images(self):
        main = self.request("document", main=True)
        self.events["requestfailed"](main)
        for index in range(80):
            image = self.request("image", url=f"https://cdn.example.com/{index}.png")
            self.events["requestfailed"](image)
        warning = self.browser.network_state()
        self.assertEqual(warning["level"], "error")
        self.assertLessEqual(warning["count"], 50)
        self.assertLessEqual(len(warning["details"]), 8)
        self.assertTrue(warning["limited"])

    def test_navigation_without_a_frame_is_not_misclassified_or_fatal(self):
        request = self.request("document")
        request.is_navigation_request.return_value = True
        type(request).frame = PropertyMock(side_effect=Error("Frame not yet created"))
        self.events["request"](request)
        self.events["requestfailed"](request)
        self.assertEqual(self.browser.network_state()["level"], "warning")


class NetworkAccessTests(unittest.TestCase):
    def test_alert_is_visible_only_to_owner_or_open_admin_and_does_not_change_task_state(self):
        warning = {"level": "warning", "message": "部分资源加载失败，可能影响页面功能",
                   "count": 1, "limited": False, "details": []}

        class Browser(FakeBrowser):
            def network_state(self):
                return warning

        with tempfile.TemporaryDirectory() as directory:
            app = create_app({"TESTING": True, "SECRET_KEY": "test-secret",
                              "ADMIN_PASSWORD": "test-admin",
                              "DATABASE": str(Path(directory) / "tasks.db")},
                             ads=FakeAds(), browser_factory=Browser)
            manager, store = app.extensions["task_manager"], app.extensions["task_store"]
            task = manager.create(VALUES)
            runtime = manager.runtimes[task["id"]]
            try:
                deadline = time.monotonic() + 3
                while not getattr(runtime, "network_warning", None) and time.monotonic() < deadline:
                    time.sleep(0.01)
                owner, other, admin = (app.test_client() for _ in range(3))
                base = "/api/task/" + task["token"]
                for client in (owner, other, admin):
                    client.get("/t/" + task["token"])
                with owner.session_transaction() as session:
                    csrf = session["csrf"]
                owner.post(base + "/claim", json={}, headers={"X-CSRF-Token": csrf})
                self.assertEqual(owner.get(base).json["task"]["network_warning"], warning)
                self.assertNotIn("network_warning", other.get(base).json["task"])
                with admin.session_transaction() as session:
                    admin_csrf = session["csrf"]
                headers = {"X-CSRF-Token": admin_csrf}
                admin.post("/api/login", json={"password": "test-admin"}, headers=headers)
                admin_base = f"/api/admin/tasks/{task['id']}/browser"
                self.assertIsNone(admin.get(admin_base).json["task"]["network_warning"])
                admin.post(admin_base + "/open", json={}, headers=headers)
                self.assertEqual(admin.get(admin_base).json["task"]["network_warning"], warning)
                self.assertEqual(store.get(task["id"])["state"], "active")
                admin.post(admin_base + "/close", json={}, headers=headers)
                self.assertIsNone(admin.get(admin_base).json["task"]["network_warning"])
            finally:
                runtime.cancel()
                runtime.thread.join(5)


if __name__ == "__main__":
    unittest.main()

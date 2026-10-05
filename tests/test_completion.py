from __future__ import annotations

import json
import time
import unittest
from unittest.mock import MagicMock

from tests import test_tasks
from taskapp import task_values
from taskapp.browser import RemoteBrowser


class CompletionTests(unittest.TestCase):
    setUp = test_tasks.TaskTests.setUp
    tearDown = test_tasks.TaskTests.tearDown
    new_task = test_tasks.TaskTests.new_task
    wait_state = test_tasks.TaskTests.wait_state
    wait_admin_ready = test_tasks.TaskTests.wait_admin_ready

    def test_create_needs_no_verification_and_form_has_no_checker(self):
        values = task_values({"title": "无需校验", "start_url": "https://example.com",
                              "lifetime_minutes": 60})
        for field in ("check_url", "pattern", "content_type", "ignore_case"):
            self.assertNotIn(field, values)
            self.assertNotIn(f'name="{field}"', self.admin.get("/").get_data(as_text=True))

    def test_complete_is_immediate_locks_all_guest_access_and_does_not_read_content(self):
        task, guest, base, headers = self.new_task()
        response = guest.post(base + "/submit", json={}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.get(task["id"])["state"], "completed")
        self.assertEqual(self.browsers[0].checked, [])
        for endpoint in ("claim", "input", "submit"):
            self.assertEqual(guest.post(base + "/" + endpoint, json={"type": "text", "text": "blocked"},
                                        headers=headers).status_code, 409)
        self.assertEqual(guest.get(base + "/frame").status_code, 409)

    def test_only_admin_can_reopen_with_csrf(self):
        task, guest, base, headers = self.new_task()
        path = f"/api/admin/tasks/{task['id']}/reopen"
        self.assertEqual(guest.post(path, json={}, headers=headers).status_code, 401)
        self.assertEqual(self.admin.post(path, json={}).status_code, 403)
        self.assertEqual(self.admin.post(path, json={}, headers={"X-CSRF-Token": self.admin_csrf}).status_code, 409)

    def test_reopen_live_admin_browser_keeps_profile_controller_and_discards_admin_queue(self):
        task, guest, base, headers = self.new_task()
        admin_headers = {"X-CSRF-Token": self.admin_csrf}
        admin_base = f"/api/admin/tasks/{task['id']}/browser"
        self.admin.post(admin_base + "/open", json={}, headers=admin_headers)
        guest.post(base + "/submit", json={}, headers=headers)
        controller = self.store.get(task["id"])["controller"]
        runtime = self.manager.runtimes[task["id"]]
        with runtime.gate:
            runtime.admin_input({"type": "text", "text": "stale-admin"})
            result = self.admin.post(f"/api/admin/tasks/{task['id']}/reopen", json={}, headers=admin_headers)
            self.assertEqual(result.status_code, 202)
            self.assertTrue(runtime.commands.empty())
        self.assertEqual(self.store.get(task["id"])["state"], "active")
        self.assertEqual(self.store.get(task["id"])["controller"], controller)
        self.assertEqual(len(self.ads.created), 1)
        self.assertFalse(self.admin.get(admin_base).json["task"]["can_control"])
        self.assertEqual(self.admin.post(admin_base + "/input", json={"type": "text", "text": "admin"},
                                        headers=admin_headers).status_code, 409)
        self.assertEqual(guest.post(base + "/input", json={"type": "text", "text": "resumed"},
                                   headers=headers).status_code, 200)

    def test_reopen_closed_profile_renews_original_lifetime_and_old_session_only(self):
        task, guest, base, headers = self.new_task()
        guest.post(base + "/submit", json={}, headers=headers)
        self.manager.runtimes[task["id"]].thread.join(2)
        self.store.update(task["id"], expires_at=time.time() - 1)
        started = time.time()
        self.assertEqual(self.admin.post(f"/api/admin/tasks/{task['id']}/reopen", json={},
                                        headers={"X-CSRF-Token": self.admin_csrf}).status_code, 202)
        result = self.wait_state(task["id"], {"active"})
        self.assertEqual(len(self.ads.created), 1)
        self.assertEqual(result["profile_id"], task["profile_id"])
        self.assertAlmostEqual(result["expires_at"] - started, 3600, delta=2)
        self.assertIsNone(result["checked_at"])
        self.assertEqual(guest.post(base + "/input", json={"type": "text", "text": "resumed"},
                                   headers=headers).status_code, 200)
        other = self.app.test_client()
        other.get("/t/" + task["token"])
        with other.session_transaction() as session:
            other_headers = {"X-CSRF-Token": session["csrf"]}
        self.assertEqual(other.post(base + "/claim", json={}, headers=other_headers).status_code, 409)

    def test_released_profile_cannot_be_reopened(self):
        task, guest, base, headers = self.new_task()
        guest.post(base + "/submit", json={}, headers=headers)
        self.manager.runtimes[task["id"]].thread.join(2)
        admin_headers = {"X-CSRF-Token": self.admin_csrf}
        self.admin.post(f"/api/admin/tasks/{task['id']}/release", json={}, headers=admin_headers)
        self.assertEqual(self.admin.post(f"/api/admin/tasks/{task['id']}/reopen", json={},
                                        headers=admin_headers).status_code, 409)

    def test_reopen_invalidates_already_dequeued_old_user_and_admin_commands(self):
        task, guest, base, headers = self.new_task()
        runtime = self.manager.runtimes[task["id"]]
        runtime.attach_admin()
        with runtime.gate:
            old_user, old_admin = runtime.user_generation, runtime.admin_generation
            runtime.submit(self.store.get(task["id"])["controller"])
            runtime.reopen_user()
            runtime.commands.put(("input", (old_user, {"type": "text", "text": "stale-user"})))
            runtime.commands.put(("admin_input", (old_admin, {"type": "text", "text": "stale-admin"})))
        guest.post(base + "/input", json={"type": "text", "text": "fresh"}, headers=headers)
        for _ in range(100):
            if {"type": "text", "text": "fresh"} in self.browsers[0].inputs:
                break
            time.sleep(0.02)
        self.assertEqual(self.browsers[0].inputs, [{"type": "text", "text": "fresh"}])

    def test_failed_reopen_stays_locked_and_does_not_create_another_profile(self):
        task, guest, base, headers = self.new_task()
        guest.post(base + "/submit", json={}, headers=headers)
        self.manager.runtimes[task["id"]].thread.join(2)
        def fail_start(*args, **kwargs):
            raise RuntimeError("start failed")
        self.ads.start = fail_start
        with self.assertLogs(level="ERROR"):
            self.admin.post(f"/api/admin/tasks/{task['id']}/reopen", json={},
                            headers={"X-CSRF-Token": self.admin_csrf})
            self.wait_state(task["id"], {"error"})
        self.assertEqual(guest.post(base + "/input", json={"type": "text", "text": "blocked"},
                                   headers=headers).status_code, 409)
        self.assertEqual(len(self.ads.created), 1)

    def test_old_database_keeps_records_and_migrates_original_lifetime(self):
        task = self.store.create({**test_tasks.VALUES, "lifetime_minutes": 15})
        with self.store.connect() as db:
            db.execute("ALTER TABLE tasks DROP COLUMN lifetime_minutes")
        migrated = test_tasks.Store(self.store.path)
        self.assertEqual(migrated.get(task["id"])["lifetime_minutes"], 15)
        self.assertEqual(migrated.get(task["id"])["token"], task["token"])

    def test_browser_tabs_migration_preserves_original_task(self):
        task = self.store.create(test_tasks.VALUES)
        with self.store.connect() as db:
            db.execute("ALTER TABLE tasks DROP COLUMN browser_tabs")
        migrated = test_tasks.Store(self.store.path)
        result = migrated.get(task["id"])
        self.assertEqual(result["browser_tabs"], "[]")
        self.assertEqual(result["token"], task["token"])
        self.assertEqual(result["start_url"], task["start_url"])

    def test_last_nonblank_tabs_are_saved_and_blank_snapshot_does_not_erase_them(self):
        task, guest, base, headers = self.new_task()
        tabs = [{"url": "https://example.com/last", "active": True}]
        self.browsers[0].snapshot = lambda: (b"frame", tabs)
        for _ in range(100):
            if json.loads(self.store.get(task["id"])["browser_tabs"]) == tabs:
                break
            time.sleep(0.02)
        self.assertEqual(json.loads(self.store.get(task["id"])["browser_tabs"]), tabs)
        self.browsers[0].snapshot = lambda: (b"frame", [{"url": "about:blank", "active": True}])
        time.sleep(0.8)
        self.assertEqual(json.loads(self.store.get(task["id"])["browser_tabs"]), tabs)


class PageRestoreTests(unittest.TestCase):
    def browser(self, urls):
        browser = RemoteBrowser.__new__(RemoteBrowser)
        browser.context = MagicMock()
        pages = []

        def page(url="about:blank"):
            item = MagicMock()
            item.url = url
            item.is_closed.return_value = False
            item.goto.side_effect = lambda value, **kwargs: setattr(item, "url", value)
            pages.append(item)
            return item

        for url in urls:
            page(url)
        browser.context.pages = pages
        browser.context.new_page.side_effect = page
        browser.page = pages[-1]
        browser._log_event = MagicMock()
        return browser

    def test_blank_resume_opens_start_page_without_touching_cookie_storage(self):
        browser = self.browser(["about:blank"])
        browser.restore_tabs([], "https://example.com/start")
        self.assertEqual(browser.page.url, "https://example.com/start")
        browser.context.clear_cookies.assert_not_called()
        browser.context.add_cookies.assert_not_called()
        browser.context.clear_permissions.assert_not_called()

    def test_saved_tabs_and_selected_page_are_restored(self):
        browser = self.browser(["about:blank"])
        browser.restore_tabs([{"url": "https://example.com/one", "active": True},
                              {"url": "https://example.com/two", "active": False}],
                             "https://example.com/start")
        self.assertEqual([p.url for p in browser.pages()],
                         ["https://example.com/one", "https://example.com/two"])
        self.assertEqual(browser.page.url, "https://example.com/one")
        browser.context.clear_cookies.assert_not_called()

    def test_existing_live_page_is_not_reloaded_or_replaced_by_blank_tab(self):
        browser = self.browser(["https://example.com/live", "about:blank"])
        browser.restore_tabs([{"url": "https://example.com/old", "active": True}],
                             "https://example.com/start")
        self.assertEqual(browser.page.url, "https://example.com/live")
        for page in browser.pages():
            page.goto.assert_not_called()
        browser.context.new_page.assert_not_called()

    def test_invalid_saved_urls_fall_back_to_start(self):
        browser = self.browser(["about:blank"])
        browser.restore_tabs([{"url": "file:///private", "active": True},
                              {"url": "javascript:alert(1)", "active": False}],
                             "https://example.com/start")
        self.assertEqual(browser.page.url, "https://example.com/start")

    def test_failed_saved_navigation_falls_back_without_clearing_cookies(self):
        from playwright.sync_api import Error

        browser = self.browser(["about:blank"])
        page = browser.page

        def navigate(url, **kwargs):
            if url.endswith("/unreachable"):
                raise Error("net::ERR_CONNECTION_REFUSED")
            page.url = url

        page.goto.side_effect = navigate
        browser.restore_tabs([{"url": "https://example.com/unreachable", "active": True}],
                             "https://example.com/start")
        self.assertEqual(browser.page.url, "https://example.com/start")
        browser.context.clear_cookies.assert_not_called()


if __name__ == "__main__":
    unittest.main()

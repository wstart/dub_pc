from __future__ import annotations

import unittest
import base64
from unittest.mock import Mock, patch

from taskapp.browser import RemoteBrowser


class BrowserDiagnosticsTests(unittest.TestCase):
    def browser(self):
        browser = RemoteBrowser.__new__(RemoteBrowser)
        browser.browser = Mock()
        browser.browser.is_connected.return_value = True
        browser.context = Mock()
        browser.context.new_cdp_session.return_value.send.return_value = {}
        browser.page = Mock()
        browser.page.is_closed.return_value = False
        browser.context.pages = [browser.page]
        browser.bind_diagnostics(7, "test-profile")
        browser._page_ids = {browser.page: 1}
        browser._page_sessions = {}
        browser._loading_pages = set()
        browser._pending_navigation = {}
        browser.disconnect_requested = False
        return browser

    def test_snapshot_failure_records_connection_and_page_state_without_secrets(self):
        browser = self.browser()
        secret = "https://private.test/?token=test-secret"

        def fail(**kwargs):
            browser.page.is_closed.return_value = True
            raise RuntimeError(secret)

        browser.page.screenshot.side_effect = fail
        with self.assertLogs("taskapp.browser", level="WARNING") as logs:
            with self.assertRaises(RuntimeError):
                browser.snapshot()
        output = "\n".join(logs.output)
        for value in ("task=7", "profile=test-profile", "event=snapshot_failed",
                      "connected=True", "page_closed=True", "open_pages=0", "error=RuntimeError"):
            self.assertIn(value, output)
        self.assertNotIn(secret, output)

    def test_page_close_crash_context_close_and_disconnect_are_logged(self):
        browser = self.browser()
        browser.enable_diagnostics()
        page_events = dict(browser.page.on.call_args_list[index].args
                           for index in range(len(browser.page.on.call_args_list)))
        context_events = dict(call.args for call in browser.context.on.call_args_list)
        browser_events = dict(call.args for call in browser.browser.on.call_args_list)
        with self.assertLogs("taskapp.browser", level="INFO") as logs:
            page_events["close"](browser.page)
            page_events["crash"](browser.page)
            context_events["close"](browser.context)
            browser_events["disconnected"](browser.browser)
        output = "\n".join(logs.output)
        for event in ("page_closed", "page_crashed", "context_closed", "browser_disconnected"):
            self.assertIn("event=" + event, output)

    def test_new_pages_are_tracked_once_and_navigation_does_not_log_urls(self):
        browser = self.browser()
        browser.enable_diagnostics()
        new_page = Mock()
        new_page.is_closed.return_value = False
        new_page.url = "https://private.test/?token=private-token"
        with self.assertLogs("taskapp.browser", level="INFO") as logs:
            browser._track_page(new_page)
            browser._track_page(new_page)
            events = dict(call.args for call in new_page.on.call_args_list)
            events["framenavigated"](new_page.main_frame)
        self.assertEqual(new_page.on.call_count, 6)
        output = "\n".join(logs.output)
        self.assertIn("event=page_created", output)
        self.assertIn("event=page_navigated", output)
        self.assertNotIn("private-token", output)
        self.assertNotIn("private.test", output)

    def test_screencast_acknowledges_frames_and_publishes_latest_without_screenshot(self):
        browser = self.browser()
        session = browser.context.new_cdp_session.return_value
        frames = []
        browser.start_frames(frames.append)
        handler = next(call.args[1] for call in session.on.call_args_list
                       if call.args[0] == "Page.screencastFrame")
        handler({"sessionId": 1, "data": base64.b64encode(b"old").decode()})
        handler({"sessionId": 2, "data": base64.b64encode(b"latest").decode()})
        browser.pump_frames()
        self.assertEqual(frames, [b"latest"])
        self.assertEqual([call.args[1]["sessionId"] for call in session.send.call_args_list
                          if call.args[0] == "Page.screencastFrameAck"], [1, 2])
        browser.page.screenshot.assert_not_called()

    def test_screencast_rebinds_when_selected_tab_changes(self):
        browser = self.browser()
        first, second = Mock(), Mock()
        browser.context.new_cdp_session.side_effect = [first, second]
        browser.start_frames(lambda frame: None)
        page = Mock()
        page.is_closed.return_value = False
        browser.context.pages.append(page)
        browser.page = page
        browser.pump_frames()
        first.detach.assert_not_called()
        self.assertTrue(any(call.args[0] == "Page.startScreencast"
                            for call in second.send.call_args_list))

    def test_navigation_does_not_wait_for_page_loading(self):
        browser = self.browser()
        browser.input({"type": "navigate", "url": "https://example.com/?a=1"})
        browser.page.goto.assert_not_called()
        session = browser.context.new_cdp_session.return_value
        method, values = session.send.call_args.args
        self.assertEqual(method, "Runtime.evaluate")
        self.assertIn("location.assign", values["expression"])
        self.assertIn("https://example.com/?a=1", values["expression"])
        session.detach.assert_not_called()

    def test_navigation_and_screencast_reuse_the_page_connection(self):
        browser = self.browser()
        browser.start_frames(lambda frame: None)
        for _ in range(3):
            browser.input({"type": "navigate", "url": "https://example.com/"})
        browser.context.new_cdp_session.assert_called_once_with(browser.page)
        browser.context.new_cdp_session.return_value.detach.assert_not_called()

    def test_returning_to_a_tab_reuses_screencast_without_duplicate_callbacks(self):
        browser = self.browser()
        first, second = Mock(), Mock()
        browser.context.new_cdp_session.side_effect = [first, second]
        original = browser.page
        browser.start_frames(lambda frame: None)
        page = Mock()
        page.is_closed.return_value = False
        browser.context.pages.append(page)
        browser.page = page
        browser.pump_frames()
        browser.page = original
        browser.pump_frames()
        self.assertEqual(browser.context.new_cdp_session.call_count, 2)
        first.on.assert_called_once()
        first.detach.assert_not_called()

    def test_back_and_reload_do_not_wait_for_page_loading(self):
        browser = self.browser()
        for action in ("back", "reload"):
            browser.input({"type": action})
        browser.page.go_back.assert_not_called()
        browser.page.reload.assert_not_called()

    def test_select_all_uses_mac_command_and_keeps_other_keys_unchanged(self):
        browser = self.browser()
        browser.page.evaluate.return_value = "MacIntel"
        browser.input({"type": "key", "key": "Control+a"})
        browser.page.keyboard.press.assert_called_once_with("Meta+a")
        browser.page.evaluate.reset_mock()
        browser.input({"type": "key", "key": "Backspace"})
        browser.page.keyboard.press.assert_called_with("Backspace")
        browser.page.evaluate.assert_not_called()

    def test_select_all_keeps_control_for_existing_non_mac_profiles(self):
        for platform in ("Linux x86_64", "Win32"):
            with self.subTest(platform=platform):
                browser = self.browser()
                browser.page.evaluate.return_value = platform
                browser.input({"type": "key", "key": "Control+a"})
                browser.page.keyboard.press.assert_called_once_with("Control+a")

    def test_noop_back_does_not_leave_screencast_waiting_forever(self):
        browser = self.browser()
        browser.start_frames(lambda frame: None)
        with patch("taskapp.browser.time.monotonic", return_value=100):
            browser.input({"type": "back"})
        with patch("taskapp.browser.time.monotonic", return_value=101):
            browser.pump_frames()
        self.assertNotIn(browser.page, browser._loading_pages)

    def test_slow_background_navigation_does_not_block_screencast_switch(self):
        browser = self.browser()
        first, second = Mock(), Mock()
        browser.context.new_cdp_session.side_effect = [first, second]
        original = browser.page
        browser.start_frames(lambda frame: None)
        browser._loading_pages.add(original)
        page = Mock()
        page.is_closed.return_value = False
        browser.context.pages.append(page)
        browser.page = page
        browser.pump_frames()
        self.assertFalse(any(call.args[0] == "Page.stopScreencast" for call in first.send.call_args_list))
        browser._loading_pages.discard(original)
        browser.pump_frames()
        self.assertTrue(any(call.args[0] == "Page.stopScreencast" for call in first.send.call_args_list))

    def test_close_background_tab_keeps_selected_page(self):
        browser = self.browser()
        background = Mock()
        background.is_closed.return_value = False
        browser.context.pages.append(background)
        browser._page_ids[background] = 2
        selected = browser.page
        browser.input({"type": "close_tab", "id": 2})
        background.close.assert_called_once()
        self.assertIs(browser.page, selected)
        browser.context.clear_cookies.assert_not_called()

    def test_close_selected_tab_selects_neighbor(self):
        browser = self.browser()
        neighbor = Mock()
        neighbor.is_closed.return_value = False
        browser.context.pages.append(neighbor)
        browser._page_ids[neighbor] = 2
        original = browser.page
        browser.input({"type": "close_tab", "id": 1})
        self.assertIs(browser.page, neighbor)
        original.close.assert_called_once()
        browser.context.close.assert_not_called()

    def test_close_last_tab_creates_blank_page_before_closing(self):
        browser = self.browser()
        original, blank, order = browser.page, Mock(), []
        def created():
            order.append("new")
            return blank
        browser.context.new_page.side_effect = created
        original.close.side_effect = lambda: order.append("close")
        browser.input({"type": "close_tab", "id": 1})
        self.assertEqual(order, ["new", "close"])
        self.assertIs(browser.page, blank)
        browser.context.clear_cookies.assert_not_called()
        browser.context.close.assert_not_called()

    def test_stale_close_id_cannot_close_a_different_tab(self):
        browser = self.browser()
        with self.assertRaises(ValueError):
            browser.input({"type": "close_tab", "id": 9})
        browser.page.close.assert_not_called()

    def test_select_uses_stable_id_after_indices_change(self):
        browser = self.browser()
        neighbor = Mock()
        neighbor.is_closed.return_value = False
        browser.context.pages.append(neighbor)
        browser._page_ids[neighbor] = 3
        browser.input({"type": "tab", "id": 3})
        self.assertIs(browser.page, neighbor)

    def test_tab_metadata_contains_stable_page_id(self):
        browser = self.browser()
        browser.page.url = "https://example.com/"
        self.assertEqual(browser.tab_state(), [{"id": 1, "index": 0,
                                               "url": "https://example.com/", "active": True}])


if __name__ == "__main__":
    unittest.main()

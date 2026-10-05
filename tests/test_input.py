from __future__ import annotations

import json
import math
import threading
import time
import unittest
from unittest.mock import Mock, call, patch

from taskapp import input_values
from taskapp.browser import RemoteBrowser
from taskapp.tasks import TaskConflict
from tests import test_tasks


class InputValidationTests(unittest.TestCase):
    def test_pointer_actions_accept_coordinates_buttons_counts_and_modifiers(self):
        for kind in ("mouse_down", "mouse_move", "mouse_up"):
            action = {"type": kind, "x": 1279.5, "y": 799.5, "modifiers": ["Shift", "Meta"]}
            if kind != "mouse_move":
                action.update(button="middle", click_count=2)
            self.assertEqual(input_values(action), action)
        self.assertEqual(input_values({"type": "reset_input"}), {"type": "reset_input"})

    def test_legacy_action_shape_stays_unchanged(self):
        for action in ({"type": "click", "x": 10, "y": 20, "button": "left"},
                       {"type": "wheel", "x": 10, "y": 20, "dx": 0, "dy": 20},
                       {"type": "key", "key": "Control+a"}):
            self.assertEqual(input_values(action), action)

    def test_numeric_input_rejects_boolean_null_nonfinite_and_containers(self):
        for value in (True, False, None, [], {}, "NaN", "Infinity", math.nan, math.inf):
            for kind in ("click", "wheel", "mouse_down", "mouse_move", "mouse_up"):
                with self.subTest(kind=kind, value=value):
                    with self.assertRaises(ValueError):
                        input_values({"type": kind, "x": value, "y": 1})
            with self.subTest(wheel=value):
                with self.assertRaises(ValueError):
                    input_values({"type": "wheel", "x": 1, "y": 1, "dx": value})
        for value in (True, None, [], {}, "bad", 1.5):
            with self.subTest(tab=value):
                with self.assertRaises(ValueError):
                    input_values({"type": "tab", "index": value})

    def test_invalid_mouse_fields_are_value_errors(self):
        for field, values in (("button", (None, [], {}, "primary")),
                              ("click_count", (None, True, 0, 4, 1.5, "2")),
                              ("modifiers", (None, "Shift", ["Unknown"], ["Shift", "Shift"], [[]]))):
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        input_values({"type": "mouse_down", "x": 10, "y": 20, field: value})

    def test_named_keys_and_modified_punctuation_are_allowed(self):
        for key in ("Space", " ", "ArrowLeft", "Control+Shift+z", "Control++", "Alt+-",
                    "Control+[", "Meta+/", "Shift+ArrowDown", "F12", "KeyA", "Digit0"):
            with self.subTest(key=key):
                self.assertEqual(input_values({"type": "key", "key": key, "primary": True}),
                                 {"type": "key", "key": key, "primary": True})

    def test_unknown_or_control_character_keys_are_rejected(self):
        for key in ("Unknown", "Control+Banana", "Control+Control+a", "Control+", "F13",
                    "a+b", "\n", "Control+\t", "Control+\0", None, []):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    input_values({"type": "key", "key": key})
        for primary in (None, 1, "true"):
            with self.assertRaises(ValueError):
                input_values({"type": "key", "key": "Control+a", "primary": primary})


class BrowserInputTests(unittest.TestCase):
    def browser(self):
        browser = RemoteBrowser.__new__(RemoteBrowser)
        browser.page = Mock()
        browser.page.is_closed.return_value = False
        browser.page.evaluate.return_value = "MacIntel"
        browser.page.frames = [browser.page]
        browser.context = Mock()
        browser.context.pages = [browser.page]
        browser._page_ids = {browser.page: 1}
        return browser

    def test_drag_dispatches_real_down_move_up_without_click(self):
        browser = self.browser()
        for action in ({"type": "mouse_down", "x": 10, "y": 20, "button": "left", "click_count": 2},
                       {"type": "mouse_move", "x": 100, "y": 20},
                       {"type": "mouse_up", "x": 110, "y": 20, "button": "left", "click_count": 2}):
            browser.input(action)
        self.assertEqual(browser.page.mouse.mock_calls,
                         [call.move(10, 20), call.down(button="left", click_count=2),
                          call.move(100, 20), call.move(110, 20), call.up(button="left", click_count=2)])
        browser.page.mouse.click.assert_not_called()

    def test_mouse_modifiers_are_event_scoped_and_released_on_failure(self):
        browser = self.browser()
        for action in ({"type": "click", "x": 10, "y": 20, "button": "left", "modifiers": ["Shift"]},
                       {"type": "mouse_move", "x": 11, "y": 22, "modifiers": ["Shift"]},
                       {"type": "wheel", "x": 10, "y": 20, "dx": 0, "dy": 30, "modifiers": ["Shift"]}):
            browser.input(action)
        self.assertEqual(browser.page.keyboard.down.call_count, 3)
        self.assertEqual(browser.page.keyboard.up.call_count, 3)
        browser.page.mouse.move.side_effect = RuntimeError("failure")
        with self.assertRaises(RuntimeError):
            browser.input({"type": "mouse_move", "x": 11, "y": 22, "modifiers": ["Alt"]})
        browser.page.keyboard.up.assert_called_with("Alt")

    def test_reset_and_page_switch_release_held_button_on_original_page(self):
        browser = self.browser()
        original = browser.page
        second = Mock()
        second.is_closed.return_value = False
        browser.context.pages.append(second)
        browser._page_ids[second] = 2
        browser.input({"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        browser.input({"type": "tab", "id": 2})
        original.mouse.up.assert_called_once_with(button="left", click_count=0)
        second.mouse.up.assert_not_called()
        browser.input({"type": "reset_input"})
        original.mouse.up.assert_called_once()

    def test_failed_down_is_released_and_does_not_leave_modifier_stuck(self):
        browser = self.browser()
        browser.page.mouse.down.side_effect = RuntimeError("failed")
        with self.assertRaises(RuntimeError):
            browser.input({"type": "mouse_down", "x": 10, "y": 20,
                           "button": "left", "modifiers": ["Shift"]})
        browser.page.mouse.up.assert_called_once_with(button="left", click_count=0)
        browser.page.keyboard.up.assert_called_with("Shift")

    def test_release_modifier_failure_retries_and_releases_all_other_state(self):
        browser = self.browser()
        failures = []
        def release(key):
            if key == "Shift" and not failures:
                failures.append(key)
                raise RuntimeError("failed release")
        browser.page.keyboard.up.side_effect = release
        with self.assertRaises(RuntimeError):
            browser.input({"type": "mouse_down", "x": 10, "y": 20, "button": "left",
                           "modifiers": ["Shift", "Alt"]})
        browser.page.mouse.up.assert_called_with(button="left", click_count=0)
        self.assertIn(call("Alt"), browser.page.keyboard.up.call_args_list)
        self.assertEqual(browser._mouse_modifiers, set())

    def test_failed_keyboard_chord_releases_key_and_modifier(self):
        browser = self.browser()
        browser.page.keyboard.press.side_effect = RuntimeError("failed chord")
        with self.assertRaises(RuntimeError):
            browser.input({"type": "key", "key": "Control+ArrowLeft"})
        self.assertEqual(browser.page.keyboard.up.call_args_list, [call("ArrowLeft"), call("Control")])

    def test_closed_page_reset_discards_old_state_without_breaking_other_tabs(self):
        browser = self.browser()
        browser.input({"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        browser.page.is_closed.return_value = True
        browser.page.mouse.up.side_effect = RuntimeError("closed page")
        browser.reset_input()
        self.assertEqual(browser._mouse_buttons, set())
        browser.page.mouse.up.assert_not_called()

    def test_transient_release_failure_preserves_state_for_retry(self):
        browser = self.browser()
        browser.input({"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        browser.page.mouse.up.side_effect = [RuntimeError("temporary"), None]
        with self.assertRaises(RuntimeError):
            browser.reset_input()
        self.assertEqual(browser._mouse_buttons, {"left"})
        self.assertIs(browser._input_page, browser.page)
        browser.reset_input()
        self.assertEqual(browser._mouse_buttons, set())

    def test_cancel_guard_covers_all_frames_and_is_removed_after_release(self):
        browser = self.browser()
        child = Mock()
        child.is_detached.return_value = False
        browser.page.frames.append(child)
        browser.input({"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        browser.reset_input()
        for frame in (browser.page, child):
            self.assertEqual(frame.evaluate.call_count, 2)
            script = frame.evaluate.call_args_list[0].args[0]
            self.assertIn("isTrusted", script)
            self.assertIn("auxclick", script)
            self.assertIn("stopImmediatePropagation", script)
            self.assertIn("removeEventListener", frame.evaluate.call_args_list[1].args[0])

    def test_guard_install_failure_does_not_dispatch_an_unsafe_mouse_up(self):
        browser = self.browser()
        browser.input({"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        browser.page.evaluate.side_effect = [RuntimeError("no guard"), None]
        with self.assertRaises(RuntimeError):
            browser.reset_input()
        browser.page.mouse.up.assert_not_called()
        self.assertEqual(browser._mouse_buttons, {"left"})

    def test_guard_cleanup_failure_retains_pending_guard_for_retry(self):
        browser = self.browser()
        browser.input({"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        browser.page.evaluate.side_effect = [None, RuntimeError("cleanup"), None]
        with self.assertRaises(RuntimeError):
            browser.reset_input()
        self.assertTrue(browser._cancel_guards)
        browser.reset_input()
        self.assertEqual(browser._cancel_guards, [])

    def test_failed_input_and_guard_cleanup_keeps_original_page_until_cleanup_retry(self):
        browser = self.browser()
        original = browser.page
        browser.page.mouse.down.side_effect = RuntimeError("failed down")
        browser.page.evaluate.side_effect = [None, RuntimeError("cleanup"), None]
        with self.assertRaises(RuntimeError):
            browser.input({"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        self.assertTrue(browser._cancel_guards)
        self.assertIs(browser._input_page, original)
        browser.reset_input()
        self.assertEqual(browser._cancel_guards, [])
        self.assertIsNone(browser._input_page)

    def test_primary_shortcuts_use_target_platform_and_legacy_select_all_stays_supported(self):
        for platform, primary, key, expected in (("MacIntel", True, "Control+z", "Meta+z"),
                                                 ("MacIntel", True, "Control+y", "Meta+Shift+z"),
                                                 ("MacIntel", True, "Control+Shift+ArrowLeft", "Meta+Shift+ArrowLeft"),
                                                 ("MacIntel", False, "Control+z", "Control+z"),
                                                 ("Linux", True, "Control+z", "Control+z"),
                                                 ("MacIntel", False, "Control+a", "Meta+a")):
            with self.subTest(platform=platform, key=key, primary=primary):
                browser = self.browser()
                browser.page.evaluate.return_value = platform
                browser.input({"type": "key", "key": key, "primary": primary})
                browser.page.keyboard.press.assert_called_once_with(expected)


class RuntimeInputTests(unittest.TestCase):
    setUp = test_tasks.TaskTests.setUp
    tearDown = test_tasks.TaskTests.tearDown
    new_task = test_tasks.TaskTests.new_task
    wait_state = test_tasks.TaskTests.wait_state
    control = test_tasks.TaskTests.control

    def prepare(self):
        task, guest, base, headers = self.new_task()
        runtime = self.manager.runtimes[task["id"]]
        browser = self.browsers[-1]
        browser.reset_input = Mock()
        return task, guest, base, headers, runtime, browser

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(predicate())

    def test_malformed_http_input_returns_400_not_500(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        for action in ({"type": "click", "x": None, "y": 0},
                       {"type": "click", "x": 0, "y": 0, "button": []},
                       {"type": "tab", "index": None},
                       {"type": [], "x": 0, "y": 0}):
            with self.subTest(action=action):
                self.assertEqual(guest.post(base + "/input", json=action, headers=headers).status_code, 400)

    def test_optional_http_generation_rejects_cross_reopen_requests(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        action = {"type": "text", "text": "old", "generation": runtime.user_generation + 1}
        self.assertEqual(guest.post(base + "/input", json=action, headers=headers).status_code, 409)
        for generation in (True, None, -1, "0"):
            self.assertEqual(guest.post(base + "/input", json={**action, "generation": generation},
                                        headers=headers).status_code, 400)

    def test_http_page_sources_do_not_reset_other_windows(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        first, second = "1" * 32, "2" * 32
        down = {"type": "mouse_down", "x": 10, "y": 20, "button": "left"}
        self.assertEqual(guest.post(base + "/input", json={**down, "source": first}, headers=headers).status_code, 200)
        self.wait_for(lambda: browser.inputs == [down])
        self.assertEqual(guest.post(base + "/input", json={"type": "reset_input", "source": second},
                                   headers=headers).status_code, 200)
        time.sleep(0.05)
        browser.reset_input.assert_not_called()
        self.assertEqual(guest.post(base + "/input", json={"type": "reset_input", "source": first},
                                   headers=headers).status_code, 200)
        self.wait_for(lambda: browser.reset_input.called)
        for value in (None, [], "bad", "A" * 32, "1" * 33):
            self.assertEqual(guest.post(base + "/input", json={**down, "source": value},
                                        headers=headers).status_code, 400)

    def test_http_sequence_rejects_down_arriving_after_its_reset(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        source = "1" * 32
        reset = {"type": "reset_input", "source": source, "sequence": 2,
                 "generation": runtime.user_generation}
        self.assertEqual(guest.post(base + "/input", json=reset, headers=headers).status_code, 200)
        down = {"type": "mouse_down", "x": 10, "y": 20, "button": "left", "source": source,
                "sequence": 1, "generation": runtime.user_generation}
        self.assertEqual(guest.post(base + "/input", json=down, headers=headers).status_code, 409)
        self.assertEqual(guest.post(base + "/input", json=reset, headers=headers).status_code, 409)
        self.assertEqual(guest.post(base + "/input", json={**down, "sequence": 3}, headers=headers).status_code, 200)
        self.wait_for(lambda: len(browser.inputs) == 1)
        self.assertNotIn("sequence", browser.inputs[0])
        self.assertNotIn("source", browser.inputs[0])
        for sequence in (True, None, 0, -1, "4", 1.5):
            self.assertEqual(guest.post(base + "/input", json={**down, "sequence": sequence},
                                        headers=headers).status_code, 400)

    def test_closed_reopen_gets_new_generation_and_rejects_old_http_input(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        generation = runtime.user_generation
        runtime.submit(self.store.get(task["id"])["controller"])
        runtime.thread.join(2)
        self.manager.reopen(task["id"])
        self.wait_state(task["id"], {"active"})
        replacement = self.manager.runtimes[task["id"]]
        self.assertNotEqual(replacement.user_generation, generation)
        self.assertLess(replacement.user_generation, 2 ** 53)
        action = {"type": "text", "text": "stale", "generation": generation, "source": "1" * 32}
        self.assertEqual(guest.post(base + "/input", json=action, headers=headers).status_code, 409)

    def test_failed_actor_release_retries_without_forgetting_owner(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        owner = self.store.get(task["id"])["controller"]
        runtime.input(owner, {"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        self.wait_for(lambda: len(browser.inputs) == 1)
        browser.reset_input.side_effect = [RuntimeError("temporary"), None]
        runtime.input(owner, {"type": "reset_input"})
        self.wait_for(lambda: browser.reset_input.call_count >= 2)
        self.assertIsNone(runtime.input_owner)
        self.assertTrue(runtime.thread.is_alive())

    def test_failed_initial_down_or_modifier_only_input_still_gets_actor_release_retries(self):
        for kind in ("mouse_down", "mouse_move"):
            task, guest, base, headers, runtime, browser = self.prepare()
            browser.input = Mock(side_effect=RuntimeError("initial input failure"))
            browser.reset_input.side_effect = [RuntimeError("temporary release failure"), None]
            runtime.input(self.store.get(task["id"])["controller"],
                          {"type": kind, "x": 10, "y": 20, "button": "left", "modifiers": ["Shift"]})
            self.wait_for(lambda: browser.reset_input.call_count >= 2)
            self.assertIsNone(runtime.input_owner)
            self.assertTrue(runtime.thread.is_alive())

    def test_permanent_release_failure_fails_closed_and_stops_profile(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        owner = self.store.get(task["id"])["controller"]
        runtime.input(owner, {"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        self.wait_for(lambda: len(browser.inputs) == 1)
        browser.reset_input.side_effect = RuntimeError("cannot release")
        runtime.input(owner, {"type": "reset_input"})
        runtime.thread.join(2)
        self.assertFalse(runtime.thread.is_alive())
        self.assertEqual(self.store.get(task["id"])["state"], "error")
        self.assertIn(task["profile_id"], self.ads.stopped)

    def test_realtime_acceptance_ack_and_error_keep_request_id(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        response, socket = self.control(guest, base, [{"csrf": headers["X-CSRF-Token"]},
            {"type": "input", "id": 1, "action": {"type": "key", "key": "Backspace"}},
            {"type": "input", "id": 2, "action": {"type": "unknown"}}])
        values = [json.loads(value) for value in socket.sent if isinstance(value, str)]
        self.assertIn({"type": "input_ack", "id": 1}, values)
        self.assertTrue(any(value.get("type") == "error" and value.get("id") == 2 for value in values))
        self.assertEqual(response.status_code, 200)

    def test_realtime_id_must_be_positive_integer(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        for identity in (True, 0, -1, "1", None):
            response, socket = self.control(guest, base, [{"csrf": headers["X-CSRF-Token"]},
                {"type": "input", "id": identity, "action": {"type": "text", "text": "bad"}}])
            self.assertFalse(any('"input_ack"' in value for value in socket.sent if isinstance(value, str)))
            self.assertTrue(any('"error"' in value for value in socket.sent if isinstance(value, str)))
        self.assertEqual(browser.inputs, [])

    def test_disconnected_source_pending_input_is_not_replayed(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        with runtime.gate:
            source = runtime.register_input_channel()
            runtime.input(self.store.get(task["id"])["controller"], {"type": "text", "text": "stale"}, source=source)
            runtime.unregister_input_channel(source)
        time.sleep(0.06)
        self.assertEqual(browser.inputs, [])

    def test_stale_close_tab_does_not_erase_saved_browser_tabs(self):
        # Control the first capture too; an already captured default URL could
        # otherwise overwrite the test's saved tabs after gate is released.
        with patch.object(test_tasks.FakeBrowser, "snapshot", return_value=(
                b"frame", [{"url": "about:blank", "active": True}])):
            task, guest, base, headers, runtime, browser = self.prepare()
            saved = [{"url": "https://example.com/keep", "active": True}]
            with runtime.gate:
                runtime.save_tabs(saved)
                source = runtime.register_input_channel()
                runtime.input(self.store.get(task["id"])["controller"],
                              {"type": "close_tab", "id": 1}, source=source)
                runtime.unregister_input_channel(source)
            time.sleep(0.2)
            self.assertEqual(browser.inputs, [])
            self.assertEqual(json.loads(self.store.get(task["id"])["browser_tabs"]), saved)

    def test_disconnect_releases_only_owner_on_actor_thread(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        thread_ids = []
        browser.reset_input.side_effect = lambda: thread_ids.append(threading.get_ident())
        owner = self.store.get(task["id"])["controller"]
        first, second = runtime.register_input_channel(), runtime.register_input_channel()
        runtime.input(owner, {"type": "mouse_down", "x": 10, "y": 20, "button": "left"}, source=second)
        self.wait_for(lambda: len(browser.inputs) == 1)
        runtime.unregister_input_channel(first)
        time.sleep(0.05)
        browser.reset_input.assert_not_called()
        runtime.unregister_input_channel(second)
        self.wait_for(lambda: browser.reset_input.called)
        self.assertEqual(thread_ids, [runtime.thread.ident])

    def test_other_source_reset_and_up_cannot_release_owner_drag(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        owner = self.store.get(task["id"])["controller"]
        first, second = runtime.register_input_channel(), runtime.register_input_channel()
        down = {"type": "mouse_down", "x": 10, "y": 20, "button": "left"}
        runtime.input(owner, down, source=first)
        self.wait_for(lambda: browser.inputs == [down])
        runtime.input(owner, {"type": "reset_input"}, source=second)
        with self.assertRaises(TaskConflict):
            runtime.input(owner, {"type": "mouse_up", "x": 30, "y": 20, "button": "left"}, source=second)
        time.sleep(0.1)
        browser.reset_input.assert_not_called()
        self.assertEqual(browser.inputs, [down])

    def test_idle_lease_releases_mouse_without_waiting_fifteen_seconds(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        owner = self.store.get(task["id"])["controller"]
        runtime.input(owner, {"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        self.wait_for(lambda: len(browser.inputs) == 1)
        with runtime.gate:
            runtime.input_deadline = time.monotonic() - 1
        self.wait_for(lambda: browser.reset_input.called)

    def test_observer_detach_does_not_interrupt_user_drag(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        runtime.attach_admin()
        down = {"type": "mouse_down", "x": 10, "y": 20, "button": "left"}
        runtime.input(self.store.get(task["id"])["controller"], down)
        self.wait_for(lambda: browser.inputs == [down])
        runtime.detach_admin()
        time.sleep(0.08)
        browser.reset_input.assert_not_called()

    def test_expiry_releases_drag_but_keeps_admin_browser_and_cookies(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        runtime.attach_admin()
        runtime.input(self.store.get(task["id"])["controller"],
                      {"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        self.wait_for(lambda: len(browser.inputs) == 1)
        self.store.update(task["id"], expires_at=time.time() - 1)
        self.wait_state(task["id"], {"expired"})
        self.wait_for(lambda: browser.reset_input.called)
        self.assertTrue(runtime.thread.is_alive())
        self.assertFalse(browser.closed)
        self.assertEqual(self.ads.stopped, [])

    def test_reset_invalidates_already_queued_down_before_it_executes(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        owner = self.store.get(task["id"])["controller"]
        with runtime.gate:
            runtime.input(owner, {"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
            runtime.input(owner, {"type": "reset_input"})
        time.sleep(0.1)
        self.assertEqual(browser.inputs, [])
        self.assertIsNone(runtime.input_owner)

    def test_late_old_source_disconnect_does_not_release_new_owner(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        owner = self.store.get(task["id"])["controller"]
        first, second = runtime.register_input_channel(), runtime.register_input_channel()
        down = {"type": "mouse_down", "x": 10, "y": 20, "button": "left"}
        runtime.input(owner, down, source=first)
        self.wait_for(lambda: len(browser.inputs) == 1)
        runtime.unregister_input_channel(first)
        self.wait_for(lambda: browser.reset_input.called)
        runtime.input(owner, down, source=second)
        self.wait_for(lambda: len(browser.inputs) == 2)
        count = browser.reset_input.call_count
        runtime.unregister_input_channel(first)
        time.sleep(0.06)
        self.assertEqual(browser.reset_input.call_count, count)
        self.assertEqual(runtime.input_owner[2], second)

    def test_cancellation_releases_drag_on_actor_before_closing_profile(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        thread_ids = []
        browser.reset_input.side_effect = lambda: thread_ids.append(threading.get_ident())
        runtime.input(self.store.get(task["id"])["controller"],
                      {"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        self.wait_for(lambda: len(browser.inputs) == 1)
        runtime.cancel()
        runtime.thread.join(2)
        self.assertEqual(thread_ids, [runtime.thread.ident])
        self.assertIn(task["profile_id"], self.ads.stopped)

    def test_submit_releases_user_drag_before_admin_can_act(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        runtime.attach_admin()
        runtime.input(self.store.get(task["id"])["controller"],
                      {"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        self.wait_for(lambda: len(browser.inputs) == 1)
        order = []
        browser.reset_input.side_effect = lambda: order.append("release")
        old_input = browser.input
        def record(action):
            order.append("input")
            old_input(action)
        browser.input = record
        runtime.submit(self.store.get(task["id"])["controller"])
        runtime.admin_input({"type": "text", "text": "admin"})
        self.wait_for(lambda: len(order) == 2)
        self.assertEqual(order, ["release", "input"])

    def test_reopen_releases_admin_drag_and_invalidates_queued_inputs(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        runtime.attach_admin()
        runtime.submit(self.store.get(task["id"])["controller"])
        runtime.admin_input({"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        self.wait_for(lambda: len(browser.inputs) == 1)
        with runtime.gate:
            runtime.admin_input({"type": "text", "text": "stale"})
            runtime.reopen_user()
        self.wait_for(lambda: browser.reset_input.called)
        self.assertNotIn({"type": "text", "text": "stale"}, browser.inputs)

    def test_reset_is_not_lost_when_input_queue_is_full(self):
        task, guest, base, headers, runtime, browser = self.prepare()
        owner = self.store.get(task["id"])["controller"]
        runtime.input(owner, {"type": "mouse_down", "x": 10, "y": 20, "button": "left"})
        self.wait_for(lambda: len(browser.inputs) == 1)
        with runtime.gate:
            for _ in range(runtime.commands.maxsize):
                runtime.commands.put_nowait(("input", (runtime.user_generation, {"type": "text", "text": "x"})))
            runtime.input(owner, {"type": "reset_input"})
        self.wait_for(lambda: browser.reset_input.called)


if __name__ == "__main__":
    unittest.main()

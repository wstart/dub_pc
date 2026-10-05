from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from taskapp.store import Store
from taskapp.tasks import TaskConflict, TaskRuntime
from tests.test_tasks import FakeAds, VALUES


class ControlledBrowser:
    def __init__(self, endpoint, url):
        self.inputs = []
        self.release_threads = []
        self.input_hook = lambda action: None
        self.release_hook = lambda: None
        self.snapshot_hook = lambda: None

    def input(self, action):
        self.input_hook(action)
        self.inputs.append(action)

    def reset_input(self):
        self.release_threads.append(threading.get_ident())
        self.release_hook()

    def snapshot(self):
        self.snapshot_hook()
        return b"frame", []

    def close(self):
        pass


class PermissionsLatencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.directory.name) / "tasks.db"))
        self.events = []
        self.workers = []
        self.runtimes = []

    def tearDown(self):
        for event in self.events:
            event.set()
        for worker in self.workers:
            worker.join(3)
        for runtime in self.runtimes:
            runtime.cancel()
            runtime.thread.join(3)
        self.directory.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("Actor did not reach the expected state")

    def prepare(self):
        task = self.store.create(VALUES)
        browsers = []

        def factory(endpoint, url):
            browser = ControlledBrowser(endpoint, url)
            browsers.append(browser)
            return browser

        runtime = TaskRuntime(task["id"], self.store, FakeAds(), factory)
        self.runtimes.append(runtime)
        runtime.start()
        self.wait_for(lambda: self.store.get(task["id"])["state"] == "active")
        runtime.claim("controller")
        source = runtime.register_input_channel()
        return runtime, browsers[0], source

    def hold(self, browser, kind):
        entered, release = threading.Event(), threading.Event()
        self.events.append(release)

        def hook(action):
            if action["type"] == kind and not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("Test did not release browser input")

        browser.input_hook = hook
        return entered, release

    def async_call(self, callback):
        done, failures = threading.Event(), []

        def invoke():
            try:
                callback()
            except Exception as exc:
                failures.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=invoke)
        self.workers.append(worker)
        worker.start()
        return done, failures

    def enqueue(self, runtime, source, action, **kwargs):
        runtime.input("controller", action, source=source, **kwargs)

    def test_blocked_browser_input_does_not_delay_second_enqueue(self):
        runtime, browser, source = self.prepare()
        entered, release = self.hold(browser, "text")
        first, second = {"type": "text", "text": "first"}, {"type": "text", "text": "second"}
        self.enqueue(runtime, source, first)
        self.assertTrue(entered.wait(2))
        done, failures = self.async_call(lambda: self.enqueue(runtime, source, second))
        try:
            self.assertTrue(done.wait(0.3), "Queue acceptance waited for browser execution")
            self.assertEqual(failures, [])
            self.assertEqual(runtime.commands.qsize(), 1)
        finally:
            release.set()
        self.wait_for(lambda: len(browser.inputs) == 2)
        self.assertEqual(browser.inputs, [first, second])

    def test_inflight_down_reset_is_fast_and_releases_on_actor(self):
        runtime, browser, source = self.prepare()
        entered, release = self.hold(browser, "mouse_down")
        self.enqueue(runtime, source, {"type": "mouse_down", "x": 10, "y": 20})
        self.assertTrue(entered.wait(2))
        done, failures = self.async_call(
            lambda: self.enqueue(runtime, source, {"type": "reset_input"}))
        try:
            self.assertTrue(done.wait(0.3), "Safety reset waited for browser execution")
            self.assertEqual(failures, [])
            self.assertEqual(runtime.input_owner, ("user", runtime.user_generation, source))
        finally:
            release.set()
        self.wait_for(lambda: bool(browser.release_threads))
        self.assertIsNone(runtime.input_owner)
        self.assertEqual(set(browser.release_threads), {runtime.thread.ident})

    def test_inflight_down_disconnect_is_fast_and_queued_source_stays_invalid(self):
        runtime, browser, source = self.prepare()
        entered, release = self.hold(browser, "mouse_down")
        with runtime.gate:
            self.enqueue(runtime, source, {"type": "mouse_down", "x": 10, "y": 20})
            self.enqueue(runtime, source, {"type": "mouse_move", "x": 30, "y": 20})
        self.assertTrue(entered.wait(2))
        done, failures = self.async_call(lambda: runtime.unregister_input_channel(source))
        try:
            self.assertTrue(done.wait(0.3), "Disconnect waited for browser execution")
            self.assertEqual(failures, [])
        finally:
            release.set()
        self.wait_for(lambda: bool(browser.release_threads) and runtime.commands.empty())
        self.assertEqual([action["type"] for action in browser.inputs], ["mouse_down"])
        self.assertIsNone(runtime.input_owner)
        self.assertEqual(set(browser.release_threads), {runtime.thread.ident})
        with self.assertRaises(TaskConflict):
            self.enqueue(runtime, source, {"type": "text", "text": "stale"})

    def test_other_source_reset_does_not_end_inflight_drag_or_observer_release_it(self):
        runtime, browser, source = self.prepare()
        runtime.attach_admin()
        other = runtime.register_input_channel()
        entered, release = self.hold(browser, "mouse_down")
        self.enqueue(runtime, source, {"type": "mouse_down", "x": 10, "y": 20})
        self.assertTrue(entered.wait(2))
        done, failures = self.async_call(
            lambda: self.enqueue(runtime, other, {"type": "reset_input"}))
        try:
            self.assertTrue(done.wait(0.3), "Other source reset waited for browser execution")
            self.assertEqual(failures, [])
        finally:
            release.set()
        self.wait_for(lambda: bool(browser.inputs))
        runtime.detach_admin()
        self.assertEqual(browser.release_threads, [])
        self.assertEqual(runtime.input_owner, ("user", runtime.user_generation, source))
        with self.assertRaises(TaskConflict):
            runtime.admin_input({"type": "text", "text": "observer"}, source=other)

    def test_submit_waits_for_inflight_input_then_drops_pending_commands(self):
        runtime, browser, source = self.prepare()
        entered, release = self.hold(browser, "text")
        captured, capture_release = threading.Event(), threading.Event()
        self.events.append(capture_release)

        def snapshot():
            if browser.inputs:
                captured.set()
                capture_release.wait(5)

        browser.snapshot_hook = snapshot
        first = {"type": "text", "text": "inflight"}
        with runtime.gate:
            self.enqueue(runtime, source, first)
            self.enqueue(runtime, source, {"type": "text", "text": "pending"})
        self.assertTrue(entered.wait(2))
        done, failures = self.async_call(lambda: runtime.submit("controller"))
        self.assertFalse(done.wait(0.1), "Completion did not wait for in-flight input")
        self.assertEqual(self.store.get(runtime.task_id)["state"], "active")
        release.set()
        self.assertTrue(done.wait(2))
        self.assertEqual(failures, [])
        capture_release.set()
        runtime.thread.join(2)
        self.assertEqual(self.store.get(runtime.task_id)["state"], "completed")
        self.assertEqual(browser.inputs, [first])
        self.assertTrue(runtime.commands.empty())
        with self.assertRaises(TaskConflict):
            self.enqueue(runtime, source, {"type": "text", "text": "completed"})

    def test_reset_arriving_during_release_preserves_new_epoch_marker(self):
        runtime, browser, source = self.prepare()
        # Pause the actor outside both input locks for a deterministic manual flush.
        capture_entered, capture_release = threading.Event(), threading.Event()
        self.events.append(capture_release)

        def snapshot():
            capture_entered.set()
            capture_release.wait(5)

        browser.snapshot_hook = snapshot
        self.assertTrue(capture_entered.wait(2))
        owner = ("user", runtime.user_generation, source)
        runtime.input_owner, runtime.input_buttons = owner, {"left"}
        runtime.input_deadline = time.monotonic() + 15
        self.enqueue(runtime, source, {"type": "reset_input"})
        release_entered, release_finish = threading.Event(), threading.Event()
        self.events.append(release_finish)

        def release_hook():
            release_entered.set()
            release_finish.wait(5)

        browser.release_hook = release_hook

        def flush():
            with runtime.gate:
                runtime._flush_input_release(browser)

        flushed, flush_failures = self.async_call(flush)
        self.assertTrue(release_entered.wait(2))
        done, failures = self.async_call(
            lambda: self.enqueue(runtime, source, {"type": "reset_input"}))
        try:
            self.assertTrue(done.wait(0.3), "New release marker waited for slow release RPC")
            self.assertEqual(failures, [])
        finally:
            release_finish.set()
        self.assertTrue(flushed.wait(2))
        self.assertEqual(flush_failures, [])
        self.assertEqual(runtime.input_epochs[owner], 2)
        self.assertIn(owner, runtime.input_release_owners)

    def test_reopen_waits_for_admin_input_and_rejects_old_generation(self):
        runtime, browser, source = self.prepare()
        runtime.attach_admin()
        runtime.submit("controller")
        generation = runtime.admin_generation
        entered, release = self.hold(browser, "text")
        capture_release = threading.Event()
        self.events.append(capture_release)
        browser.snapshot_hook = lambda: capture_release.wait(5) if browser.inputs else None
        first = {"type": "text", "text": "admin inflight"}
        with runtime.gate:
            runtime.admin_input(first, source=source, generation=generation)
            runtime.admin_input({"type": "text", "text": "admin pending"}, source=source,
                                generation=generation)
        self.assertTrue(entered.wait(2))
        done, failures = self.async_call(runtime.reopen_user)
        self.assertFalse(done.wait(0.1))
        release.set()
        self.assertTrue(done.wait(2))
        self.assertEqual(failures, [])
        capture_release.set()
        self.assertEqual(self.store.get(runtime.task_id)["state"], "active")
        self.assertEqual(browser.inputs, [first])
        self.assertTrue(runtime.commands.empty())
        with self.assertRaises(TaskConflict):
            runtime.admin_input({"type": "text", "text": "old"}, source=source,
                                generation=generation)
        with self.assertRaises(TaskConflict):
            self.enqueue(runtime, source, {"type": "text", "text": "old"},
                         generation=runtime.user_generation - 1)

    def test_cancel_waits_for_inflight_down_then_releases_and_closes(self):
        runtime, browser, source = self.prepare()
        entered, release = self.hold(browser, "mouse_down")
        self.enqueue(runtime, source, {"type": "mouse_down", "x": 10, "y": 20})
        self.assertTrue(entered.wait(2))
        done, failures = self.async_call(runtime.cancel)
        self.assertFalse(done.wait(0.1))
        self.assertEqual(self.store.get(runtime.task_id)["state"], "active")
        release.set()
        self.assertTrue(done.wait(2))
        self.assertEqual(failures, [])
        runtime.thread.join(2)
        self.assertFalse(runtime.thread.is_alive())
        self.assertEqual(self.store.get(runtime.task_id)["state"], "cancelled")
        self.assertTrue(browser.release_threads)
        self.assertEqual(set(browser.release_threads), {runtime.thread.ident})

    def test_failed_inflight_down_reset_retains_owner_until_release_retry(self):
        runtime, browser, source = self.prepare()
        entered, release = self.hold(browser, "mouse_down")
        original = browser.input_hook

        def failed_input(action):
            original(action)
            raise RuntimeError("failed down")

        browser.input_hook = failed_input
        released = []

        def transient_release():
            released.append(1)
            if len(released) == 1:
                raise RuntimeError("temporary release failure")

        browser.release_hook = transient_release
        self.enqueue(runtime, source, {"type": "mouse_down", "x": 10, "y": 20})
        self.assertTrue(entered.wait(2))
        done, failures = self.async_call(
            lambda: self.enqueue(runtime, source, {"type": "reset_input"}))
        try:
            self.assertTrue(done.wait(0.3))
            self.assertEqual(failures, [])
        finally:
            release.set()
        self.wait_for(lambda: len(released) >= 2 and runtime.input_owner is None)
        self.assertEqual(set(browser.release_threads), {runtime.thread.ident})

    def test_only_slow_input_logs_timing_without_payload_or_source(self):
        runtime, browser, source = self.prepare()
        entered, release = self.hold(browser, "text")
        secret = "private-text-and-cookie-token"
        with patch("taskapp.tasks.logger.warning") as warning:
            self.enqueue(runtime, source, {"type": "text", "text": secret})
            self.assertTrue(entered.wait(2))
            time.sleep(1.01)
            release.set()
            self.wait_for(lambda: bool(warning.call_args_list))
            slow_calls = [item for item in warning.call_args_list
                          if "event=input_slow" in item.args[0]]
            self.assertEqual(len(slow_calls), 1)
            self.assertEqual(slow_calls[0].args[1:4], (runtime.task_id, "user", "text"))
            self.assertGreaterEqual(slow_calls[0].args[4], 1)
            self.assertNotIn(secret, str(slow_calls))
            self.assertNotIn(str(source), str(slow_calls))
            self.enqueue(runtime, source, {"type": "text", "text": "fast"})
            self.wait_for(lambda: len(browser.inputs) == 2)
            self.assertEqual(len(warning.call_args_list), 1)


if __name__ == "__main__":
    unittest.main()

"""Opt-in ACK latency regressions using isolated Gunicorn and fake profiles.

Run with TASKROOM_ACK_LATENCY=1.  No AdsPower service or real task is used.
The two fault-injection checks are expected to be red until the input control
path no longer waits for browser execution or a blocked binary frame send.
"""
from __future__ import annotations

import http.cookiejar
import json
import multiprocessing
import os
import re
import socket
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from urllib import request
from unittest.mock import patch

import flask_sock
from gunicorn.app.base import BaseApplication
from simple_websocket import Client, Server as WebSocketServer

from taskapp import create_app
from taskapp.tasks import TaskConflict
from tests import test_tasks
from tests.test_tasks import FakeAds, FakeBrowser


class LocalClient(Client):
    def handshake(self):
        # simple-websocket 1.1.0 omits the nondefault port from Host.
        # Browsers retain it; use the browser-equivalent header in this probe.
        self.host = f"{self.host}:{self.port}"
        return super().handshake()


def serve_ack_probe(database, port, mode, entered_input, release_input,
                    entered_frame, release_frame, received_second):
    class CaptureBrowser(FakeBrowser):
        def input(self, action):
            if action.get("text") == "hold-browser":
                entered_input.set()
                release_input.wait(8)
            super().input(action)

    class ProbeSocket(WebSocketServer):
        def receive(self, timeout=None):
            raw = super().receive(timeout)
            if isinstance(raw, str):
                try:
                    value = json.loads(raw)
                except ValueError:
                    value = {}
                if value.get("type") == "input" and value.get("id") == 2:
                    received_second.set()
            return raw

        def send(self, value):
            if mode == "slow-frame" and isinstance(value, bytes) and not entered_frame.is_set():
                entered_frame.set()
                release_frame.wait(8)
            return super().send(value)

    class ProbeServer(BaseApplication):
        def load_config(self):
            for key, value in {"bind": f"127.0.0.1:{port}", "workers": 1,
                               "threads": 12, "timeout": 30, "loglevel": "warning"}.items():
                self.cfg.set(key, value)

        def load(self):
            # Initialize system SQLite after the Gunicorn fork.  macOS' system
            # SQLite can otherwise crash in os_log on its first worker thread.
            return create_app({"SECRET_KEY": "isolated-ack-secret",
                               "ADMIN_PASSWORD": "isolated-ack-admin",
                               "DATABASE": database}, ads=FakeAds(), browser_factory=CaptureBrowser)

    with patch.object(flask_sock, "Server", ProbeSocket):
        ProbeServer().run()


@unittest.skipUnless(os.environ.get("TASKROOM_ACK_LATENCY") == "1",
                     "Opt-in isolated Gunicorn ACK latency probes")
class ControlAckLatencyTests(unittest.TestCase):
    @contextmanager
    def control(self, mode):
        context = multiprocessing.get_context("spawn")
        events = {name: context.Event() for name in (
            "entered_input", "release_input", "entered_frame", "release_frame", "received_second")}
        with tempfile.TemporaryDirectory(prefix="taskroom-ack-latency-") as directory:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            origin = f"http://127.0.0.1:{port}"
            process = context.Process(target=serve_ack_probe, args=(
                str(Path(directory) / "tasks.db"), port, mode, *events.values()))
            process.start()
            channel = None
            try:
                for _ in range(100):
                    try:
                        with request.urlopen(origin + "/health", timeout=0.5):
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail("Isolated Gunicorn did not start")

                admin_jar, guest_jar = http.cookiejar.CookieJar(), http.cookiejar.CookieJar()
                admin = request.build_opener(request.HTTPCookieProcessor(admin_jar))
                guest = request.build_opener(request.HTTPCookieProcessor(guest_jar))

                def csrf(opener, path):
                    return re.search(r'name="csrf-token" content="([^"]+)"',
                                     opener.open(origin + path, timeout=3).read().decode())[1]

                def post(opener, path, token, data):
                    return json.load(opener.open(request.Request(
                        origin + path, data=json.dumps(data).encode(), headers={
                            "Content-Type": "application/json", "X-CSRF-Token": token}), timeout=3))

                admin_csrf = csrf(admin, "/")
                post(admin, "/api/login", admin_csrf, {"password": "isolated-ack-admin"})
                task = post(admin, "/api/admin/tasks", admin_csrf, {
                    "title": "isolated ACK probe", "start_url": "https://example.com",
                    "lifetime_minutes": 5})["task"]
                base = "/api/task/" + task["token"]
                guest_csrf = csrf(guest, "/t/" + task["token"])
                for _ in range(100):
                    if json.load(guest.open(origin + base, timeout=3))["task"]["state"] == "active":
                        break
                    time.sleep(0.02)
                post(guest, base + "/claim", guest_csrf, {})
                cookies = "; ".join(f"{cookie.name}={cookie.value}" for cookie in guest_jar)
                channel = LocalClient.connect(origin.replace("http://", "ws://") + base + "/control",
                                              headers={"Origin": origin, "Cookie": cookies})
                channel.send(json.dumps({"type": "auth", "csrf": guest_csrf}))
                self.assertIsNotNone(self.wait_message(channel, "state", None, 2))
                yield channel, events
            finally:
                events["release_input"].set()
                events["release_frame"].set()
                if channel is not None:
                    channel.close()
                process.terminate()
                process.join(10)
                self.assertFalse(process.is_alive(), "Own isolated server did not stop")

    def wait_message(self, channel, kind, identifier, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = channel.receive(timeout=max(0, deadline - time.monotonic()))
            if isinstance(raw, bytes):
                channel.send(json.dumps({"type": "frame_ack"}))
            elif isinstance(raw, str):
                value = json.loads(raw)
                if value.get("type") == kind and (identifier is None or value.get("id") == identifier):
                    return value
                if value.get("type") == "error":
                    self.fail(f"Unexpected isolated control error: {value}")
            else:
                return None
        return None

    def send_text(self, channel, identifier, text):
        channel.send(json.dumps({"type": "input", "id": identifier,
                                 "action": {"type": "text", "text": text}}))

    def test_baseline_input_ack_is_prompt(self):
        with self.control("baseline") as (channel, events):
            started = time.monotonic()
            self.send_text(channel, 2, "fast")
            self.assertIsNotNone(self.wait_message(channel, "input_ack", 2, 0.5))
            elapsed = time.monotonic() - started
            print(json.dumps({"ack_probe": "baseline", "ack_seconds": round(elapsed, 3)},
                             ensure_ascii=False), flush=True)
            self.assertLess(elapsed, 0.5)

    def test_browser_execution_must_not_delay_queue_acceptance_ack(self):
        with self.control("hold-browser") as (channel, events):
            self.send_text(channel, 1, "hold-browser")
            self.assertIsNotNone(self.wait_message(channel, "input_ack", 1, 2))
            self.assertTrue(events["entered_input"].wait(2))
            started = time.monotonic()
            self.send_text(channel, 2, "next-input")
            before_release = self.wait_message(channel, "input_ack", 2, 5.2)
            read_before_release = events["received_second"].is_set()
            events["release_input"].set()
            after_release = before_release or self.wait_message(channel, "input_ack", 2, 2)
            elapsed = time.monotonic() - started
            print(json.dumps({"ack_probe": "browser-gate", "ack_seconds": round(elapsed, 3),
                              "ack_before_release": before_release is not None,
                              "application_read_before_release": read_before_release,
                              "ack_after_release": after_release is not None},
                             ensure_ascii=False), flush=True)
            self.assertIsNotNone(after_release)
            self.assertLess(elapsed, 0.5, "Queue-acceptance ACK waited on synchronous browser execution")

    def test_binary_send_must_not_delay_input_ack(self):
        with self.control("slow-frame") as (channel, events):
            self.assertTrue(events["entered_frame"].wait(2))
            started = time.monotonic()
            self.send_text(channel, 2, "next-input")
            before_release = self.wait_message(channel, "input_ack", 2, 5.2)
            read_before_release = events["received_second"].is_set()
            events["release_frame"].set()
            after_release = before_release or self.wait_message(channel, "input_ack", 2, 2)
            elapsed = time.monotonic() - started
            print(json.dumps({"ack_probe": "binary-send-fault-injection", "ack_seconds": round(elapsed, 3),
                              "ack_before_release": before_release is not None,
                              "application_read_before_release": read_before_release,
                              "ack_after_release": after_release is not None},
                             ensure_ascii=False), flush=True)
            self.assertIsNotNone(after_release)
            self.assertLess(elapsed, 0.5, "Input ACK waited on a blocked frame send in the same loop")


class InputQueueLatencyTests(unittest.TestCase):
    setUp = test_tasks.TaskTests.setUp
    tearDown = test_tasks.TaskTests.tearDown
    wait_state = test_tasks.TaskTests.wait_state
    new_task = test_tasks.TaskTests.new_task

    @contextmanager
    def paused_capture(self):
        task, client, base, headers = self.new_task()
        runtime = self.manager.runtimes[task["id"]]
        entered, release = threading.Event(), threading.Event()
        browser = self.browsers[-1]
        original = browser.snapshot

        def snapshot():
            entered.set()
            release.wait(8)
            return original()

        browser.snapshot = snapshot
        try:
            self.assertTrue(entered.wait(2), "Fake capture did not enter its gate-free wait")
            yield runtime, "a" * 32
        finally:
            browser.snapshot = original
            release.set()

    def queue_types(self, runtime):
        with runtime.commands.mutex:
            return [payload[1]["type"] for kind, payload in runtime.commands.queue]

    def queue_moves(self, runtime):
        with runtime.commands.mutex:
            return [payload[1]["x"] for kind, payload in runtime.commands.queue]

    def test_move_flood_keeps_latest_tail_without_filling_queue(self):
        with self.paused_capture() as (runtime, source):
            for index in range(runtime.commands.maxsize + 2):
                runtime.input(self.store.get(runtime.task_id)["controller"],
                              {"type": "mouse_move", "x": index, "y": 20}, source=source)
            self.assertEqual(self.queue_types(runtime), ["mouse_move"])
            with runtime.commands.mutex:
                self.assertEqual(runtime.commands.queue[-1][1][1]["x"], runtime.commands.maxsize + 1)

    def test_move_flood_does_not_drop_or_delay_discrete_input_barriers(self):
        with self.paused_capture() as (runtime, source):
            controller = self.store.get(runtime.task_id)["controller"]
            runtime.input(controller, {"type": "mouse_down", "x": 10, "y": 20}, source=source)
            for index in range(runtime.commands.maxsize + 2):
                runtime.input(controller, {"type": "mouse_move", "x": index, "y": 20}, source=source)
            runtime.input(controller, {"type": "text", "text": "do-not-drop"}, source=source)
            runtime.input(controller, {"type": "mouse_move", "x": 90, "y": 20}, source=source)
            runtime.input(controller, {"type": "mouse_up", "x": 100, "y": 20}, source=source)
            self.assertEqual(self.queue_types(runtime), ["mouse_down", "mouse_move", "text", "mouse_move", "mouse_up"])

    def test_hover_flood_does_not_reject_a_following_click(self):
        with self.paused_capture() as (runtime, source):
            controller = self.store.get(runtime.task_id)["controller"]
            for index in range(runtime.commands.maxsize):
                runtime.input(controller, {"type": "mouse_move", "x": index, "y": 20}, source=source)
            runtime.input(controller, {"type": "click", "x": 100, "y": 20}, source=source)
            self.assertEqual(self.queue_types(runtime), ["mouse_move", "click"])

    def test_different_sources_are_move_merge_barriers(self):
        with self.paused_capture() as (runtime, source):
            controller = self.store.get(runtime.task_id)["controller"]
            for identity, coordinate in ((source, 10), (source, 11), ("b" * 32, 20),
                                         ("b" * 32, 21), (source, 30), (source, 31)):
                runtime.input(controller, {"type": "mouse_move", "x": coordinate, "y": 20}, source=identity)
            self.assertEqual(self.queue_moves(runtime), [11, 21, 31])

    def test_reset_epoch_is_a_move_merge_barrier_and_sequences_stay_monotonic(self):
        with self.paused_capture() as (runtime, source):
            controller = self.store.get(runtime.task_id)["controller"]
            for sequence in (1, 2):
                runtime.input(controller, {"type": "mouse_move", "x": sequence, "y": 20},
                              source=source, sequence=sequence)
            with self.assertRaises(TaskConflict):
                runtime.input(controller, {"type": "mouse_move", "x": 9, "y": 20}, source=source, sequence=1)
            runtime.input(controller, {"type": "reset_input"}, source=source, sequence=3)
            runtime.input(controller, {"type": "mouse_move", "x": 4, "y": 20}, source=source, sequence=4)
            self.assertEqual(self.queue_moves(runtime), [2, 4])
            with runtime.commands.mutex:
                self.assertEqual([payload[3] for kind, payload in runtime.commands.queue], [0, 1])

    def test_modifier_changes_are_move_merge_barriers(self):
        with self.paused_capture() as (runtime, source):
            controller = self.store.get(runtime.task_id)["controller"]
            for index, modifiers in enumerate(([], [], ["Shift"], ["Shift"], ["Meta"], ["Meta"])):
                runtime.input(controller, {"type": "mouse_move", "x": index, "y": 20,
                                          "modifiers": modifiers}, source=source)
            self.assertEqual(self.queue_moves(runtime), [1, 3, 5])


if __name__ == "__main__":
    unittest.main()

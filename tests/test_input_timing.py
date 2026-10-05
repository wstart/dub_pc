"""Opt-in execution-lock diagnostic using only a temporary DB and FakeBrowser.

Run directly as a module.  This measures the existing product, not a proposed fix.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import inspect
import json
import multiprocessing
import queue
import re
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib import error, request
from unittest.mock import patch

import flask_sock
from gunicorn.app.base import BaseApplication
from simple_websocket import ConnectionClosed, Server as WebSocketServer

from taskapp import create_app
from taskapp.tasks import TaskRuntime
from tests.test_control_ack_latency import LocalClient
from tests.test_tasks import FakeAds, FakeBrowser


def serve(database, port, events, entered, release, move_seconds):
    class TimedGate:
        def __init__(self, original):
            self.original = original

        def __enter__(self):
            if not self.original.acquire(blocking=False):
                started = time.monotonic()
                callers = [item.function for item in inspect.stack()[1:5]]
                events.put({"event": "gate_wait_start", "at": started, "callers": callers})
                self.original.acquire()
                events.put({"event": "gate_wait_end", "at": time.monotonic(),
                            "wait_seconds": time.monotonic() - started, "callers": callers})
            return self

        def __exit__(self, *args):
            self.original.release()

    original_init = TaskRuntime.__init__

    def init_runtime(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.gate = TimedGate(self.gate)

    class CaptureBrowser(FakeBrowser):
        def input(self, action):
            started = time.monotonic()
            events.put({"event": "browser_input_start", "at": started, "action": action})
            if action.get("text") == "owned-hold":
                entered.set()
                release.wait(12)
            elif action["type"] == "mouse_move" and move_seconds:
                time.sleep(move_seconds)
            super().input(action)
            events.put({"event": "browser_input_end", "at": time.monotonic(),
                        "seconds": time.monotonic() - started, "action": action})

    class ProbeSocket(WebSocketServer):
        def receive(self, timeout=None):
            raw = super().receive(timeout)
            if isinstance(raw, str):
                value = json.loads(raw)
                if value.get("type") == "input":
                    events.put({"event": "application_receive", "at": time.monotonic(),
                                "id": value.get("id")})
            return raw

        def send(self, value):
            if isinstance(value, str):
                data = json.loads(value)
                if data.get("type") in {"input_ack", "error"}:
                    events.put({"event": "application_reply", "at": time.monotonic(),
                                "id": data.get("id"), "kind": data["type"]})
            return super().send(value)

    class Server(BaseApplication):
        def load_config(self):
            for key, value in {"bind": f"127.0.0.1:{port}", "workers": 1,
                               "threads": 12, "timeout": 30, "loglevel": "warning"}.items():
                self.cfg.set(key, value)

        def load(self):
            app = create_app({"SECRET_KEY": "isolated-timing-secret",
                              "ADMIN_PASSWORD": "isolated-timing-admin",
                              "DATABASE": database}, ads=FakeAds(), browser_factory=CaptureBrowser)

            @app.get("/__owned_timing_state")
            def state_without_runtime_gate():
                # Diagnostic-only route in this fake server, never installed in production.
                task = app.extensions["task_store"].list()[0]
                runtime = app.extensions["task_manager"].runtimes[task["id"]]
                return {"state": task["state"], "queued": runtime.commands.qsize()}

            return app

    with patch.object(TaskRuntime, "__init__", init_runtime), patch.object(flask_sock, "Server", ProbeSocket):
        Server().run()


def case(label, interval=0.033, hold_seconds=0, move_seconds=0, completion=False):
    context = multiprocessing.get_context("spawn")
    events, entered, release = context.Queue(), context.Event(), context.Event()
    with tempfile.TemporaryDirectory(prefix="taskroom-input-timing-fake-") as directory:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        process = context.Process(target=serve, args=(str(Path(directory) / "tasks.db"), port,
                                                     events, entered, release, move_seconds))
        process.start()
        channel = None
        send_lock = threading.Lock()
        sender = None
        try:
            for _ in range(100):
                try:
                    with request.urlopen(origin + "/health", timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.05)
            jars = [http.cookiejar.CookieJar(), http.cookiejar.CookieJar()]
            admin, guest = [request.build_opener(request.HTTPCookieProcessor(jar)) for jar in jars]

            def csrf(opener, path):
                return re.search(r'name="csrf-token" content="([^"]+)"',
                                 opener.open(origin + path, timeout=3).read().decode())[1]

            def post(opener, path, token, data):
                return json.load(opener.open(request.Request(origin + path, data=json.dumps(data).encode(),
                    headers={"Content-Type": "application/json", "X-CSRF-Token": token}), timeout=12))

            admin_csrf = csrf(admin, "/")
            post(admin, "/api/login", admin_csrf, {"password": "isolated-timing-admin"})
            task = post(admin, "/api/admin/tasks", admin_csrf, {"title": "owned timing diagnostic",
                "start_url": "https://example.com", "lifetime_minutes": 5})["task"]
            base = "/api/task/" + task["token"]
            guest_csrf = csrf(guest, "/t/" + task["token"])
            for _ in range(100):
                if json.load(guest.open(origin + base, timeout=3))["task"]["state"] == "active":
                    break
                time.sleep(0.02)
            post(guest, base + "/claim", guest_csrf, {})
            cookies = "; ".join(f"{cookie.name}={cookie.value}" for cookie in jars[1])
            channel = LocalClient.connect(origin.replace("http://", "ws://") + base + "/control",
                headers={"Origin": origin, "Cookie": cookies})
            channel.send(json.dumps({"csrf": guest_csrf, "frame_chunks": True}))
            messages, sent, frame_id, frame_offset = [], {}, None, 0

            def receive(timeout):
                nonlocal frame_id, frame_offset
                raw = channel.receive(timeout=timeout)
                if isinstance(raw, bytes):
                    frame_offset += len(raw)
                    with send_lock:
                        channel.send(json.dumps({"type": "frame_ack", "id": frame_id, "offset": frame_offset}))
                elif isinstance(raw, str):
                    value = json.loads(raw)
                    if value["type"] == "frame_start":
                        frame_id, frame_offset = value["id"], 0
                    messages.append({**value, "at": time.monotonic()})
                    return value
                return None

            deadline = time.monotonic() + 3
            state = None
            while time.monotonic() < deadline and state is None:
                value = receive(0.1)
                if value and value["type"] == "state":
                    state = value["task"]
            assert state, "Own control channel did not authenticate"
            generation = state["stream_generation"]

            def send(identifier, action):
                sent[identifier] = time.monotonic()
                with send_lock:
                    channel.send(json.dumps({"type": "input", "id": identifier,
                        "action": {**action, "source": "a" * 32, "generation": generation, "sequence": identifier}}))

            if hold_seconds:
                send(1, {"type": "text", "text": "owned-hold"})
                while not any(value.get("type") == "input_ack" and value.get("id") == 1 for value in messages):
                    receive(0.1)
                assert entered.wait(2), "Own fake input never began"
            started = time.monotonic()
            request_state, submit_result = None, {}
            if completion:
                def submit():
                    submit_result["started"] = time.monotonic()
                    submit_result["response"] = post(guest, base + "/submit", guest_csrf, {})
                    submit_result["finished"] = time.monotonic()

                sender = threading.Thread(target=submit)
                sender.start()
                time.sleep(0.2)
                request_state = json.load(request.urlopen(origin + "/__owned_timing_state", timeout=2))
            else:
                def sample():
                    for index in range(100):
                        send(index + 2, {"type": "mouse_move", "x": 100 + index, "y": 100})
                        time.sleep(interval)

                sender = threading.Thread(target=sample)
                sender.start()
            release_at = started + hold_seconds
            deadline = started + max(18, hold_seconds + 5)
            max_queued = 0
            while time.monotonic() < deadline:
                if time.monotonic() >= release_at:
                    release.set()
                if completion:
                    if "finished" in submit_result:
                        break
                    time.sleep(0.02)
                else:
                    receive(0.05)
                    acknowledgements = {value.get("id") for value in messages if value["type"] == "input_ack"}
                    if all(identifier in acknowledgements for identifier in range(2, 102)):
                        break
                max_queued = max(max_queued, json.load(request.urlopen(
                    origin + "/__owned_timing_state", timeout=2))["queued"])
            release.set()
            sender.join(3)
            telemetry = []
            while True:
                try:
                    telemetry.append(events.get(timeout=0.1))
                except queue.Empty:
                    break
            receipts = [{"id": value["id"], "delay_seconds": value["at"] - sent[value["id"]]}
                        for value in messages if value["type"] == "input_ack" and value["id"] in sent]
            result = {"label": label, "sample_count": 0 if completion else 100,
                      "sample_interval_seconds": interval, "fake_first_input_hold_seconds": hold_seconds,
                      "fake_move_execution_seconds": move_seconds, "receipts": receipts,
                      "max_ack_seconds": max((value["delay_seconds"] for value in receipts if value["id"] != 1), default=None),
                      "ack_over_5s": sum(value["delay_seconds"] >= 5 for value in receipts if value["id"] != 1),
                      "max_commands_queued": max_queued, "telemetry": telemetry,
                      "production_database_opened": False, "real_adspower_used": False}
            if completion:
                result["state_200ms_after_submit_started"] = request_state["state"]
                result["submit_seconds"] = submit_result["finished"] - submit_result["started"]
                result["state_after_submit_returned"] = json.load(request.urlopen(
                    origin + "/__owned_timing_state", timeout=2))["state"]
                try:
                    post(guest, base + "/input", guest_csrf, {"type": "text", "text": "must-not-execute"})
                except error.HTTPError as exc:
                    result["input_status_after_completion"] = exc.code
                else:
                    raise AssertionError("Post-completion input was not rejected")
            else:
                assert len([value for value in receipts if value["id"] != 1]) == 100, result
            print("INPUT_TIMING_CASE " + json.dumps({key: value for key, value in result.items()
                if key not in {"receipts", "telemetry"}}, ensure_ascii=False), flush=True)
            return result
        finally:
            release.set()
            if sender:
                sender.join(3)
            if channel:
                try:
                    channel.close()
                except ConnectionClosed:
                    pass
            process.terminate()
            process.join(10)
            assert not process.is_alive()
            events.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"source_hashes": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (Path("taskapp/__init__.py"), Path("taskapp/tasks.py"), Path("taskapp/static/app.js"))},
        "gunicorn_threads": 12, "cases": []}
    scenarios = [("normal-33ms", {}), ("gate-held-33ms", {"hold_seconds": 6.2}),
                 ("gate-held-20ms", {"hold_seconds": 6.2, "interval": 0.02}),
                 ("submit-while-gate-held", {"hold_seconds": 6.2, "completion": True})]
    for label, values in scenarios:
        report["cases"].append(case(label, **values))
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("INPUT_TIMING_DIAGNOSTIC_COMPLETE: isolated fake data only", flush=True)


if __name__ == "__main__":
    main()

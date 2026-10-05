"""Green permission/actor timing checks; only fake profiles and temporary DBs."""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import queue
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib import error, request
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from tests import test_input_timing as red
from tests.test_control_ack_latency import LocalClient
from tests.test_tasks import FakeBrowser


def serve(database, port, events, entered, release, image):
    original_factory = red.create_app

    class ImageBrowser(FakeBrowser):
        def snapshot(self):
            return image, [{"id": 1, "index": 0, "url": "https://example.com", "active": True}]

    def factory(*args, **kwargs):
        app = original_factory(*args, **kwargs)

        @app.get("/__owned_green_queue")
        def pending():
            task = app.extensions["task_store"].list()[0]
            runtime = app.extensions["task_manager"].runtimes[task["id"]]
            with runtime.commands.mutex:
                actions = [dict(payload[1]) for kind, payload in runtime.commands.queue]
            return {"state": task["state"], "pending": actions}

        return app

    # Reuse the frozen red harness without changing any of its source files.
    with patch.object(red, "create_app", factory), patch.object(red, "FakeBrowser", ImageBrowser):
        red.serve(database, port, events, entered, release, 0)


@contextmanager
def isolated(image):
    import http.cookiejar
    import re

    context = multiprocessing.get_context("spawn")
    events, entered, release = context.Queue(), context.Event(), context.Event()
    with tempfile.TemporaryDirectory(prefix="taskroom-timing-green-fake-") as directory:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        process = context.Process(target=serve, args=(str(Path(directory) / "tasks.db"),
                                                     port, events, entered, release, image))
        process.start()
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

            def post(path, data, opener=None, token=None):
                return json.load((opener or guest).open(request.Request(origin + path,
                    data=json.dumps(data).encode(), headers={"Content-Type": "application/json",
                    "X-CSRF-Token": token or guest_csrf}), timeout=12))

            admin_csrf = csrf(admin, "/")
            post("/api/login", {"password": "isolated-timing-admin"}, admin, admin_csrf)
            task = post("/api/admin/tasks", {"title": "owned green timing fixture",
                "start_url": "https://example.com", "lifetime_minutes": 5}, admin, admin_csrf)["task"]
            base = "/api/task/" + task["token"]
            path = "/t/" + task["token"]
            guest_csrf = csrf(guest, path)
            for _ in range(100):
                if json.load(guest.open(origin + base, timeout=3))["task"]["state"] == "active":
                    break
                time.sleep(0.02)
            post(base + "/claim", {})
            yield {"origin": origin, "base": base, "path": path, "csrf": guest_csrf,
                   "jar": jars[1], "post": post, "events": events, "entered": entered,
                   "release": release}
        finally:
            release.set()
            process.terminate()
            process.join(10)
            assert not process.is_alive()
            events.close()


def drain(events):
    values = []
    while True:
        try:
            values.append(events.get(timeout=0.1))
        except queue.Empty:
            return values


def pending(server):
    return json.load(request.urlopen(server["origin"] + "/__owned_green_queue", timeout=2))


def raw_case(image, hold=False, completion=False):
    with isolated(image) as server:
        cookies = "; ".join(f"{value.name}={value.value}" for value in server["jar"])
        channel = LocalClient.connect(server["origin"].replace("http://", "ws://") + server["base"] + "/control",
            headers={"Origin": server["origin"], "Cookie": cookies})
        channel.send(json.dumps({"csrf": server["csrf"], "frame_chunks": True}))
        messages, sent, frame_id, offset = [], {}, None, 0

        def receive(timeout=0.05):
            nonlocal frame_id, offset
            raw = channel.receive(timeout=timeout)
            if isinstance(raw, bytes):
                offset += len(raw)
                channel.send(json.dumps({"type": "frame_ack", "id": frame_id, "offset": offset}))
            elif isinstance(raw, str):
                value = json.loads(raw)
                if value["type"] == "frame_start":
                    frame_id, offset = value["id"], 0
                messages.append({**value, "at": time.monotonic()})
                return value
            return None

        def ack(identifier):
            return next((value for value in messages if value["type"] == "input_ack"
                         and value.get("id") == identifier), None)

        try:
            deadline = time.monotonic() + 3
            state = None
            while time.monotonic() < deadline and state is None:
                value = receive()
                if value and value["type"] == "state":
                    state = value["task"]
            assert state

            def send(identifier, action):
                sent[identifier] = time.monotonic()
                channel.send(json.dumps({"type": "input", "id": identifier, "action": {
                    **action, "source": "a" * 32, "generation": state["stream_generation"], "sequence": identifier}}))

            if hold:
                send(1, {"type": "text", "text": "owned-hold"})
                deadline = time.monotonic() + 0.5
                while ack(1) is None and time.monotonic() < deadline:
                    receive()
                assert ack(1), "First admission ACK was not prompt"
                assert server["entered"].wait(2)
            started = time.monotonic()
            submit_result = {}
            submit_thread = None
            if completion:
                send(2, {"type": "text", "text": "owned-old-pending"})
                deadline = time.monotonic() + 0.5
                while ack(2) is None and time.monotonic() < deadline:
                    receive()
                assert ack(2), "Pending admission waited for browser execution"

                def submit():
                    submit_result["started"] = time.monotonic()
                    server["post"](server["base"] + "/submit", {})
                    submit_result["finished"] = time.monotonic()

                submit_thread = threading.Thread(target=submit)
                submit_thread.start()
                while time.monotonic() < started + 6.2:
                    time.sleep(0.02)
                assert "finished" not in submit_result, "Submit incorrectly bypassed in-flight operation"
                server["release"].set()
                submit_thread.join(3)
                assert "finished" in submit_result
                try:
                    server["post"](server["base"] + "/input", {"type": "text", "text": "owned-new-after-submit"})
                except error.HTTPError as exc:
                    rejected = exc.code
                else:
                    rejected = 200
                assert rejected == 409
                time.sleep(0.15)
            else:
                for index in range(100):
                    send(index + 2, {"type": "mouse_move", "x": 100 + index, "y": 100})
                    due = time.monotonic() + 0.02
                    while time.monotonic() < due:
                        receive(min(0.01, max(0, due - time.monotonic())))
                deadline = time.monotonic() + 0.5
                while not all(ack(identifier) for identifier in range(2, 102)) and time.monotonic() < deadline:
                    receive()
                assert all(ack(identifier) for identifier in range(2, 102))
                sampled_pending = pending(server)
                if hold:
                    assert not server["release"].is_set()
                    assert time.monotonic() < started + 6.2
                    assert sampled_pending["pending"][-1].get("x") == 199
                    while time.monotonic() < started + 6.2:
                        receive()
                    server["release"].set()
                time.sleep(0.2)
            telemetry = drain(server["events"])
            receipts = [{"id": value["id"], "seconds": value["at"] - sent[value["id"]]}
                        for value in messages if value["type"] == "input_ack" and value["id"] in sent]
            assert all(value["seconds"] < 0.5 for value in receipts), receipts
            if hold:
                actor = next(value for value in telemetry if value["event"] == "browser_input_end"
                             and value["action"].get("text") == "owned-hold")
                assert actor["seconds"] >= 6.2, actor
            result = {"kind": "submit-inflight" if completion else "slow-browser-100x20ms" if hold else "normal-100x20ms",
                      "raw_websocket": True, "receipts": receipts, "max_ack_seconds": max(value["seconds"] for value in receipts),
                      "actor_hold_seconds": actor["seconds"] if hold else 0, "telemetry": telemetry}
            if completion:
                after = [value for value in telemetry if value["event"] == "browser_input_start"
                         and value["at"] >= submit_result["finished"]]
                assert not after, after
                result.update(submit_seconds=submit_result["finished"] - submit_result["started"],
                    after_completion_status=rejected, inputs_executed_after_submit_returned=after,
                    old_pending_executed_before_return=any(value["event"] == "browser_input_start"
                        and value["action"].get("text") == "owned-old-pending" for value in telemetry))
            else:
                result.update(sample_count=100, sample_interval_seconds=0.02, pending_after_samples=sampled_pending)
            return result
        finally:
            server["release"].set()
            try:
                channel.close()
            except red.ConnectionClosed:
                pass


def frontend_case(image):
    with isolated(image) as server, sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        context.add_cookies([{"name": cookie.name, "value": cookie.value, "url": server["origin"]}
                            for cookie in server["jar"]])
        context.add_init_script("""(() => {
            window.ownedInputTimeline=[];let order=0;const Native=WebSocket;
            window.WebSocket=class extends Native{
                constructor(...args){super(...args);this.addEventListener('message',event=>{
                    if(typeof event.data==='string'){const item=JSON.parse(event.data);
                        if(item.type==='input_ack')window.ownedInputTimeline.push({kind:'ack',id:item.id,at:performance.now(),order:++order});}});}
                send(raw){const item=JSON.parse(raw);if(item.type==='input')
                    window.ownedInputTimeline.push({kind:'sent',id:item.id,action:item.action,at:performance.now(),order:++order});
                    return super.send(raw);}
            };
        })();""")
        page = context.new_page()
        try:
            page.goto(server["origin"] + server["path"], wait_until="domcontentloaded")
            page.wait_for_function("() => !document.querySelector('#remote-text').disabled&&document.querySelector('#screen').naturalWidth===1280")
            page.locator("#remote-text").fill("owned-hold")
            page.locator('#text-input button[type="submit"]').click()
            assert server["entered"].wait(2)
            started = time.monotonic()
            bounds = page.locator("#screen").bounding_box()
            page.mouse.move(bounds["x"] + 80, bounds["y"] + 140)
            page.mouse.down()
            for index in range(100):
                page.mouse.move(bounds["x"] + 85 + index * 5, bounds["y"] + 140)
                page.wait_for_timeout(20)
            page.mouse.up()
            page.wait_for_function("() => {const items=window.ownedInputTimeline;const up=items.find(item=>item.kind==='sent'&&item.action.type==='mouse_up');return up&&items.some(item=>item.kind==='ack'&&item.id===up.id);}", timeout=2000)
            timeline = page.evaluate("window.ownedInputTimeline")
            active, maximum = set(), 0
            for value in timeline:
                if value["kind"] == "sent" and value["action"]["type"] == "mouse_move":
                    active.add(value["id"])
                    maximum = max(maximum, len(active))
                elif value["kind"] == "ack":
                    active.discard(value["id"])
            assert maximum <= 1, timeline
            up = next(value for value in timeline if value["kind"] == "sent" and value["action"]["type"] == "mouse_up")
            up_ack = next(value for value in timeline if value["kind"] == "ack" and value["id"] == up["id"])
            assert (up_ack["at"] - up["at"]) / 1000 < 0.5
            assert not page.locator("#notice").inner_text()
            while time.monotonic() < started + 6.2:
                page.wait_for_timeout(50)
            server["release"].set()
            page.wait_for_timeout(200)
            telemetry = drain(server["events"])
            applied = [value["action"]["type"] for value in telemetry if value["event"] == "browser_input_start"]
            assert applied.count("mouse_down") == applied.count("mouse_up") == 1, applied
            assert applied.index("mouse_down") < applied.index("mouse_up"), applied
            return {"kind": "real-frontend-100x20ms", "timeline": timeline, "max_move_ack_inflight": maximum,
                    "up_ack_seconds": (up_ack["at"] - up["at"]) / 1000,
                    "native_sample_count": 100, "applied_types": applied, "telemetry": telemetry}
        finally:
            server["release"].set()
            browser.close()


def main():
    from tests.smoke_input_ack import images

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image, _ = images()
    report = {"source_hashes": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (Path("taskapp/__init__.py"), Path("taskapp/tasks.py"), Path("taskapp/static/app.js"),
                     Path("tests/smoke_input_timing.py"), Path("tests/test_input_timing.py"))},
        "production_database_opened": False, "real_adspower_used": False, "cases": []}
    for function in (lambda: raw_case(image), lambda: raw_case(image, hold=True),
                     lambda: raw_case(image, hold=True, completion=True), lambda: frontend_case(image)):
        result = function()
        report["cases"].append(result)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print("INPUT_TIMING_GREEN_CASE " + json.dumps({key: value for key, value in result.items()
            if key not in {"receipts", "telemetry", "timeline"}}, ensure_ascii=False), flush=True)
    print("INPUT_TIMING_GREEN_COMPLETE: raw and real frontend; isolated fake data only", flush=True)


if __name__ == "__main__":
    main()

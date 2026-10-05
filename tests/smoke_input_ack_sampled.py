"""Isolated ACK head-of-line diagnostic; no real AdsPower or production DB."""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import multiprocessing
import queue
import re
import socket
import struct
import tempfile
import threading
import time
from pathlib import Path
from urllib import request

from gunicorn.app.base import BaseApplication
from playwright.sync_api import sync_playwright

from taskapp import create_app
from tests.test_tasks import FakeAds, FakeBrowser


class SlowDownstream:
    """Transparent TCP relay, limiting only upgraded WS server-to-client bytes."""

    def __init__(self, upstream_port: int, bytes_per_second: int):
        self.upstream_port = upstream_port
        self.bytes_per_second = bytes_per_second
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.stopped = threading.Event()
        self.connections = []
        self.ws_bytes = 0
        self.ws_started = None
        self.thread = threading.Thread(target=self.accept, daemon=True)

    def start(self):
        self.thread.start()

    def accept(self):
        while not self.stopped.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.relay, args=(client,), daemon=True).start()

    def relay(self, client):
        upstream = None
        try:
            upstream = socket.create_connection(("127.0.0.1", self.upstream_port), timeout=5)
            upstream.settimeout(None)
            self.connections.extend((client, upstream))
            opening = b""
            while b"\r\n\r\n" not in opening:
                block = client.recv(4096)
                if not block:
                    return
                opening += block
            websocket = b"upgrade: websocket" in opening.lower()
            upstream.sendall(opening)

            def upload():
                try:
                    while not self.stopped.is_set():
                        data = client.recv(65536)
                        if not data:
                            return
                        upstream.sendall(data)
                except OSError:
                    pass
                finally:
                    try:
                        upstream.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            threading.Thread(target=upload, daemon=True).start()
            if websocket:
                headers = b""
                while b"\r\n\r\n" not in headers:
                    block = upstream.recv(4096)
                    if not block:
                        return
                    headers += block
                header, pending = headers.split(b"\r\n\r\n", 1)
                client.sendall(header + b"\r\n\r\n")
                self.ws_started = time.monotonic()
            else:
                pending = b""
            due = time.monotonic()
            while not self.stopped.is_set():
                data = pending or upstream.recv(4096 if websocket else 65536)
                pending = b""
                if not data:
                    return
                if websocket:
                    for offset in range(0, len(data), 4096):
                        part = data[offset:offset + 4096]
                        if self.stopped.wait(max(0, due - time.monotonic())):
                            return
                        client.sendall(part)
                        self.ws_bytes += len(part)
                        due = max(due, time.monotonic()) + len(part) / self.bytes_per_second
                else:
                    client.sendall(data)
        except OSError:
            pass
        finally:
            client.close()
            if upstream:
                upstream.close()

    def close(self):
        self.stopped.set()
        self.listener.close()
        for connection in self.connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self.thread.join(2)


def serve(database, port, image, events):
    from simple_websocket import Server as WebSocketServer

    original_send = WebSocketServer.send

    def measured_send(self, data):
        kind = "binary" if isinstance(data, bytes) else json.loads(data).get("type")
        value = {"phase": "send_start", "kind": kind, "at": time.monotonic(),
                 "bytes": len(data), "id": None if isinstance(data, bytes) else json.loads(data).get("id")}
        events.put(value)
        try:
            return original_send(self, data)
        finally:
            events.put({**value, "phase": "send_end", "at": time.monotonic()})

    WebSocketServer.send = measured_send

    class CaptureBrowser(FakeBrowser):
        def input(self, action):
            super().input(action)
            events.put({"phase": "browser_input", "kind": action["type"],
                        "at": time.monotonic(), "action": action})

        def snapshot(self):
            return image, [{"id": 1, "index": 0, "url": "https://example.com", "active": True}]

    class Server(BaseApplication):
        def load_config(self):
            for key, value in {"bind": f"127.0.0.1:{port}", "workers": 1,
                               "threads": 12, "timeout": 30, "loglevel": "warning"}.items():
                self.cfg.set(key, value)

        def load(self):
            return create_app({"SECRET_KEY": "isolated-ack-secret",
                               "ADMIN_PASSWORD": "isolated-ack-admin",
                               "DATABASE": database}, ads=FakeAds(), browser_factory=CaptureBrowser)

    Server().run()


def images():
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.set_content('<body style="margin:0"><canvas width="1280" height="800"></canvas></body>')
        small = page.screenshot(type="jpeg", quality=70)
        page.evaluate("""() => {
            const canvas=document.querySelector('canvas'), context=canvas.getContext('2d');
            const value=context.createImageData(1280,800); let seed=123456789;
            for(let i=0;i<value.data.length;i+=4){
                for(let j=0;j<3;j++){seed=(Math.imul(seed,1664525)+1013904223)|0;
                    value.data[i+j]=(seed>>>24)&255;}
                value.data[i+3]=255;
            }
            context.putImageData(value,0,0);
        }""")
        large = page.screenshot(type="jpeg", quality=70)
        browser.close()
    return small, large


def run_case(label, image, rate, operation="text", verify_frame=False, hover_count=0, output=None):
    with tempfile.TemporaryDirectory(prefix="taskroom-ack-fake-") as directory:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        events = multiprocessing.get_context("spawn").Queue()
        process = multiprocessing.get_context("spawn").Process(
            target=serve, args=(str(Path(directory) / "tasks.db"), port, image, events))
        process.start()
        proxy = SlowDownstream(port, rate)
        proxy.start()
        direct = f"http://127.0.0.1:{port}"
        origin = f"http://127.0.0.1:{proxy.port}"
        try:
            for _ in range(100):
                try:
                    with request.urlopen(direct + "/health", timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.05)
            admin_jar, guest_jar = http.cookiejar.CookieJar(), http.cookiejar.CookieJar()
            admin = request.build_opener(request.HTTPCookieProcessor(admin_jar))
            guest = request.build_opener(request.HTTPCookieProcessor(guest_jar))

            def csrf(opener, path):
                return re.search(r'name="csrf-token" content="([^"]+)"',
                                 opener.open(direct + path).read().decode())[1]

            def post(opener, path, csrf_value, data):
                return json.load(opener.open(request.Request(
                    direct + path, data=json.dumps(data).encode(),
                    headers={"Content-Type": "application/json", "X-CSRF-Token": csrf_value})))

            admin_csrf = csrf(admin, "/")
            post(admin, "/api/login", admin_csrf, {"password": "isolated-ack-admin"})
            task = post(admin, "/api/admin/tasks", admin_csrf, {
                "title": "isolated ACK bandwidth diagnostic", "start_url": "https://example.com",
                "lifetime_minutes": 5})["task"]
            path, base = "/t/" + task["token"], "/api/task/" + task["token"]
            guest_csrf = csrf(guest, path)
            for _ in range(100):
                if json.load(guest.open(direct + base))["task"]["state"] == "active":
                    break
                time.sleep(0.05)
            post(guest, base + "/claim", guest_csrf, {})
            with sync_playwright() as driver:
                browser = driver.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context()
                context.add_cookies([{"name": cookie.name, "value": cookie.value, "url": origin}
                                    for cookie in guest_jar])
                context.add_init_script("""(() => {
                    window.ackDiagnostic={sent:[],received:[],binary:[],frameAcks:[],handshakes:[],assembled:[]};
                    let frameId=null;
                    window.ackFrameBlobs=new Map();const createUrl=URL.createObjectURL;
                    URL.createObjectURL=blob=>{const url=createUrl.call(URL,blob);
                        window.ackDiagnostic.assembled.push({id:frameId,size:blob.size,at:performance.now()});
                        window.ackFrameBlobs.set(url,blob);return url;};
                    const Native=window.WebSocket;
                    window.WebSocket=class extends Native{
                        constructor(...args){super(...args);this.addEventListener('message',event=>{
                            if(typeof event.data==='string'){
                                const item=JSON.parse(event.data);
                                if(item.type==='frame_start')frameId=item.id;
                                window.ackDiagnostic.received.push({type:item.type,id:item.id,size:item.size,at:performance.now()});
                            }else window.ackDiagnostic.binary.push({bytes:event.data.byteLength,at:performance.now()});
                        });}
                        send(data){const item=JSON.parse(data);if(item.type==='input')
                            window.ackDiagnostic.sent.push({id:item.id,type:item.action.type,at:performance.now()});
                            else if(item.type==='frame_ack')window.ackDiagnostic.frameAcks.push({id:item.id,offset:item.offset,at:performance.now()});
                            else if(Object.hasOwn(item,'csrf'))window.ackDiagnostic.handshakes.push({frame_chunks:item.frame_chunks===true,at:performance.now()});
                            return super.send(data);}
                    };
                })();""")
                page = context.new_page()
                page.goto(origin + path, wait_until="domcontentloaded")
                page.wait_for_function("() => !document.querySelector('#remote-text').disabled")
                # Wait until some WS payload is already on the downlink, not a mock sleep.
                deadline = time.monotonic() + 3
                while proxy.ws_bytes < min(16384, len(image)) and time.monotonic() < deadline:
                    page.wait_for_timeout(20)
                expected_click = None
                if operation == "click":
                    page.wait_for_function("() => document.querySelector('#screen').naturalWidth===1280")
                    bounds = page.locator("#screen").bounding_box()
                    for index in range(hover_count):
                        page.mouse.move(bounds["x"] + 100 + index % 20 * 8,
                                        bounds["y"] + 120 + index % 7 * 5)
                        page.wait_for_timeout(20)
                    expected_click = page.locator("#screen").evaluate("""image => {
                        const rect=image.getBoundingClientRect();
                        return {x:80*1280/rect.width,y:80*800/rect.height};
                    }""")
                    page.locator("#screen").click(position={"x": 80, "y": 80})
                else:
                    page.locator("#remote-text").fill("owned ACK diagnostic")
                    page.locator('#text-input button[type="submit"]').click()
                observed_at = time.monotonic()
                timed_out = False
                deadline = observed_at + (45 if operation == "click" else 30)
                while time.monotonic() < deadline:
                    diagnostic = page.evaluate("window.ackDiagnostic")
                    expected = "mouse_down" if operation == "click" else "text"
                    sent = next((item for item in diagnostic["sent"] if item["type"] == expected), None)
                    ack = next((item for item in diagnostic["received"]
                                if item["type"] == "input_ack" and sent and item.get("id") == sent["id"]), None)
                    timed_out |= "输入确认超时" in page.locator("#notice").inner_text()
                    relevant = [item for item in diagnostic["sent"]
                                if item["type"] in ({"mouse_down", "mouse_up"} if operation == "click" else {"text"})]
                    complete = relevant and all(any(value["type"] == "input_ack"
                        and value.get("id") == item["id"] for value in diagnostic["received"])
                        for item in relevant)
                    if ack and complete:
                        break
                    page.wait_for_timeout(50)
                assert sent, "Real frontend did not send input"
                decoded = None
                if verify_frame:
                    page.wait_for_function("() => {const image=document.querySelector('#screen');"
                                           "return image.complete&&image.naturalWidth===1280&&image.naturalHeight===800;}")
                    decoded = page.evaluate("""async () => {
                        const image=document.querySelector('#screen');
                        const bytes=await window.ackFrameBlobs.get(image.src).arrayBuffer();
                        const digest=await crypto.subtle.digest('SHA-256',bytes);
                        return {width:image.naturalWidth,height:image.naturalHeight,bytes:bytes.byteLength,
                            sha256:Array.from(new Uint8Array(digest),value=>value.toString(16).padStart(2,'0')).join('')};
                    }""")
                    decoded["matches_original_jpeg"] = decoded["sha256"] == hashlib.sha256(image).hexdigest()
                    assert decoded["matches_original_jpeg"], "Chunk reassembly did not preserve JPEG bytes"
                    png = page.locator("#screen").screenshot(type="png")
                    assert png[:8] == b"\x89PNG\r\n\x1a\n", "Decoded screen screenshot is not PNG"
                    decoded["png_dimensions"] = struct.unpack(">II", png[16:24])
                    screenshot = output.parent / (label + "-" + operation + "-screen.png")
                    screenshot.write_bytes(png)
                    decoded["png_path"] = str(screenshot)
                    decoded["png_sha256"] = hashlib.sha256(png).hexdigest()
                    diagnostic = page.evaluate("window.ackDiagnostic")
                    assert any(item["frame_chunks"] for item in diagnostic["handshakes"])
                    assert any(item["type"] == "frame_start" and item["size"] == len(image)
                               for item in diagnostic["received"])
                    assert diagnostic["binary"] and all(item["bytes"] <= 16384
                                                        for item in diagnostic["binary"])
                telemetry = []
                while True:
                    try:
                        telemetry.append(events.get(timeout=0.1))
                    except queue.Empty:
                        break
                input_ack = next((item for item in telemetry if item["kind"] == "input_ack"
                                  and item["phase"] == "send_start" and item["id"] == sent["id"]), None)
                receipts = []
                for item in diagnostic["sent"]:
                    receipt = next((value for value in diagnostic["received"]
                        if value["type"] == "input_ack" and value.get("id") == item["id"]), None)
                    receipts.append({**item, "received_ms": receipt["at"] if receipt else None,
                        "delay_seconds": (receipt["at"] - item["at"]) / 1000 if receipt else None})
                completed_frames = []
                for item in diagnostic["assembled"]:
                    start = next((value for value in diagnostic["received"]
                        if value["type"] == "frame_start" and value.get("id") == item["id"]), None)
                    if start:
                        completed_frames.append({"id": item["id"], "bytes": item["size"],
                            "seconds": (item["at"] - start["at"]) / 1000})
                applied = [item["kind"] for item in telemetry if item["phase"] == "browser_input"]
                if operation == "click":
                    assert applied.count("mouse_down") == applied.count("mouse_up") == 1, applied
                    assert applied.index("mouse_down") < applied.index("mouse_up"), applied
                    assert all(kind == "mouse_move" for kind in applied[:applied.index("mouse_down")]), applied
                    down = next(item["action"] for item in telemetry
                        if item["phase"] == "browser_input" and item["kind"] == "mouse_down")
                    up = next(item["action"] for item in telemetry
                        if item["phase"] == "browser_input" and item["kind"] == "mouse_up")
                    assert all(abs(down[axis] - expected_click[axis]) < 1 for axis in ("x", "y")), down
                    assert all(abs(up[axis] - down[axis]) < 1 for axis in ("x", "y")), (down, up)
                result = {"label": label, "width": 1280, "height": 800, "operation": operation,
                          "hover_count": hover_count, "applied_input_order": applied,
                          "expected_click_coordinates": expected_click,
                          "input_receipts": receipts, "decoded_frame": decoded,
                          "completed_frames": completed_frames,
                          "jpeg_quality": 70, "jpeg_bytes": len(image),
                          "downstream_bytes_per_second": rate, "upstream_limited": False,
                          "input_id": sent["id"], "input_sent_browser_ms": sent["at"],
                          "ack_received_browser_ms": ack["at"] if ack else None,
                          "ack_delay_seconds": (ack["at"] - sent["at"]) / 1000 if ack else None,
                          "frontend_timeout_seen": timed_out,
                          "local_draft_after_receipt": page.locator("#remote-text").input_value(),
                          "server_ack_attempt_seconds_after_ui_click":
                              input_ack["at"] - observed_at if input_ack else None,
                          "telemetry": telemetry, "browser_messages": diagnostic,
                          "proxy_ws_bytes": proxy.ws_bytes,
                          "production_database_opened": False, "real_adspower_used": False}
                browser.close()
                print("ACK_DIAGNOSTIC_CASE " + json.dumps({key: value for key, value in result.items()
                      if key not in {"telemetry", "browser_messages"}}, ensure_ascii=False), flush=True)
                return result
        finally:
            proxy.close()
            process.terminate()
            process.join(10)
            assert not process.is_alive()
            events.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect-timeout", action="store_true")
    parser.add_argument("--operation", choices=("text", "click"), default="text")
    parser.add_argument("--only-slow", action="store_true")
    parser.add_argument("--verify-frame", action="store_true")
    parser.add_argument("--hover-count", type=int, default=0)
    parser.add_argument("--max-ack-seconds", type=float)
    args = parser.parse_args()
    small, large = images()
    report = {"source_hashes": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in (Path("taskapp/__init__.py"), Path("taskapp/static/app.js"),
                           Path("taskapp/tasks.py"), Path("tests/smoke_input_ack_sampled.py"))},
              "gunicorn": {"workers": 1, "threads": 12}, "cases": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cases = [("small-64KiB", small, 65536), ("large-256KiB", large, 262144),
             ("large-64KiB", large, 65536)]
    if args.only_slow:
        cases = cases[-1:]
    for label, image, rate in cases:
        report["cases"].append(run_case(label, image, rate, args.operation,
            args.verify_frame, args.hover_count, args.output))
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if args.max_ack_seconds is not None:
            receipts = report["cases"][-1]["input_receipts"]
            assert receipts and all(item["delay_seconds"] is not None
                and item["delay_seconds"] < args.max_ack_seconds for item in receipts), receipts
    if not args.only_slow:
        assert not report["cases"][0]["frontend_timeout_seen"], "Small-frame control must not time out"
    slow = report["cases"][-1]
    if args.expect_timeout:
        assert slow["frontend_timeout_seen"], "Expected timeout was not reproduced"
        assert slow["ack_delay_seconds"] and slow["ack_delay_seconds"] > 5
    else:
        assert not slow["frontend_timeout_seen"], "Slow downlink input ACK timed out"
    print("ACK_DIAGNOSTIC_COMPLETE " + json.dumps({"timeout_reproduced": slow["frontend_timeout_seen"],
          "cases": len(report["cases"]), "production_database_opened": False,
          "real_adspower_used": False}), flush=True)


if __name__ == "__main__":
    main()

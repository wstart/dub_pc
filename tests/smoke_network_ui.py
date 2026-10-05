"""Network warning and compact UI acceptance in one owned, isolated profile."""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler

from taskapp import create_app
from taskapp.browser import AdsPower
from tests.smoke_admin_browser import wait


def main():
    config = dict(line.split("=", 1) for line in Path("/etc/adspower-headless.env").read_text().splitlines()
                  if "=" in line)
    requests, healthy, errors, frames = Counter(), threading.Event(), [], []
    shots = Path(os.environ["TASKROOM_UI_SHOTS"])
    shots.mkdir(parents=True, exist_ok=True)

    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            requests[path] += 1
            status, kind = 200, "text/html; charset=utf-8"
            body = "<!doctype html><meta charset=utf-8><h1>独立测试页面</h1><input id=field value='保留输入'>"
            if path == "/broken":
                body += "<link rel=stylesheet href='/style.css'><script src='/missing.js?token=OWN_SECRET'></script><img src='/image.png'><script>fetch('/api/data?token=OWN_SECRET')</script>"
            elif path == "/images":
                body += "<img src='/image.png'>"
            elif path == "/main-error":
                status = 503
            elif path in {"/style.css", "/missing.js", "/api/data", "/image.png"}:
                status = 200 if healthy.is_set() else 503 if path != "/image.png" else 404
                kind = {"/style.css": "text/css", "/missing.js": "text/javascript",
                        "/api/data": "application/json", "/image.png": "image/png"}[path]
                body = {"/style.css": "body { color: #203c32; }", "/missing.js": "window.fixtureReady = true;",
                        "/api/data": "{}", "/image.png": ""}[path]
            encoded = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2ioAAAAASUVORK5CYII=") if path == "/image.png" else body.encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except BrokenPipeError:
                pass

        def log_message(self, *args):
            pass

    class OwnedAds(AdsPower):
        def __init__(self):
            super().__init__("http://127.0.0.1:50325", config["ADSPOWER_API_KEY"])
            self.profiles, self.endpoints = [], []

        def create(self, task):
            profile = super().create(task)
            self.profiles.append(profile)
            return profile

        def start(self, profile, restore_tabs=False):
            endpoint = super().start(profile, restore_tabs)
            self.endpoints.append(endpoint)
            return endpoint

    class QuietRequests(WSGIRequestHandler):
        def log_request(self, *args):
            pass

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{fixture.server_port}"
    ads = OwnedAds()
    with tempfile.TemporaryDirectory(prefix="taskroom-network-owned-") as directory:
        app = create_app({"TESTING": True, "SECRET_KEY": "isolated-warning-secret",
                          "ADMIN_PASSWORD": "isolated-warning-admin",
                          "DATABASE": str(Path(directory) / "tasks.db")}, ads=ads)
        manager, store = app.extensions["task_manager"], app.extensions["task_store"]
        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietRequests)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        ui_origin = f"http://127.0.0.1:{server.server_port}"
        admin, guest = app.test_client(), app.test_client()
        admin.get("/")
        with admin.session_transaction() as session:
            admin_headers = {"X-CSRF-Token": session["csrf"]}
        admin.post("/api/login", json={"password": "isolated-warning-admin"}, headers=admin_headers)
        try:
            task = manager.create({"title": "这是一个较长的任务名称，用来验证任务说明收起后浏览器仍靠上且手机布局不会被长文字撑开" * 2,
                                   "start_url": origin + "/start", "lifetime_minutes": 5})
            wait(lambda: store.get(task["id"])["state"] in {"active", "error"}, "own profile startup")
            assert store.get(task["id"])["state"] == "active"
            base = "/api/task/" + task["token"]
            guest.get("/t/" + task["token"])
            with guest.session_transaction() as session:
                guest_headers = {"X-CSRF-Token": session["csrf"]}
            guest.post(base + "/claim", json={}, headers=guest_headers)
            runtime = manager.runtimes[task["id"]]
            with sync_playwright() as driver:
                remote = driver.chromium.connect_over_cdp(ads.endpoints[-1])
                remote_context = remote.contexts[0]
                target = next(page for page in remote_context.pages if page.url == origin + "/start")
                remote_context.add_cookies([{"name": "preserve_network_test", "value": "owned-cookie",
                                            "url": origin, "expires": time.time() + 3600}])
                ui_browser = driver.chromium.launch(headless=True, args=["--no-sandbox"])

                def view(client, path):
                    context = ui_browser.new_context(viewport={"width": 1440, "height": 1000})
                    context.add_cookies([{"name": "session", "value": client.get_cookie("session").value,
                                          "url": ui_origin}])
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("websocket", lambda ws: ws.on("framereceived", lambda value: frames.append(value)))
                    page.goto(ui_origin + path, wait_until="domcontentloaded")
                    expect(page.locator("#new-tab")).to_be_enabled()
                    return page

                page = view(guest, "/t/" + task["token"])
                expect(page.locator("#connection-state")).to_have_text("实时连接")
                expect(page.locator("#screen")).to_have_attribute("src", re.compile(r"^blob:"))
                assert any(isinstance(value, bytes) and value.startswith(b"\xff\xd8") for value in frames)
                layout = {}
                for name, viewport in (("desktop", {"width": 1440, "height": 1000}),
                                       ("mobile", {"width": 390, "height": 844})):
                    page.set_viewport_size(viewport)
                    before = page.locator(".viewport").bounding_box()
                    assert before["y"] < 180, before
                    address, go = page.locator("#address").bounding_box(), page.locator("#navigate button").bounding_box()
                    assert abs(address["height"] - go["height"]) < 1
                    assert abs(address["y"] - go["y"]) < 1
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.locator("#task-details summary").click()
                    expect(page.locator("#task-details .session-info")).to_be_visible()
                    assert abs(page.locator(".viewport").bounding_box()["y"] - before["y"]) < 1
                    page.locator("#task-details summary").click()
                    page.screenshot(path=str(shots / ("compact-" + name + ".png")), full_page=True)
                    layout[name] = round(before["y"], 1)
                print("COMPACT_LAYOUT_OK", json.dumps(layout), "description opens without moving viewport", flush=True)
                page.set_viewport_size({"width": 1440, "height": 1000})

                def navigate(path):
                    page.locator("#address").fill(origin + path)
                    page.locator("#navigate button").click()
                    target.wait_for_url(origin + path)

                navigate("/broken")
                expect(page.locator("#network-alert")).to_be_visible()
                expect(page.locator("#network-alert")).to_have_attribute("data-level", "warning")
                expect(page.locator("#network-message")).to_contain_text("可能影响")
                wait(lambda: runtime.network_warning and runtime.network_warning["count"] >= 4, "script/style/api/image failures")
                page.locator("#network-alert summary").click()
                assert "HTTP 503" in page.locator("#network-errors").inner_text()
                assert "OWN_SECRET" not in page.locator("#network-errors").inner_text()
                page.screenshot(path=str(shots / "network-warning.png"), full_page=True)
                snapshot = dict(requests)
                page.wait_for_timeout(1600)
                assert snapshot == dict(requests), "Unexpected automatic retry"
                assert store.get(task["id"])["state"] == "active"
                print("PARTIAL_WARNING_OK: scripts/styles/API/image errors; no automatic retry; private query excluded", flush=True)
                healthy.set()
                page.locator("#reload").click()
                expect(page.locator("#network-alert")).to_be_hidden()
                wait(lambda: runtime.network_warning is None, "manual refresh clears resolved warnings")
                target.wait_for_function("window.fixtureReady === true")
                assert any(cookie["name"] == "preserve_network_test" for cookie in remote_context.cookies())
                print("MANUAL_REFRESH_OK: warning cleared, script loaded, Cookie preserved", flush=True)

                healthy.clear()
                navigate("/images")
                expect(page.locator("#network-alert")).to_have_attribute("data-level", "info")
                page.locator("#new-tab").click()
                expect(page.locator("#address")).to_have_value("")
                expect(page.locator("#network-alert")).to_be_hidden()
                page.locator("#tabs .active .tab-close").click()
                expect(page.locator("#address")).to_have_value(origin + "/images")
                expect(page.locator("#network-alert")).to_have_attribute("data-level", "info")
                navigate("/main-error")
                expect(page.locator("#network-alert")).to_have_attribute("data-level", "error")
                assert store.get(task["id"])["state"] == "active"
                print("TAB_AND_MAIN_ERROR_OK: minor summary; per-tab isolation; HTTP 503 main page remains operable", flush=True)

                with socket.socket() as unused:
                    unused.bind(("127.0.0.1", 0))
                    closed_port = unused.getsockname()[1]
                page.locator("#address").fill(f"http://127.0.0.1:{closed_port}/unreachable")
                page.locator("#navigate button").click()
                expect(page.locator("#network-alert")).to_have_attribute("data-level", "error")
                wait(lambda: runtime.network_warning and any(item["reason"] == "net::ERR_CONNECTION_REFUSED"
                     for item in runtime.network_warning["details"]), "main document connection failure")
                assert store.get(task["id"])["state"] == "active"
                assert not page.locator("#reload").is_disabled()
                print("MAIN_NETWORK_FAILURE_OK: browser still operable and refresh available", flush=True)
                navigate("/start")
                expect(page.locator("#network-alert")).to_be_hidden()

                fallback = ui_browser.new_context()
                fallback.add_cookies([{"name": "session", "value": guest.get_cookie("session").value, "url": ui_origin}])
                fallback_page = fallback.new_page()
                fallback_page.route_web_socket("**/control", lambda ws: None)
                fallback_page.goto(ui_origin + "/t/" + task["token"], wait_until="domcontentloaded")
                expect(fallback_page.locator("#connection-state")).to_have_text("兼容连接", timeout=10000)
                navigate("/broken")
                expect(fallback_page.locator("#network-alert")).to_have_attribute("data-level", "warning")
                fallback_page.close()
                print("HTTP_FALLBACK_WARNING_OK", flush=True)
                assert not errors, errors
                ui_browser.close()
            runtime.cancel()
            runtime.thread.join(20)
            assert not runtime.thread.is_alive()
            initial = manager.create({"title": "初始网址不可达测试", "lifetime_minutes": 5,
                                      "start_url": f"http://127.0.0.1:{closed_port}/initial-failure"})
            wait(lambda: store.get(initial["id"])["state"] in {"active", "error"}, "initial failed navigation")
            assert store.get(initial["id"])["state"] == "active"
            initial_runtime = manager.runtimes[initial["id"]]
            wait(lambda: initial_runtime.network_warning is not None, "initial network alert")
            assert initial_runtime.network_warning["level"] == "error"
            print("INITIAL_NAVIGATION_FAILURE_OK: new task remains active with explicit network alert", flush=True)
        finally:
            for runtime in manager.runtimes.values():
                runtime.cancel()
                runtime.thread.join(20)
                assert not runtime.thread.is_alive(), "Own profile still running; do not delete"
            for profile in ads.profiles:
                ads.delete(profile)
            server.shutdown()
            server.server_close()
            fixture.shutdown()
            fixture.server_close()
            print("OWN_NETWORK_PROFILE_REMOVED; production database untouched", flush=True)


if __name__ == "__main__":
    main()

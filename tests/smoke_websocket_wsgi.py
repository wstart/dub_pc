"""Opt-in WebSocket check with the production Gunicorn thread configuration."""
from __future__ import annotations

import http.cookiejar
import json
import multiprocessing
import re
import socket
import tempfile
import time
from pathlib import Path
from urllib import request

from gunicorn.app.base import BaseApplication
from playwright.sync_api import sync_playwright

from taskapp import create_app
from tests.test_tasks import FakeAds, FakeBrowser


def serve(database, port, image):
    class CaptureBrowser(FakeBrowser):
        def snapshot(self):
            return image, [{"id": 1, "index": 0, "url": "https://example.com", "active": True}]

    application = create_app({"SECRET_KEY": "isolated-gunicorn-secret",
                              "ADMIN_PASSWORD": "isolated-gunicorn-admin",
                              "DATABASE": database}, ads=FakeAds(), browser_factory=CaptureBrowser)

    class Server(BaseApplication):
        def load_config(self):
            for key, value in {"bind": f"127.0.0.1:{port}", "workers": 1,
                               "threads": 12, "timeout": 30, "loglevel": "warning"}.items():
                self.cfg.set(key, value)

        def load(self):
            return application

    Server().run()


def main():
    with tempfile.TemporaryDirectory(prefix="taskroom-gunicorn-ws-") as directory:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True, args=["--no-sandbox"])
            image = browser.new_page().screenshot(type="jpeg")
            browser.close()
        process = multiprocessing.get_context("fork").Process(
            target=serve, args=(str(Path(directory) / "tasks.db"), port, image))
        process.start()
        try:
            for _ in range(100):
                try:
                    with request.urlopen(origin + "/health", timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.05)
            admin_jar, guest_jar = http.cookiejar.CookieJar(), http.cookiejar.CookieJar()
            admin = request.build_opener(request.HTTPCookieProcessor(admin_jar))
            guest = request.build_opener(request.HTTPCookieProcessor(guest_jar))

            def csrf(opener, path):
                return re.search(r'name="csrf-token" content="([^"]+)"',
                                 opener.open(origin + path).read().decode())[1]

            def post(opener, path, csrf_value, data):
                return json.load(opener.open(request.Request(
                    origin + path, data=json.dumps(data).encode(),
                    headers={"Content-Type": "application/json", "X-CSRF-Token": csrf_value})))

            admin_csrf = csrf(admin, "/")
            post(admin, "/api/login", admin_csrf, {"password": "isolated-gunicorn-admin"})
            task = post(admin, "/api/admin/tasks", admin_csrf, {
                "title": "isolated websocket server", "start_url": "https://example.com",
                "lifetime_minutes": 5})["task"]
            path, base = "/t/" + task["token"], "/api/task/" + task["token"]
            guest_csrf = csrf(guest, path)
            for _ in range(100):
                if json.load(guest.open(origin + base))["task"]["state"] == "active":
                    break
                time.sleep(0.05)
            post(guest, base + "/claim", guest_csrf, {})
            with sync_playwright() as driver:
                browser = driver.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context()
                context.add_cookies([{"name": cookie.name, "value": cookie.value, "url": origin}
                                    for cookie in guest_jar])
                page = context.new_page()
                messages, closures, actions = [], [], []

                def connected(ws):
                    ws.on("framereceived", lambda value: messages.append(value))
                    ws.on("close", lambda: closures.append(True))
                    ws.on("framesent", lambda value: actions.append(json.loads(value))
                          if isinstance(value, str) else None)

                page.on("websocket", connected)
                page.goto(origin + path)
                for _ in range(100):
                    if (page.locator("#screen").get_attribute("src") or "").startswith("blob:"):
                        break
                    page.wait_for_timeout(50)
                assert (page.locator("#screen").get_attribute("src") or "").startswith("blob:")
                page.locator("#new-tab").click()
                page.locator("#tabs .tab-close").click()
                assert any(value.get("action", {}).get("type") == "close_tab"
                           and value["action"].get("id") == 1 for value in actions)
                assert page.locator("#connection-state").inner_text() == "实时连接"
                assert image in messages
                assert any(isinstance(value, str) and json.loads(value).get("type") == "state"
                           for value in messages)
                fallback = context.new_page()
                # Leave the mocked channel unanswered; the application's own
                # handshake timeout must restore HTTP, without a mock-side close.
                fallback.route_web_socket("**/control", lambda ws: None)
                fallback.goto(origin + path, wait_until="domcontentloaded")
                for _ in range(100):
                    if "/stream?" in (fallback.locator("#screen").get_attribute("src") or ""):
                        break
                    fallback.wait_for_timeout(50)
                assert "/stream?" in (fallback.locator("#screen").get_attribute("src") or "")
                with fallback.expect_response(lambda response: response.url.endswith("/input")) as result:
                    fallback.locator("#new-tab").click()
                assert result.value.status == 200
                with fallback.expect_response(lambda response: response.url.endswith("/input")) as result:
                    fallback.locator("#tabs .tab-close").click()
                assert result.value.status == 200
                assert fallback.locator("#connection-state").inner_text() == "兼容连接"
                fallback.close()
                post(guest, base + "/submit", guest_csrf, {})
                page.locator("#overlay-title").get_by_text("已完成", exact=True).wait_for()
                page.wait_for_timeout(200)
                assert closures
                print("GUNICORN_WEBSOCKET_OK: 12 threads, authenticated state/binary frames, "
                      "input, HTTP fallback and completion revocation; isolated fake profile only", flush=True)
                browser.close()
        finally:
            process.terminate()
            process.join(10)
            assert not process.is_alive()


if __name__ == "__main__":
    main()

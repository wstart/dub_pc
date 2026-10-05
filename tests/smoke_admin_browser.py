"""Opt-in real UI regression, with an isolated DB and one owned AdsPower profile."""
from __future__ import annotations

import json
import logging
import os
import select
import socket
import socketserver
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections import deque

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler

from taskapp import create_app
from taskapp.browser import AdsPower


def wait(predicate, label, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("Timed out: " + label)


def main():
    logging.basicConfig(level=logging.INFO)
    config = dict(line.split("=", 1) for line in Path("/etc/adspower-headless.env").read_text().splitlines()
                  if "=" in line)
    requested = []
    slow_started = threading.Event()
    ui_errors = []
    screenshots = os.environ.get("TASKROOM_UI_SHOTS")

    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            if self.path == "/slow":
                slow_started.set()
                time.sleep(3)
            body = """<!doctype html><meta charset="utf-8"><input id="field" value="">
                    <button id="save" onclick="localStorage.setItem('done','REGRESSION_DONE')">保存</button>
                    <p>中文页面：管理员回归测试</p>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass  # The owned test may leave the slow page before it finishes.

        def log_message(self, *args):
            pass

    class TestAds(AdsPower):
        def __init__(self):
            super().__init__("http://127.0.0.1:50325", config["ADSPOWER_API_KEY"])
            self.created, self.endpoints = [], []

        def create(self, task):
            profile = super().create(task)
            self.created.append(profile)
            return profile

        def start(self, profile_id, restore_tabs=False):
            endpoint = super().start(profile_id, restore_tabs)
            self.endpoints.append(endpoint)
            if restore_tabs:
                # Force AdsPower's blank-tab failure only in this owned test profile.
                with sync_playwright() as driver:
                    browser = driver.chromium.connect_over_cdp(endpoint)
                    context = browser.contexts[0]
                    previous = context.pages[:]
                    context.new_page()
                    for page in previous:
                        page.close()
            return endpoint

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{fixture.server_port}"
    ads = TestAds()
    with tempfile.TemporaryDirectory(prefix="taskroom-admin-regression-") as directory:
        app = create_app({"TESTING": True, "SECRET_KEY": "isolated-regression-secret",
                          "ADMIN_PASSWORD": "isolated-regression-admin",
                          "DATABASE": str(Path(directory) / "tasks.sqlite3")}, ads=ads)
        manager, store = app.extensions["task_manager"], app.extensions["task_store"]

        class QuietRequests(WSGIRequestHandler):
            def log_request(self, *args):
                pass

        ui_server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietRequests)
        threading.Thread(target=ui_server.serve_forever, daemon=True).start()
        ui_origin = f"http://127.0.0.1:{ui_server.server_port}"
        latency = float(os.environ.get("TASKROOM_TEST_LATENCY_MS", "0")) / 1000
        delay_proxy = None
        if latency:
            class DelayProxy(socketserver.BaseRequestHandler):
                def handle(self):
                    upstream = socket.create_connection(("127.0.0.1", ui_server.server_port))

                    def relay(source, destination):
                        pending, eof = deque(), False
                        try:
                            while not eof or pending:
                                now = time.monotonic()
                                while pending and pending[0][0] <= now:
                                    destination.sendall(pending.popleft()[1])
                                timeout = min(0.02, max(0, pending[0][0] - now)) if pending else 0.02
                                if not eof and select.select([source], [], [], timeout)[0]:
                                    data = source.recv(262144)
                                    if data:
                                        pending.append((time.monotonic() + latency / 2, data))
                                    else:
                                        eof = True
                                elif eof:
                                    time.sleep(timeout)
                        except OSError:
                            pass
                        finally:
                            try:
                                destination.shutdown(socket.SHUT_WR)
                            except OSError:
                                pass

                    inbound = threading.Thread(target=relay, args=(self.request, upstream), daemon=True)
                    inbound.start()
                    relay(upstream, self.request)
                    upstream.close()

            delay_proxy = socketserver.ThreadingTCPServer(("127.0.0.1", 0), DelayProxy)
            delay_proxy.daemon_threads = True
            threading.Thread(target=delay_proxy.serve_forever, daemon=True).start()
            ui_origin = f"http://127.0.0.1:{delay_proxy.server_address[1]}"
        admin, guest, other = (app.test_client() for _ in range(3))
        admin.get("/")
        with admin.session_transaction() as session:
            admin_headers = {"X-CSRF-Token": session["csrf"]}
        assert admin.post("/api/login", json={"password": "isolated-regression-admin"},
                          headers=admin_headers).status_code == 200
        task = None
        try:
            response = admin.post("/api/admin/tasks", json={
                "title": "独立管理员真实回归", "start_url": origin + "/start",
                "lifetime_minutes": 5}, headers=admin_headers)
            assert response.status_code == 201
            task = response.json["task"]
            task_id, token = task["id"], task["token"]
            base, admin_base = "/api/task/" + token, f"/api/admin/tasks/{task_id}/browser"
            wait(lambda: store.get(task_id)["state"] in {"active", "error"}, "browser startup")
            assert store.get(task_id)["state"] == "active", store.get(task_id)["message"]
            profile = store.get(task_id)["profile_id"]
            guest.get("/t/" + token)
            other.get("/t/" + token)
            with guest.session_transaction() as session:
                guest_headers = {"X-CSRF-Token": session["csrf"]}
            with other.session_transaction() as session:
                other_headers = {"X-CSRF-Token": session["csrf"]}

            def post(client, path, headers, status, data=None):
                result = client.post(path, json=data or {}, headers=headers)
                assert result.status_code == status, (path, result.status_code, result.json)

            def ui_page(context, client, path):
                context = context.browser.new_context(bypass_csp=True)
                context.add_cookies([{"name": "session", "value": client.get_cookie("session").value,
                                      "url": ui_origin}])
                page = context.new_page()
                page.control_actions = []
                page.on("pageerror", lambda error: ui_errors.append(str(error)))

                def socket_opened(socket):
                    def sent(payload):
                        if isinstance(payload, str):
                            value = json.loads(payload)
                            if value.get("type") == "input":
                                page.control_actions.append(value["action"])
                    socket.on("framesent", sent)

                page.on("websocket", socket_opened)
                page.set_viewport_size({"width": 1440, "height": 1200})
                page.goto(ui_origin + path, wait_until="domcontentloaded")
                return page

            def check_layout(page):
                address = page.locator("#address").bounding_box()
                go = page.locator("#navigate button").bounding_box()
                new_tab = page.locator("#new-tab").bounding_box()
                for box in (address, go, new_tab):
                    assert abs(box["height"] - 44) < 1, box
                    assert abs(box["y"] - address["y"]) < 1, (box, address)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

            def screenshot(page, name):
                if screenshots:
                    page.screenshot(path=str(Path(screenshots) / (name + ".png")), full_page=True,
                                    animations="disabled")

            def remote_click(view, target):
                rect = target.bounding_box()
                screen = view.locator("#screen")
                screen.wait_for(state="visible")
                bounds = screen.bounding_box()
                screen.click(position={"x": (rect["x"] + rect["width"] / 2) * bounds["width"] / 1280,
                                       "y": (rect["y"] + rect["height"] / 2) * bounds["height"] / 800})

            with sync_playwright() as driver:
                ui_browser = driver.chromium.launch(headless=True, args=["--no-sandbox"])
                ui_context = ui_browser.new_context()
                browser = driver.chromium.connect_over_cdp(ads.endpoints[-1])
                context = browser.contexts[0]
                context.add_cookies([{"name": "taskroom_preserve_cookie", "value": "owned-test-cookie",
                                      "url": origin, "expires": time.time() + 3600}])
                target = next(page for page in reversed(context.pages) if page.url == origin + "/start")
                guest_ui = ui_page(ui_context, guest, "/t/" + token)
                guest_ui.locator("#claim").click()
                guest_ui.wait_for_function("!document.getElementById('clear-input').disabled")
                guest_ui.wait_for_function("document.getElementById('screen').naturalWidth > 0")
                guest_ui.wait_for_function("document.getElementById('screen').src.startsWith('blob:')")
                runtime = manager.runtimes[task_id]
                before_tabs = len(context.pages)
                original_index = next(tab["index"] for tab in runtime.tabs if tab["active"])
                started = time.monotonic()
                guest_ui.locator("#new-tab").click()
                guest_ui.wait_for_function("count => document.querySelectorAll('#tabs .tab-select').length === count",
                                           arg=before_tabs + 1)
                new_tab_latency = time.monotonic() - started
                assert new_tab_latency < 0.6 + latency, new_tab_latency
                guest_ui.locator("#address").fill(origin + "/slow")
                guest_ui.locator("#navigate button").click()
                wait(slow_started.is_set, "slow page started")
                started = time.monotonic()
                guest_ui.locator("#new-tab").click()
                guest_ui.wait_for_function("count => document.querySelectorAll('#tabs .tab-select').length === count",
                                           arg=before_tabs + 2)
                during_load = time.monotonic() - started
                assert during_load < 0.6 + latency, during_load
                active_id = next(tab["id"] for tab in runtime.tabs if tab["active"])
                guest_ui.locator("#tabs .tab-close").nth(before_tabs).click()
                guest_ui.wait_for_function("count => document.querySelectorAll('#tabs .tab-select').length === count",
                                           arg=before_tabs + 1)
                assert next(tab["id"] for tab in runtime.tabs if tab["active"]) == active_id
                guest_ui.locator("#tabs .active .tab-close").click()
                guest_ui.wait_for_function("count => document.querySelectorAll('#tabs .tab-select').length === count",
                                           arg=before_tabs)
                guest_ui.locator("#tabs .tab-select").nth(original_index).click()
                wait(lambda: runtime.tabs[original_index]["active"], "return to original tab")
                print(f"TAB_SPEED_OK: RTT {latency:.3f}s; new tab {new_tab_latency:.3f}s; during 3s page load {during_load:.3f}s", flush=True)
                assert guest_ui.locator("#connection-state").inner_text() == "实时连接"
                check_layout(guest_ui)
                screenshot(guest_ui, "task-desktop")
                guest_ui.set_viewport_size({"width": 390, "height": 844})
                check_layout(guest_ui)
                screenshot(guest_ui, "task-mobile")
                guest_ui.set_viewport_size({"width": 1440, "height": 1200})
                print("LAYOUT_OK: desktop/mobile controls aligned at 44px, no horizontal overflow", flush=True)
                print("TAB_CLOSE_OK: background close preserves selection; current close selects neighbor", flush=True)
                remote_click(guest_ui, target.locator("#field"))
                guest_ui.locator("#remote-text").fill("用户输入中文")
                guest_ui.locator("#text-input button").first.click()
                target.wait_for_function("document.getElementById('field').value === '用户输入中文'")
                runtime = manager.runtimes[task_id]
                remote_click(guest_ui, target.locator("#field"))
                guest_ui.locator("#clear-input").click()
                target.wait_for_function("document.getElementById('field').value === ''")
                inputs, streams = [], []
                guest_ui.control_actions.clear()
                guest_ui.on("request", lambda req: inputs.append(req.post_data_json)
                            if req.url.endswith("/input") else streams.append(req.url)
                            if "/stream?" in req.url else None)
                before, started = runtime.frame_version, time.monotonic()
                guest_ui.evaluate("""() => {
                    const screen=document.getElementById('screen');
                    for(const key of 'abcdefghijklmnopqrstuvwxyz0123456789')
                        screen.dispatchEvent(new KeyboardEvent('keydown',{key,bubbles:true}));
                    screen.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}));
                    screen.dispatchEvent(new KeyboardEvent('keydown',{key:'X',bubbles:true}));
                }""")
                target.wait_for_function("document.getElementById('field').value === 'abcdefghijklmnopqrstuvwxyz012345678X9'")
                wait(lambda: runtime.frame_version > before, "input rendered to continuous frame")
                input_latency = time.monotonic() - started
                assert len(inputs) <= 4, inputs
                assert len(guest_ui.control_actions) == 3, guest_ui.control_actions
                assert input_latency < 1.5, input_latency
                guest_ui.wait_for_timeout(1200)
                assert len(streams) <= 1, "MJPEG must not reconnect for each image"
                print(f"SPEED_OK: 37 characters + cursor key in {len(guest_ui.control_actions)} realtime messages; input/frame {input_latency:.3f}s", flush=True)
                remote_click(guest_ui, target.locator("#save"))
                target.wait_for_function("localStorage.getItem('done') === 'REGRESSION_DONE'")
                original_controller = store.get(task_id)["controller"]
                admin_ui = ui_page(ui_context, admin, f"/admin/tasks/{task_id}/browser")
                admin_ui.wait_for_function("document.getElementById('screen').naturalWidth > 0")
                assert admin_ui.locator("#clear-input").is_disabled()
                assert admin_ui.locator("#tabs .tab-close").first.is_disabled()
                assert admin_ui.locator("#task-state").inner_text() == "只读观察"
                assert admin_ui.locator("#submit").inner_text() == "结束观察"
                post(admin, admin_base + "/input", admin_headers, 409, {"type": "text", "text": "blocked"})
                post(admin, admin_base + "/input", admin_headers, 409,
                     {"type": "close_tab", "id": runtime.tabs[0]["id"]})
                assert store.get(task_id)["controller"] == original_controller
                admin_ui.locator("#submit").click()
                admin_ui.wait_for_function("document.getElementById('overlay-title').textContent === '已结束观察'")
                assert store.get(task_id)["state"] == "active"
                remote_click(guest_ui, target.locator("#field"))
                guest_ui.locator("#clear-input").click()
                target.wait_for_function("document.getElementById('field').value === ''")
                guest_ui.locator("#remote-text").fill("管理员退出后用户继续")
                guest_ui.locator("#text-input button").first.click()
                target.wait_for_function("document.getElementById('field').value === '管理员退出后用户继续'")
                runtime = manager.runtimes[task_id]
                assert guest_ui.locator("#screen").get_attribute("src").startswith("blob:")
                before = runtime.frame_version
                target.evaluate("document.body.style.backgroundColor = '#b9ded1'")
                wait(lambda: runtime.frame_version > before, "continuous frame after observer exit")
                guest_ui.wait_for_function("""() => {
                    const image=document.getElementById('screen'), canvas=document.createElement('canvas');
                    canvas.width=1280;canvas.height=800;
                    const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0,1280,800);
                    const pixel=ctx.getImageData(1200,700,1,1).data;
                    return Math.abs(pixel[0]-185)<10&&Math.abs(pixel[1]-222)<10;
                }""")
                assert len(ads.endpoints) == 1
                assert store.get(task_id)["controller"] == original_controller
                admin_ui.close()
                print("OBSERVER_OK: real frame, read-only, close observer preserves guest", flush=True)

                admin_ui = ui_page(ui_context, admin, f"/admin/tasks/{task_id}/browser")
                admin_ui.wait_for_function("document.getElementById('screen').naturalWidth > 0")
                guest_ui.on("dialog", lambda dialog: dialog.accept())
                guest_ui.locator("#submit").click()
                guest_ui.wait_for_function("document.getElementById('clear-input').disabled")
                wait(lambda: store.get(task_id)["state"] == "completed", "server accepted completion")
                assert store.get(task_id)["state"] == "completed"
                for endpoint in ("claim", "input", "submit"):
                    post(guest, base + "/" + endpoint, guest_headers, 409,
                         {"type": "text", "text": "blocked"})
                assert guest.get(base + "/frame").status_code == 409
                assert guest_ui.locator("#tabs .tab-close").first.is_disabled()
                post(guest, base + "/input", guest_headers, 409,
                     {"type": "close_tab", "id": runtime.tabs[0]["id"]})
                result = store.get(task_id)
                assert result["state"] == "completed", result["message"]
                assert "/verify" not in requested
                admin_ui.wait_for_function("!document.getElementById('clear-input').disabled")
                assert admin_ui.locator("#task-state").inner_text() == "管理员操作"
                assert admin_ui.locator("#submit").inner_text() == "关闭管理员浏览器"
                admin_ui.locator("#new-tab").click()
                admin_ui.wait_for_function("document.querySelectorAll('#tabs .tab-select').length === 3")
                admin_ui.locator("#tabs .active .tab-close").click()
                admin_ui.wait_for_function("document.querySelectorAll('#tabs .tab-select').length === 2")
                admin_ui.once("dialog", lambda dialog: dialog.dismiss())
                admin_ui.locator("#submit").click()
                assert store.get(task_id)["admin_open"] == 1
                assert manager.runtimes[task_id].thread.is_alive()
                remote_click(admin_ui, target.locator("#field"))
                admin_ui.locator("#clear-input").click()
                target.wait_for_function("document.getElementById('field').value === ''")
                admin_ui.locator("#remote-text").fill("管理员继续操作")
                admin_ui.locator("#text-input button").first.click()
                target.wait_for_function("document.getElementById('field').value === '管理员继续操作'")
                for client, headers in ((guest, guest_headers), (other, other_headers)):
                    for endpoint in ("claim", "input", "submit"):
                        post(client, base + "/" + endpoint, headers, 409, {"type": "text", "text": "blocked"})
                    assert client.get(base + "/frame").status_code == 409
                for endpoint, data in (("release", {}), ("delete", {"confirm": True})):
                    post(admin, f"/api/admin/tasks/{task_id}/" + endpoint, admin_headers, 409, data)
                assert ads.created == [profile]
                print("HANDOVER_OK: immediate completion, no checking, guest lock, admin clear/Chinese input", flush=True)
                dashboard = ui_page(ui_context, admin, "/")
                dashboard.locator(".task-card").wait_for()
                screenshot(dashboard, "admin-desktop")
                dashboard.set_viewport_size({"width": 390, "height": 844})
                assert dashboard.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                screenshot(dashboard, "admin-mobile")
                dashboard.set_viewport_size({"width": 1440, "height": 1200})
                dashboard.on("dialog", lambda dialog: dialog.accept())
                dashboard.get_by_role("button", name="重新开放", exact=True).click()
                admin_ui.wait_for_function("document.getElementById('clear-input').disabled")
                assert store.get(task_id)["state"] == "active"
                assert store.get(task_id)["controller"] == original_controller
                post(admin, admin_base + "/input", admin_headers, 409, {"type": "text", "text": "blocked"})
                post(other, base + "/claim", other_headers, 409)
                guest_ui.reload()
                guest_ui.wait_for_function("!document.getElementById('clear-input').disabled")
                remote_click(guest_ui, target.locator("#field"))
                guest_ui.locator("#clear-input").click()
                target.wait_for_function("document.getElementById('field').value === ''")
                guest_ui.locator("#remote-text").fill("重新开放后继续")
                guest_ui.locator("#text-input button").first.click()
                target.wait_for_function("document.getElementById('field').value === '重新开放后继续'")
                guest_ui.locator("#submit").click()
                admin_ui.wait_for_function("!document.getElementById('clear-input').disabled")
                assert store.get(task_id)["state"] == "completed"
                result = store.get(task_id)
                assert ads.created == [profile]
                admin_ui.locator("#address").fill(origin + "/last-task-page")
                admin_ui.locator("#navigate button").click()
                target.wait_for_url(origin + "/last-task-page")
                wait(lambda: any(tab["url"] == origin + "/last-task-page"
                                 for tab in json.loads(store.get(task_id)["browser_tabs"])), "save last page")
                dashboard.close()
                print("REOPEN_OK: dashboard button, original guest resumes, admin read-only, complete locks again", flush=True)
                guest_ui.close()
                admin_ui.once("dialog", lambda dialog: dialog.accept())
                admin_ui.locator("#submit").click()
                admin_ui.wait_for_function("document.getElementById('overlay-title').textContent === '管理员浏览器已关闭'")
                admin_ui.close()
                manager.runtimes[task_id].thread.join(15)
                assert not manager.runtimes[task_id].thread.is_alive()
                for field in ("state", "message", "checked_at", "profile_id"):
                    assert store.get(task_id)[field] == result[field]

                post(admin, admin_base + "/open", admin_headers, 202)
                wait(lambda: admin.get(admin_base).json["task"]["browser_ready"], "resume original")
                browser = driver.chromium.connect_over_cdp(ads.endpoints[-1])
                context = browser.contexts[0]
                assert ads.created == [profile]
                assert store.get(task_id)["profile_id"] == profile
                post(guest, base + "/claim", guest_headers, 409)
                admin_ui = ui_page(ui_context, admin, f"/admin/tasks/{task_id}/browser")
                admin_ui.wait_for_function("!document.getElementById('clear-input').disabled")
                target = next(page for page in context.pages if page.url == origin + "/last-task-page")
                assert target.evaluate("localStorage.getItem('done')") == "REGRESSION_DONE"
                assert any(cookie["name"] == "taskroom_preserve_cookie" and cookie["value"] == "owned-test-cookie"
                           for cookie in context.cookies())
                temporary_page = context.new_page()
                temporary_page.close()
                print("RESUME_OK: last page auto-restored from blank, original profile, Cookie/localStorage preserved", flush=True)
                admin_ui.close()
                post(admin, admin_base + "/close", admin_headers, 200)
                manager.runtimes[task_id].thread.join(15)
                assert not manager.runtimes[task_id].thread.is_alive()
                store.update(task_id, browser_tabs="[]")
                post(admin, f"/api/admin/tasks/{task_id}/reopen", admin_headers, 202)
                wait(lambda: store.get(task_id)["state"] == "active", "fallback reopening")
                browser = driver.chromium.connect_over_cdp(ads.endpoints[-1])
                context = browser.contexts[0]
                assert any(page.url == origin + "/start" for page in context.pages)
                assert any(cookie["name"] == "taskroom_preserve_cookie" and cookie["value"] == "owned-test-cookie"
                           for cookie in context.cookies())
                assert store.get(task_id)["controller"] == original_controller
                post(guest, base + "/input", guest_headers, 200, {"type": "text", "text": ""})
                print("FALLBACK_OK: empty legacy record opens start page, Cookie and original guest preserved", flush=True)
                guest_ui = ui_page(ui_context, guest, "/t/" + token)
                guest_ui.wait_for_function("document.getElementById('screen').src.startsWith('blob:')")
                assert guest_ui.locator("#tabs .tab-close").count() == 1
                guest_ui.locator("#tabs .tab-close").click()
                # Pump the UI driver's events before reading the second CDP
                # connection's page list; time.sleep alone leaves it stale.
                guest_ui.wait_for_function("document.querySelectorAll('#tabs .tab-select').length === 1 && document.getElementById('address').value === ''")
                assert len(context.pages) == 1 and context.pages[0].url == "about:blank"
                wait(lambda: store.get(task_id)["browser_tabs"] == "[]", "explicit close clears stale restore URLs")
                assert store.get(task_id)["state"] == "active"
                assert store.get(task_id)["profile_id"] == profile
                assert store.get(task_id)["controller"] == original_controller
                assert any(cookie["name"] == "taskroom_preserve_cookie" and cookie["value"] == "owned-test-cookie"
                           for cookie in context.cookies())
                print("LAST_TAB_OK: blank page retained; original profile, owner, Cookie preserved", flush=True)
                assert not ui_errors, ui_errors
                post(admin, "/api/logout", admin_headers, 200)
                assert admin.get(admin_base + "/frame").status_code == 401
                assert guest.get(admin_base + "/frame").status_code == 401
                print("AUTH_OK: logout and guest cannot access admin frames", flush=True)
                ui_browser.close()
        finally:
            for runtime in list(manager.runtimes.values()):
                runtime.cancel()
                runtime.thread.join(20)
                assert not runtime.thread.is_alive(), "Own runtime did not stop; not deleting live profile"
            for profile in ads.created:
                ads.delete(profile)
            fixture.shutdown()
            fixture.server_close()
            ui_server.shutdown()
            ui_server.server_close()
            if delay_proxy:
                delay_proxy.shutdown()
                delay_proxy.server_close()
            print("OWN_REGRESSION_PROFILE_REMOVED; production database untouched", flush=True)


if __name__ == "__main__":
    main()

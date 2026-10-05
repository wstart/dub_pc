"""Cookie UI/API acceptance in an isolated DB; optional owned AdsPower profile."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from flask import request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler

from taskapp import create_app
from taskapp.browser import AdsPower
from tests.smoke_admin_browser import wait
from tests.test_tasks import FakeAds, FakeBrowser


FIXTURE_COOKIES = [
    {"name": "owned_http_only", "value": "owned-session", "domain": "cookie-fixture.test",
     "path": "/", "httpOnly": True, "secure": True, "sameSite": "Lax"},
    {"name": "owned_duplicate", "value": "owned-root", "domain": ".cookie-fixture.test",
     "path": "/", "expires": time.time() + 86400, "sameSite": "Strict"},
    {"name": "owned_duplicate", "value": "owned-other-site", "domain": "other-cookie-fixture.test",
     "path": "/private", "expires": time.time() + 86400, "sameSite": "Lax"},
]


def assert_fixture(cookies):
    indexed = {(item["domain"], item["path"], item["name"]): item for item in cookies}
    for item in FIXTURE_COOKIES:
        key = item["domain"], item["path"], item["name"]
        assert indexed[key]["value"] == item["value"], "Fixture cookie missing or changed"
    assert indexed[("cookie-fixture.test", "/", "owned_http_only")]["httpOnly"] is True


def main():
    real = os.environ.get("TASKROOM_REAL_COOKIES") == "1"
    errors, requests = [], Counter()

    class Fixture(BaseHTTPRequestHandler):
        def do_GET(self):
            body = "<!doctype html><meta charset=utf-8><h1>Cookie 独立测试</h1><input id=field>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class CookieBrowser(FakeBrowser):
        def cookies(self):
            return FIXTURE_COOKIES

    class QuietRequests(WSGIRequestHandler):
        def log_request(self, *args):
            pass

    if real:
        config = dict(line.split("=", 1) for line in
                      Path("/etc/adspower-headless.env").read_text().splitlines() if "=" in line)
        ads = AdsPower("http://127.0.0.1:50325", config["ADSPOWER_API_KEY"])
        kwargs = {}
    else:
        ads, kwargs = FakeAds(), {"browser_factory": CookieBrowser}
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    owned_profile = None
    with tempfile.TemporaryDirectory(prefix="taskroom-cookie-owned-") as directory:
        app = create_app({"TESTING": True, "SECRET_KEY": "cookie-fixture-secret",
                          "ADMIN_PASSWORD": "cookie-fixture-admin",
                          "DATABASE": str(Path(directory) / "tasks.db")}, ads=ads, **kwargs)
        manager, store = app.extensions["task_manager"], app.extensions["task_store"]

        @app.before_request
        def count_cookie_reads():
            if "/cookies" in request.path:
                requests[request.path] += 1

        server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietRequests)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{server.server_port}"
        admin, guest = app.test_client(), app.test_client()
        admin.get("/")
        with admin.session_transaction() as session:
            headers = {"X-CSRF-Token": session["csrf"]}
        admin.post("/api/login", json={"password": "cookie-fixture-admin"}, headers=headers)
        try:
            task = manager.create({"title": "Cookie 查看与下载测试", "lifetime_minutes": 5,
                                   "start_url": f"http://127.0.0.1:{fixture.server_port}/"})
            task_id = task["id"]
            wait(lambda: store.get(task_id)["state"] in {"active", "error"}, "own browser startup")
            task = store.get(task_id)
            owned_profile = task["profile_id"]
            assert task["state"] == "active", "Own browser did not start"
            base = f"/api/admin/tasks/{task_id}/cookies"
            user_base = "/api/task/" + task["token"]
            guest.get("/t/" + task["token"])
            with guest.session_transaction() as session:
                guest_headers = {"X-CSRF-Token": session["csrf"]}
            assert guest.post(user_base + "/claim", json={}, headers=guest_headers).status_code == 200
            wait(lambda: bool(manager.runtimes[task_id].frame), "first frame")
            with sync_playwright() as driver:
                if real:
                    status = ads.call("/api/v1/browser/active?user_id=" + owned_profile)
                    remote = driver.chromium.connect_over_cdp(status["ws"]["puppeteer"])
                    remote_context = remote.contexts[0]
                    remote_context.add_cookies(FIXTURE_COOKIES)
                browser = driver.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context(accept_downloads=True)
                context.add_cookies([{"name": "session", "value": admin.get_cookie("session").value,
                                     "url": origin}])
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("dialog", lambda dialog: dialog.accept())
                page.goto(origin)
                card = page.locator(".task-card").first
                expect(card.get_by_role("button", name="查看 Cookie", exact=True)).to_be_visible()
                assert requests[base] == 0, "Cookie must not be read automatically"
                before = store.get(task_id)
                for width in (1440, 390, 320):
                    page.set_viewport_size({"width": width, "height": 1000})
                    card.get_by_role("button", name="查看 Cookie", exact=True).click()
                    expect(card.locator(".cookie-json")).to_be_visible()
                    assert_fixture(json.loads(card.locator(".cookie-json").inner_text()))
                    assert card.locator(".cookie-json img").count() == 0
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                    if os.environ.get("TASKROOM_COOKIE_SHOTS"):
                        shots = Path(os.environ["TASKROOM_COOKIE_SHOTS"])
                        shots.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(shots / f"cookies-{width}.png"), full_page=True)
                    reads = requests[base]
                    store.update(task_id, message=f"Fixture UI refresh {width}")
                    expect(card.locator(".muted")).to_have_text(f"Fixture UI refresh {width}", timeout=6000)
                    expect(card.locator(".cookie-json")).to_be_visible()
                    assert requests[base] == reads, "List polling must not re-read cookies"
                    card.get_by_role("button", name="收起", exact=True).click()
                    assert card.locator(".cookie-json").count() == 0
                for key in ("state", "profile_id", "controller", "token", "expires_at", "admin_open"):
                    assert store.get(task_id)[key] == before[key], key + " changed by cookie access"
                with page.expect_download() as event:
                    card.get_by_role("button", name="下载 Cookie", exact=True).click()
                download = event.value
                assert download.suggested_filename == f"cookies-{task['code']}.json"
                assert_fixture(json.loads(Path(download.path()).read_text()))
                assert requests[base + "/download"] == 1
                assert "owned-session" not in admin.get("/api/admin/tasks").get_data(as_text=True)
                assert "owned-session" not in guest.get(user_base).get_data(as_text=True)
                assert guest.post(base, json={}, headers=guest_headers).status_code == 401
                assert guest.post(user_base + "/submit", json={}, headers=guest_headers).status_code == 200
                manager.runtimes[task_id].thread.join(15)
                assert not manager.runtimes[task_id].thread.is_alive()
                closed = admin.post(base, json={}, headers=headers)
                assert closed.status_code == 409 and "管理员打开" in closed.json["error"]
                card.get_by_role("button", name="查看 Cookie", exact=True).click()
                expect(card.locator(".cookie-error")).to_contain_text("管理员打开")
                expect(card.locator(".cookie-heading h4")).to_have_text("Cookie 读取失败")
                assert admin.post(f"/api/admin/tasks/{task_id}/browser/open", json={}, headers=headers).status_code == 202
                wait(lambda: manager.runtimes[task_id].browser_ready, "manual admin open")
                response = admin.post(base, json={}, headers=headers)
                assert response.status_code == 200
                assert_fixture(response.json["cookies"])
                card.get_by_role("button", name="查看 Cookie", exact=True).click()
                expect(card.locator(".cookie-json")).to_be_visible()
                assert_fixture(json.loads(card.locator(".cookie-json").inner_text()))
                assert store.get(task_id)["state"] == "completed"
                assert guest.get(user_base + "/frame").status_code == 409
                page.get_by_role("button", name="退出登录", exact=True).click()
                expect(page.locator("#login")).to_be_visible()
                assert page.request.post(origin + base, headers=headers).status == 401
                assert not errors, errors
                browser.close()
                print("COOKIE_UI_API_OK: all sites, HttpOnly, duplicate names, JSON download, mobile, manual reopen, owner lock, logout", flush=True)
        finally:
            for runtime in manager.runtimes.values():
                runtime.cancel()
                runtime.thread.join(15)
                assert not runtime.thread.is_alive(), "Owned runtime did not close"
            if owned_profile:
                ads.delete(owned_profile)
                print("OWN_COOKIE_TEST_PROFILE_REMOVED", flush=True)
            server.shutdown()
            fixture.shutdown()


if __name__ == "__main__":
    main()

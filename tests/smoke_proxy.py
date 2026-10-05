"""Opt-in local authenticated proxy integration test; deletes only its own task."""
from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, request


def main():
    config = dict(line.split("=", 1) for line in Path("/etc/adspower-tasks.env").read_text().splitlines()
                  if "=" in line)
    accepted = []
    auth = "Basic " + base64.b64encode(b"taskroom-smoke:local-test-only").decode()

    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Proxy-Authorization") != auth:
                self.send_response(407)
                self.send_header("Proxy-Authenticate", 'Basic realm="taskroom-smoke"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            accepted.append(self.path)
            body = b"<html><body>TASKROOM_AUTH_PROXY_OK</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_CONNECT(self):
            try:
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    base = os.environ.get("TASKROOM_SMOKE_URL", "http://127.0.0.1:5088")

    def session(path):
        opener = request.build_opener(request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        html = opener.open(base + path, timeout=20).read().decode()
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        return opener, csrf

    def api(opener, csrf, path, data=None):
        req = request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                              headers={"Content-Type": "application/json", "X-CSRF-Token": csrf})
        return json.load(opener.open(req, timeout=90))

    admin, csrf = session("/")
    task = None
    try:
        api(admin, csrf, "/api/login", {"password": config["ADMIN_PASSWORD"]})
        task = api(admin, csrf, "/api/admin/tasks", {
            "title": "代理与删除功能验收", "start_url": "http://taskroom-proxy-check.invalid/start",
            "lifetime_minutes": 5,
            "proxy": f"http://taskroom-smoke:local-test-only@127.0.0.1:{proxy.server_port}",
        })["task"]
        assert "local-test-only" not in json.dumps(task)
        print("created own proxy test task", task["code"], flush=True)
        guest, guest_csrf = session("/t/" + task["token"])
        task_api = "/api/task/" + task["token"]
        deadline = time.monotonic() + 90

        def wait(states):
            while time.monotonic() < deadline:
                current = api(guest, guest_csrf, task_api)["task"]
                if current["state"] in states:
                    return current
                if current["state"] == "error":
                    raise RuntimeError(current["message"])
                time.sleep(1)
            raise RuntimeError("Proxy task timed out")

        wait({"active"})
        api(guest, guest_csrf, task_api + "/claim", {})
        api(guest, guest_csrf, task_api + "/submit", {})
        result = wait({"completed"})
        assert result["state"] == "completed", result
        assert any("/start" in path for path in accepted), "Start page bypassed proxy"
        assert not any("/verify" in path for path in accepted), "Unexpected verification"
        print("AUTHENTICATED_HTTP_PROXY_OK: start through proxy; completion does not verify", flush=True)
        time.sleep(2)
        api(admin, csrf, f"/api/admin/tasks/{task['id']}/delete", {"confirm": True})
        try:
            guest.open(base + "/t/" + task["token"], timeout=20)
            raise AssertionError("Deleted task URL still worked")
        except error.HTTPError as exc:
            assert exc.code == 404
        print("DELETE_OK: own task + profile deleted; old URL returns 404", flush=True)
        task = None
    finally:
        # Never touch unrelated tasks, including tasks created by the administrator.
        if task:
            current = next((item for item in api(admin, csrf, "/api/admin/tasks")["tasks"]
                            if item["id"] == task["id"]), None)
            if current and current["state"] in {"starting", "active", "verifying"}:
                api(admin, csrf, f"/api/admin/tasks/{task['id']}/cancel", {})
                time.sleep(2)
            if current:
                api(admin, csrf, f"/api/admin/tasks/{task['id']}/delete", {"confirm": True})
                print("own smoke task cleaned up", flush=True)
        proxy.shutdown()
        proxy.server_close()


if __name__ == "__main__":
    main()

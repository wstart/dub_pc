"""Opt-in real integration check. Creates two profiles; keeps task records."""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import re
import time
from urllib import error, request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5088")
    parser.add_argument("--env", default="/etc/adspower-tasks.env")
    options = parser.parse_args()
    from pathlib import Path
    config = dict(line.split("=", 1) for line in Path(options.env).read_text().splitlines()
                  if "=" in line)

    def session(path):
        opener = request.build_opener(request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        html = opener.open(options.url + path, timeout=20).read().decode()
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        return opener, csrf

    def api(opener, csrf, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        req = request.Request(options.url + path, data=body,
                              headers={"Content-Type": "application/json", "X-CSRF-Token": csrf})
        with opener.open(req, timeout=90) as response:
            return json.load(response)

    admin, csrf = session("/")
    api(admin, csrf, "/api/login", {"password": config["ADMIN_PASSWORD"]})
    tasks = []
    for expected in ("completed",):
        task = api(admin, csrf, "/api/admin/tasks", {
            "title": "部署验收：" + expected,
            "start_url": "https://example.com", "lifetime_minutes": 10,
        })["task"]
        tasks.append(task)
        print("created", task["code"], flush=True)
        base = "/api/task/" + task["token"]
        guest, guest_csrf = session("/t/" + task["token"])
        deadline = time.monotonic() + 240
        while True:
            state = api(guest, guest_csrf, base)["task"]
            if state["state"] == "active":
                break
            if state["state"] != "starting" or time.monotonic() > deadline:
                raise RuntimeError(state["state"] + ": " + state["message"])
            time.sleep(2)
        api(guest, guest_csrf, base + "/claim", {})
        time.sleep(1)
        with guest.open(options.url + base + "/frame", timeout=20) as response:
            image = response.read()
            assert image.startswith(b"\xff\xd8"), "No real browser frame"
        print("browser screenshot received", len(image), flush=True)
        api(guest, guest_csrf, base + "/input", {"type": "click", "x": 200, "y": 150, "button": "left"})
        api(guest, guest_csrf, base + "/submit", {})
        try:
            api(guest, guest_csrf, base + "/input", {"type": "text", "text": "rejected"})
            raise AssertionError("Input was allowed after submit")
        except error.HTTPError as exc:
            assert exc.code == 409
        while True:
            state = api(guest, guest_csrf, base)["task"]
            if state["state"] == "completed":
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Completion timed out")
            time.sleep(1)
        assert state["state"] == expected, state
        print("completed", task["code"], expected, "input locked", flush=True)
        # These two environments were created only for this smoke test.
        time.sleep(2)
        api(admin, csrf, f"/api/admin/tasks/{task['id']}/release", {})
        print("test profile released; task record retained", flush=True)
    print("SMOKE_OK", json.dumps([task["code"] for task in tasks]), flush=True)


if __name__ == "__main__":
    main()

"""Opt-in UI/real keyboard check. Removes only its own temporary profile."""
from __future__ import annotations

import time
from pathlib import Path

from jinja2 import Environment

from taskapp.browser import AdsPower, RemoteBrowser


def main():
    root = Path(__file__).resolve().parents[1]
    config = dict(line.split("=", 1) for line in Path("/etc/adspower-headless.env").read_text().splitlines()
                  if "=" in line)
    ads = AdsPower("http://127.0.0.1:50325", config["ADSPOWER_API_KEY"])
    profile = None
    remote = None
    try:
        profile = ads.create({"code": "CLEAR-INPUT-SMOKE-" + str(time.time_ns()),
                              "start_url": "about:blank"})
        remote = RemoteBrowser(ads.start(profile), "about:blank")
        remote.page.set_content("""<input id="text" value="中文 abc">
            <input id="password" type="password" value="test-secret">
            <textarea id="multiline">第一行\nsecond line</textarea>""")
        image = remote.page.screenshot(type="jpeg")
        ui = remote.context.new_page()
        ui.set_viewport_size({"width": 1280, "height": 1200})
        template = Environment().from_string((root / "taskapp/templates/task.html").read_text())
        html = template.render(csrf="test", token="test", task={"title": "清空按钮验收", "code": "TEST"},
                               url_for=lambda endpoint, filename: "/static/" + filename)
        js = (root / "taskapp/static/app.js").read_text()
        css = (root / "taskapp/static/style.css").read_text()
        state = {"value": "active"}
        actions = []

        def route(r):
            path = r.request.url.split("clear-input.test", 1)[-1]
            if path == "/":
                r.fulfill(body=html, content_type="text/html")
            elif path == "/static/app.js":
                r.fulfill(body=js, content_type="text/javascript")
            elif path == "/static/style.css":
                r.fulfill(body=css, content_type="text/css")
            elif path.endswith("/frame"):
                r.fulfill(body=image, content_type="image/jpeg")
            elif path.endswith("/input"):
                action = r.request.post_data_json
                actions.append(action)
                remote.input(action)
                r.fulfill(json={})
            else:
                r.fulfill(json={"task": {"state": state["value"], "mine": True,
                                         "message": "Ready", "expires_at": time.time() + 300,
                                         "tabs": []}})

        ui.route("**/*", route)
        ui.goto("http://clear-input.test/")
        button = ui.get_by_role("button", name="清空远程输入框", exact=True)
        assert button.count() == 1, "Clear input button is missing"
        assert button.get_attribute("type") == "button", "Clear must not submit the text form"
        ui.wait_for_function("!document.getElementById('clear-input').disabled")
        ui.locator("#remote-text").fill("尚未发送的本地文字")
        screen = ui.locator("#screen")
        for field in ("text", "password", "multiline"):
            target = remote.page.locator("#" + field)
            box = target.bounding_box()
            rect = screen.bounding_box()
            screen.click(position={"x": (box["x"] + 10) * rect["width"] / 1280,
                                   "y": (box["y"] + 10) * rect["height"] / 800})
            button.click()
            ui.wait_for_timeout(300)
            assert target.input_value() == "", (field, actions)
            assert actions[-2:] == [{"type": "key", "key": "Control+a"},
                                    {"type": "key", "key": "Backspace"}], actions
            assert ui.evaluate("document.activeElement.id") == "screen"
            assert ui.locator("#remote-text").input_value() == "尚未发送的本地文字"
        print("CLEAR_INPUT_OK: Chinese, password, textarea; local draft preserved", flush=True)
        state["value"] = "completed"
        ui.wait_for_function("document.getElementById('clear-input').disabled")
        count = len(actions)
        # Even a programmatic invocation cannot bypass the frontend permission gate.
        ui.evaluate("document.getElementById('clear-input').onclick()")
        ui.wait_for_timeout(200)
        assert len(actions) == count, actions
        print("CLEAR_LOCK_OK: completion disables button and rejects input", flush=True)
    finally:
        if remote:
            remote.close()
        if profile:
            ads.stop(profile)
            ads.delete(profile)
            print("OWN_SMOKE_PROFILE_REMOVED", flush=True)


if __name__ == "__main__":
    main()

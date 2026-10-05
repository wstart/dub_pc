"""Opt-in header layout check using fixtures, without opening AdsPower profiles."""
from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment
from playwright.sync_api import expect, sync_playwright


def main():
    root = Path(__file__).resolve().parents[1]
    template = Environment().from_string(
        (root / "taskapp/templates/task.html").read_text())
    css = (root / "taskapp/static/style.css").read_text()
    js = (root / "taskapp/static/app.js").read_text()
    title = "任务名称很长时仍需与操作状态保持在同一行" * 4
    modes = [
        (False, "active", "可操作", "rgb(70, 114, 69)"),
        (False, "completed", "已完成", "rgb(98, 116, 105)"),
        (False, "error", "连接异常", "rgb(161, 79, 66)"),
        (True, "active", "只读观察", "rgb(70, 114, 69)"),
        (True, "completed", "管理员操作", "rgb(98, 116, 105)"),
    ]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for admin, state, label, color in modes:
                html = template.render(
                    csrf="fixture", token="fixture", admin_browser=admin,
                    task={"id": 1, "title": title, "code": "FIXTURE"},
                    url_for=lambda endpoint, filename: "/static/" + filename)
                context = browser.new_context()
                context.add_init_script("window.WebSocket = class { close() {} };")
                errors = []
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))

                def route(request):
                    path = urlsplit(request.request.url).path
                    if path == "/":
                        request.fulfill(body=html, content_type="text/html")
                    elif path == "/static/style.css":
                        request.fulfill(body=css, content_type="text/css")
                    elif path == "/static/app.js":
                        request.fulfill(body=js, content_type="text/javascript")
                    else:
                        request.fulfill(json={"task": {
                            "state": state, "mine": True, "claimed": True,
                            "message": "Fixture", "expires_at": time.time() + 300,
                            "admin_open": admin, "browser_ready": admin,
                            "can_control": admin and state == "completed",
                            "tabs": [], "stream_generation": 1,
                        }})

                page.route("**/*", route)
                try:
                    for width in (1440, 390, 320):
                        page.set_viewport_size({"width": width, "height": 900})
                        page.goto("http://session-header.test/")
                        expect(page.locator("#task-state")).to_have_text(label)
                        geometry = page.evaluate("""() => {
                            const title = document.querySelector('.session-title h1');
                            const state = document.getElementById('task-state');
                            const t = title.getBoundingClientRect();
                            const s = state.getBoundingClientRect();
                            const dot = getComputedStyle(state, '::before');
                            return {title: {x:t.x, y:t.y, width:t.width, height:t.height},
                                state: {x:s.x, y:s.y, width:s.width, height:s.height},
                                dot: {content:dot.content, width:dot.width,
                                    height:dot.height, color:dot.backgroundColor},
                                overflow:document.documentElement.scrollWidth > innerWidth};
                        }""")
                        t, s = geometry["title"], geometry["state"]
                        assert abs(t["y"] + t["height"] / 2 - s["y"] - s["height"] / 2) < 1, geometry
                        assert t["width"] >= 24 and s["x"] >= t["x"] + t["width"], geometry
                        assert not geometry["overflow"], geometry
                        assert geometry["dot"] == {
                            "content": '""', "width": "6px", "height": "6px", "color": color,
                        }, geometry
                        assert page.locator(".session-title h1").get_attribute("title") == title
                        assert page.locator("#task-state").get_attribute("role") == "status"
                        before = page.locator(".viewport").bounding_box()["y"]
                        page.locator("#task-details > summary").click()
                        expect(page.locator(".session-info h2")).to_have_text(title)
                        assert page.locator(".viewport").bounding_box()["y"] == before
                    assert not errors, errors
                finally:
                    context.close()
            print("SESSION_HEADER_OK: one row, status dots, long titles, user/admin, 1440/390/320px")
        finally:
            browser.close()


if __name__ == "__main__":
    main()

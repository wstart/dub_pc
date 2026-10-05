from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import threading
import time
from urllib import error, request
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary


logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class BrowserError(RuntimeError):
    pass


def is_web_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username


class AdsPower:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.lock = threading.Lock()
        self.last_call = 0.0

    def call(self, path: str, payload: dict | None = None) -> dict:
        with self.lock:
            time.sleep(max(0, 1.05 - (time.monotonic() - self.last_call)))
            self.last_call = time.monotonic()
            headers = {"Authorization": f"Bearer {self.api_key}"}
            body = None
            if payload is not None:
                headers["Content-Type"] = "application/json"
                body = json.dumps(payload).encode()
            req = request.Request(self.base_url + path, data=body, headers=headers)
            try:
                with request.urlopen(req, timeout=120) as response:
                    result = json.load(response)
            except (error.URLError, TimeoutError, ValueError) as exc:
                raise BrowserError("AdsPower API 无法连接或响应无效") from exc
            if result.get("code") != 0:
                raise BrowserError("AdsPower：" + str(result.get("msg", "请求失败")))
            return result.get("data", {})

    def create(self, task: dict) -> str:
        data = self.call("/api/v2/browser-profile/create", {
            "name": f"Task-{task['code']}", "group_id": "0",
            "tabs": [task["start_url"]],
            "user_proxy_config": task.get("user_proxy_config") or {"proxy_soft": "no_proxy"},
            "fingerprint_config": {
                "browser_kernel_config": {"type": "chrome", "version": "152"},
                "random_ua": {"ua_system_version": ["Mac OS X"], "ua_version": ["152"]},
                "webrtc": "disabled",
            },
        })
        return data["profile_id"]

    def start(self, profile_id: str, restore_tabs: bool = False) -> str:
        data = self.call("/api/v2/browser-profile/start", {
            "profile_id": profile_id, "headless": "1",
            "last_opened_tabs": "1" if restore_tabs else "0", "proxy_detection": "0",
        })
        return data["ws"]["puppeteer"]

    def stop(self, profile_id: str) -> None:
        self.call("/api/v2/browser-profile/stop", {"profile_id": profile_id})

    def delete(self, profile_id: str) -> None:
        self.call("/api/v2/browser-profile/delete", {"profile_id": [profile_id]})


class RemoteBrowser:
    """Only its owning task thread may call Playwright methods."""

    def __init__(self, endpoint: str, start_url: str | None):
        from playwright.sync_api import Error, sync_playwright

        self._mouse_buttons = set()
        self._mouse_modifiers = set()
        self._input_page = None
        self._cancel_guards = []
        self.driver = sync_playwright().start()
        try:
            self.browser = self.driver.chromium.connect_over_cdp(endpoint, timeout=30000)
            self.context = self.browser.contexts[0]
            self.bind_diagnostics(None, None)
            self.enable_diagnostics()
            self.context.set_default_timeout(10000)
            self.context.set_default_navigation_timeout(30000)
            pages = self.pages()
            self.page = (pages[-1] if start_url is None and pages else self.context.new_page())
            self.page.set_viewport_size({"width": 1280, "height": 800})
            if start_url is not None:
                try:
                    self.page.goto(start_url, wait_until="domcontentloaded")
                except Error:
                    warning = self.network_state()
                    if not warning or warning["level"] != "error":
                        raise
        except Exception:
            self.driver.stop()
            raise

    def bind_diagnostics(self, task_id: int | None, profile_id: str | None) -> None:
        self.task_id, self.profile_id = task_id, profile_id

    def _log_event(self, event: str, page=None, error=None) -> None:
        # Do not log URLs, page contents, input values, endpoints or error messages.
        logger.log(logging.WARNING if error or event in {
            "page_crashed", "browser_disconnected", "context_closed"} else logging.INFO,
            "Browser task=%s profile=%s event=%s page=%s current=%s "
            "connected=%s page_closed=%s open_pages=%s disconnect_requested=%s error=%s",
            self.task_id, self.profile_id, event,
            self._page_ids.get(page) if page is not None else None,
            page is getattr(self, "page", None) if page is not None else None,
            self.browser.is_connected(),
            page.is_closed() if page is not None else None, len(self.pages()),
            self.disconnect_requested, type(error).__name__ if error else None)

    def _track_page(self, page) -> None:
        if page in self._page_ids:
            return
        self._page_ids[page] = len(self._page_ids) + 1
        self._network_pages[page] = {"epoch": 0, "problems": {}, "limited": False}
        def closed(closed_page):
            self._log_event("page_closed", closed_page)
            self._page_sessions.pop(closed_page, None)
            self._loading_pages.discard(closed_page)
            self._pending_navigation.pop(closed_page, None)
            self._network_pages.pop(closed_page, None)

        def navigated(frame):
            if frame == page.main_frame:
                if getattr(self, "_input_page", None) is page:
                    self.reset_input()
                self._loading_pages.discard(page)
                self._pending_navigation.pop(page, None)
                self._log_event("page_navigated", page)

        def requested(request):
            if page not in self._network_pages:
                return
            if self._main_request(page, request):
                self._loading_pages.add(page)
                self._pending_navigation.pop(page, None)
                state = self._network_pages[page]
                state["epoch"] += 1
                state["problems"].clear()
                state["limited"] = False
            self._request_epochs[request] = (page, self._network_pages[page]["epoch"])

        def failed(request):
            if self._main_request(page, request):
                self._loading_pages.discard(page)
            reason = request.failure or ""
            if reason == "net::ERR_ABORTED":
                return  # Navigation and closed tabs routinely cancel pending requests.
            self._network_problem(page, request, reason if re.fullmatch(r"net::ERR_[A-Z_]+", reason)
                                  else "NETWORK_ERROR")

        def responded(response):
            request = response.request
            if response.status >= 400:
                self._network_problem(page, request, f"HTTP {response.status}")
            elif self._current_request(page, request):
                self._network_pages[page]["problems"].pop(self._resource_key(request), None)

        page.on("close", closed)
        page.on("crash", lambda crashed: self._log_event("page_crashed", crashed))
        page.on("framenavigated", navigated)
        page.on("request", requested)
        page.on("requestfailed", failed)
        page.on("response", responded)
        self._log_event("page_created", page)

    def enable_diagnostics(self) -> None:
        self._page_ids = {}
        self._page_sessions = {}
        self._loading_pages = set()
        self._pending_navigation = {}
        self._network_pages = {}
        self._request_epochs = WeakKeyDictionary()
        self.disconnect_requested = False
        self.browser.on("disconnected", lambda _: self._log_event("browser_disconnected"))
        self.context.on("close", lambda _: self._log_event("context_closed"))
        self.context.on("page", self._track_page)
        for page in self.context.pages:
            self._track_page(page)

    def _current_request(self, page, request) -> bool:
        state = self._network_pages.get(page)
        return bool(state is not None and self._request_epochs.get(request, (page, 0))
                    == (page, state["epoch"]))

    def _resource_key(self, request):
        return (request.resource_type, hashlib.sha256(request.url.encode()).hexdigest())

    def _main_request(self, page, request) -> bool:
        from playwright.sync_api import Error

        if not request.is_navigation_request():
            return False
        try:
            return request.frame == page.main_frame
        except Error:
            return False  # Some navigation requests precede creation of their frame.

    def _network_problem(self, page, request, reason: str) -> None:
        if not self._current_request(page, request) or not is_web_url(request.url):
            return
        main = self._main_request(page, request)
        kind = request.resource_type
        level = "error" if main else "warning" if kind in {
            "document", "script", "stylesheet", "xhr", "fetch", "websocket", "eventsource"} else "info"
        problem = {"type": kind, "host": urlsplit(request.url).hostname,
                   "reason": reason, "level": level}
        state, key = self._network_pages[page], self._resource_key(request)
        problems = state["problems"]
        if problems.get(key) == problem:
            return
        if key not in problems and len(problems) >= 50:
            victim = next((key for key, value in problems.items() if value["level"] == "info"),
                          next((key for key, value in problems.items() if value["level"] != "error"),
                               next(iter(problems))))
            problems.pop(victim)
            state["limited"] = True
        problems[key] = problem
        logger.warning("Browser task=%s profile=%s event=resource_failed page=%s type=%s reason=%s",
                       self.task_id, self.profile_id, self._page_ids[page], kind, reason)

    def network_state(self) -> dict | None:
        state = self._network_pages.get(self.page)
        if not state or not state["problems"]:
            return None
        order = {"error": 0, "warning": 1, "info": 2}
        problems = sorted(state["problems"].values(), key=lambda item: order[item["level"]])
        level = problems[0]["level"]
        message = {"error": "主页面加载失败，请检查网络或代理后手动刷新",
                   "warning": "部分资源加载失败，可能影响页面功能；可手动刷新",
                   "info": "图片、字体等资源加载失败，详情可查看"}[level]
        return {"level": level, "message": message, "count": len(problems),
                "limited": state["limited"], "details": [dict(item) for item in problems[:8]]}

    def pages(self):
        return [page for page in self.context.pages if not page.is_closed()]

    def cookies(self) -> list[dict]:
        return self.context.cookies()

    def tab_state(self) -> list[dict]:
        return [{"id": self._page_ids[page], "index": index, "url": page.url, "active": page == self.page}
                for index, page in enumerate(self.pages())]

    def restore_tabs(self, saved_tabs: list[dict], start_url: str) -> None:
        from playwright.sync_api import Error

        live = [page for page in self.pages() if is_web_url(page.url)]
        selected = next((tab["url"] for tab in saved_tabs if tab.get("active")), None)
        if live:
            self.page = next((page for page in live if page.url == selected), live[-1])
            self.page.set_viewport_size({"width": 1280, "height": 800})
            return
        targets = [tab for tab in saved_tabs if is_web_url(tab["url"])]
        if not targets:
            targets = [{"url": start_url, "active": True}]
        restored = []
        for index, tab in enumerate(targets):
            pages = self.pages()
            page = pages[0] if index == 0 and pages else self.context.new_page()
            page.set_viewport_size({"width": 1280, "height": 800})
            try:
                page.goto(tab["url"], wait_until="domcontentloaded", timeout=15000)
                restored.append((page, tab.get("active", False)))
            except Error as exc:
                self._log_event("tab_restore_failed", page, exc)
        if restored:
            self.page = next((page for page, active in restored if active), restored[-1][0])
        else:
            pages = self.pages()
            self.page = pages[0] if pages else self.context.new_page()
            try:
                self.page.goto(start_url, wait_until="domcontentloaded", timeout=15000)
            except Error as exc:
                self._log_event("start_page_restore_failed", self.page, exc)
        self._log_event("tabs_restored", self.page)

    def snapshot(self) -> tuple[bytes, list[dict]]:
        pages = self.pages()
        if self.page.is_closed():
            self.page = pages[-1] if pages else self.context.new_page()
        try:
            image = self.page.screenshot(type="jpeg", quality=65, timeout=500)
        except Exception as exc:
            self._log_event("snapshot_failed", self.page, exc)
            raise
        return image, self.tab_state()

    def start_frames(self, publish) -> None:
        self._publish_frame = publish
        self._cast_session = None
        self._cast_page = None
        self._pending_frame = None
        self._last_frame_at = 0
        self._running_casts = set()
        self._frame_handlers = set()
        self._unacked_frames = {}
        self._page_frames = {}
        self._bind_screencast()

    def cdp_session(self):
        if self.page not in self._page_sessions:
            self._page_sessions[self.page] = self.context.new_cdp_session(self.page)
        return self._page_sessions[self.page]

    def _bind_screencast(self) -> None:
        from playwright.sync_api import Error

        page = self.page
        self._pending_frame = self._page_frames.get(page)
        self._cast_page = page
        session = self.cdp_session()
        self._cast_session = session

        def received(event):
            if page not in self._running_casts:
                return
            self._page_frames[page] = event["data"]
            if session is self._cast_session:
                self._pending_frame = event["data"]
            if session is not self._cast_session or page in self._loading_pages:
                # Chromium bounds unacknowledged frames. Do not send a command
                # to a navigating background page: CDP waits for its response.
                self._unacked_frames.setdefault(page, []).append(event["sessionId"])
                return
            try:
                session.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
            except Error:
                pass  # The actor checks the connection and rebinds closed tabs.

        if page not in self._frame_handlers:
            session.on("Page.screencastFrame", received)
            self._frame_handlers.add(page)
        if page not in self._running_casts:
            self._running_casts.add(page)
            session.send("Page.startScreencast", {
                "format": "jpeg", "quality": 65, "maxWidth": 1280, "maxHeight": 800,
            })
        self._log_event("screencast_started", self.page)

    def pump_frames(self) -> None:
        if not self.browser.is_connected():
            raise BrowserError("浏览器连接已断开，请管理员重新打开")
        for page, deadline in list(self._pending_navigation.items()):
            if time.monotonic() >= deadline:
                self._pending_navigation.pop(page)
                self._loading_pages.discard(page)  # Back can be a no-op with no history.
        if self.page.is_closed():
            self.reset_input()
            pages = self.pages()
            self.page = pages[-1] if pages else self.context.new_page()
            self.page.set_viewport_size({"width": 1280, "height": 800})
        if self.page is not self._cast_page:
            self._bind_screencast()
        for page in self._running_casts.copy():
            if page.is_closed():
                self._running_casts.discard(page)
                self._frame_handlers.discard(page)
                self._unacked_frames.pop(page, None)
                self._page_frames.pop(page, None)
            elif page not in self._loading_pages:
                if page is not self.page:
                    self._page_sessions[page].send("Page.stopScreencast")
                    self._running_casts.discard(page)
                    self._unacked_frames.pop(page, None)
                else:
                    for session_id in self._unacked_frames.pop(page, []):
                        self._cast_session.send("Page.screencastFrameAck", {"sessionId": session_id})
        self.page.wait_for_timeout(20)
        # Keep only the latest frame; never drop the final update on a static page.
        if self._pending_frame and time.monotonic() - self._last_frame_at >= 0.1:
            data, self._pending_frame = self._pending_frame, None
            self._last_frame_at = time.monotonic()
            self._publish_frame(base64.b64decode(data))

    def _remove_cancel_guards(self) -> None:
        error = None
        for frame, name in tuple(getattr(self, "_cancel_guards", [])):
            try:
                if not hasattr(frame, "is_detached") or frame.is_detached() is not True:
                    frame.evaluate("""name => {
                        const guard = window[name];
                        if (guard) {
                            for (const type of ['click', 'auxclick', 'dblclick', 'contextmenu'])
                                window.removeEventListener(type, guard, true);
                            delete window[name];
                        }
                    }""", name)
                self._cancel_guards.remove((frame, name))
            except Exception as exc:
                error = error or exc
        if error is not None:
            raise error

    def reset_input(self) -> None:
        page = getattr(self, "_input_page", None)
        buttons = getattr(self, "_mouse_buttons", set())
        modifiers = getattr(self, "_mouse_modifiers", set())
        if page is None or page.is_closed():
            self._mouse_buttons, self._mouse_modifiers, self._input_page = set(), set(), None
            self._cancel_guards = []
            return
        self._remove_cancel_guards()
        self._cancel_guards = []
        error = None
        if buttons:
            try:
                frames = page.frames
                for frame in frames:
                    name = "__taskroom_cancel_" + secrets.token_hex(12)
                    self._cancel_guards.append((frame, name))
                    frame.evaluate("""([name, buttons]) => {
                        const blocked = new Set(buttons);
                        const guard = event => {
                            if (event.isTrusted && blocked.has(event.button)) {
                                event.preventDefault();
                                event.stopImmediatePropagation();
                            }
                        };
                        window[name] = guard;
                        for (const type of ['click', 'auxclick', 'dblclick', 'contextmenu'])
                            window.addEventListener(type, guard, true);
                    }""", [name, [{"left": 0, "middle": 1, "right": 2}[button] for button in buttons]])
                for button in tuple(buttons):
                    try:
                        page.mouse.up(button=button, click_count=0)
                        buttons.discard(button)
                    except Exception as exc:
                        error = error or exc
            except Exception as exc:
                error = error or exc
            finally:
                try:
                    self._remove_cancel_guards()
                except Exception as exc:
                    error = error or exc
        for modifier in tuple(modifiers):
            try:
                page.keyboard.up(modifier)
                modifiers.discard(modifier)
            except Exception as exc:
                error = error or exc
        if not buttons and not modifiers and not self._cancel_guards:
            self._input_page = None
        if error is not None:
            raise error

    def _mouse_input(self, action: dict) -> None:
        if getattr(self, "_input_page", None) not in (None, self.page):
            self.reset_input()
        self._input_page = self.page
        if not hasattr(self, "_mouse_buttons"):
            self._mouse_buttons, self._mouse_modifiers = set(), set()
        kind = action["type"]
        button = action.get("button", "left")
        count = {"click_count": action["click_count"]} if "click_count" in action else {}
        try:
            for modifier in action.get("modifiers", []):
                self._mouse_modifiers.add(modifier)
                self.page.keyboard.down(modifier)
            if kind == "click":
                self._mouse_buttons.add(button)
                self.page.mouse.click(action["x"], action["y"], button=button, **count)
                self._mouse_buttons.discard(button)
            else:
                self.page.mouse.move(action["x"], action["y"])
                if kind == "mouse_down":
                    if button in self._mouse_buttons:
                        raise ValueError("鼠标按键已按住")
                    self._mouse_buttons.add(button)
                    self.page.mouse.down(button=button, **count)
                elif kind == "mouse_up" and button in self._mouse_buttons:
                    self.page.mouse.up(button=button, **count)
                    self._mouse_buttons.discard(button)
                elif kind == "wheel":
                    self.page.mouse.wheel(action["dx"], action["dy"])
        except Exception:
            self.reset_input()
            raise
        finally:
            release_error = None
            for modifier in tuple(self._mouse_modifiers):
                try:
                    self.page.keyboard.up(modifier)
                    self._mouse_modifiers.discard(modifier)
                except Exception as exc:
                    release_error = release_error or exc
            if release_error is not None:
                self.reset_input()
                raise release_error
            if (not self._mouse_buttons and not self._mouse_modifiers
                    and not getattr(self, "_cancel_guards", [])):
                self._input_page = None

    def input(self, action: dict) -> None:
        kind = action["type"]
        if getattr(self, "_cancel_guards", []):
            self.reset_input()
        if kind in {"navigate", "tab", "close_tab", "new_tab", "back", "reload", "reset_input"}:
            self.reset_input()
        if kind in {"click", "wheel", "mouse_down", "mouse_move", "mouse_up"}:
            self._mouse_input(action)
        elif kind == "key":
            key = action["key"]
            if (action.get("primary") or key.lower() == "control+a") and self.page.evaluate(
                    "navigator.platform").startswith("Mac"):
                if action.get("primary") and key.lower() == "control+y":
                    key = "Meta+Shift+z"
                else:
                    key = re.sub(r"(^|\+)Control(?=\+)", r"\1Meta", key)
            try:
                self.page.keyboard.press(key)
            except Exception:
                modifiers, remaining = [], key
                while "+" in remaining:
                    prefix, suffix = remaining.split("+", 1)
                    if prefix not in {"Shift", "Alt", "Control", "Meta"}:
                        break
                    modifiers.append(prefix)
                    remaining = suffix
                for released in [remaining, *reversed(modifiers)]:
                    try:
                        self.page.keyboard.up(released)
                    except Exception:
                        self._input_page = self.page
                        if not hasattr(self, "_mouse_modifiers"):
                            self._mouse_buttons, self._mouse_modifiers = set(), set()
                        self._mouse_modifiers.add(released)  # Retry on the actor before allowing another action.
                raise
        elif kind == "text":
            self.page.keyboard.insert_text(action["text"])
        elif kind == "navigate":
            self.navigate_without_wait("location.assign(" + json.dumps(action["url"]) + ")")
        elif kind == "tab":
            pages = self.pages()
            if "id" in action:
                self.page = self.page_by_id(action["id"])
            else:
                if action["index"] >= len(pages):
                    raise ValueError("标签页已关闭")
                self.page = pages[action["index"]]
            self.page.set_viewport_size({"width": 1280, "height": 800})
        elif kind == "close_tab":
            pages = self.pages()
            target = self.page_by_id(action["id"])
            if len(pages) == 1:
                # Never close the profile's last page before its replacement exists.
                self.page = self.context.new_page()
                self.page.set_viewport_size({"width": 1280, "height": 800})
            elif target is self.page:
                index = pages.index(target)
                self.page = pages[index + 1] if index + 1 < len(pages) else pages[index - 1]
                self.page.set_viewport_size({"width": 1280, "height": 800})
            target.close()
        elif kind == "new_tab":
            self.page = self.context.new_page()
            self.page.set_viewport_size({"width": 1280, "height": 800})
        elif kind == "back":
            self.navigate_without_wait("history.back()")
        elif kind == "reload":
            self.navigate_without_wait("location.reload()")

    def page_by_id(self, page_id: int):
        for page in self.pages():
            if self._page_ids.get(page) == page_id:
                return page
        raise ValueError("标签页已关闭")

    def navigate_without_wait(self, expression: str) -> None:
        # Reuse the page session: detaching it also waits for navigation to commit.
        self._loading_pages.add(self.page)
        self._pending_navigation[self.page] = time.monotonic() + 0.5
        result = self.cdp_session().send("Runtime.evaluate", {
            "expression": "setTimeout(() => { " + expression + "; }, 0)",
            "userGesture": True,
        })
        if result.get("exceptionDetails"):
            self._loading_pages.discard(self.page)
            self._pending_navigation.pop(self.page, None)
            raise BrowserError("无法执行浏览器导航，请稍后重试")

    def close(self) -> None:
        self.disconnect_requested = True
        self._log_event("disconnect_requested")
        try:
            self.reset_input()
        finally:
            self.driver.stop()

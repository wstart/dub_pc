from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from urllib.parse import unquote, urlsplit

from flask import Flask, Response, abort, g, jsonify, render_template, request, session
from flask_sock import Sock

from .browser import AdsPower, BrowserError
from .store import Store
from .tasks import TaskConflict, TaskManager


def valid_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("网址过长或格式错误")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("请输入完整的 HTTP 或 HTTPS 网址")
    return value


def task_values(data: dict) -> dict:
    title = data.get("title", "")
    if not isinstance(title, str):
        raise ValueError("任务名称必须是文字")
    title = title.strip()
    if not title or len(title) > 100:
        raise ValueError("任务名称需要 1–100 个字符")
    try:
        lifetime = int(data.get("lifetime_minutes", 60))
    except (ValueError, TypeError) as exc:
        raise ValueError("任务有效期必须是整数") from exc
    if not 5 <= lifetime <= 1440:
        raise ValueError("任务有效期应为 5–1440 分钟")
    return {"title": title,
            "start_url": valid_url(data.get("start_url", "")),
            "lifetime_minutes": lifetime, "user_proxy_config": proxy_values(data)}


def parse_proxy_text(value: str) -> dict:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("代理内容无效或过长")
    value = value.strip()
    if not value:
        return {"proxy_type": "no_proxy"}
    if any(c in value for c in "\r\n\0"):
        raise ValueError("请只粘贴一条代理")
    kind, address = value.split("://", 1) if "://" in value else ("http", value)
    kind = kind.lower()
    if kind not in {"http", "https", "socks5"}:
        raise ValueError("代理协议只支持 HTTP、HTTPS 或 SOCKS5")
    user, password = "", ""
    endpoint_first = re.match(r"^(?:\[[^\]]+\]|[^:@\s]+):[0-9]+(?::|$)", address)
    if "@" in address and not endpoint_first:
        try:
            parsed = urlsplit(kind + "://" + address)
            host, port = parsed.hostname, parsed.port
            if not host or port is None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError
            user, password = unquote(parsed.username or ""), unquote(parsed.password or "")
        except ValueError as exc:
            raise ValueError("无法识别这条代理，请检查地址和端口是否完整") from exc
    else:
        if "://" in value and address.endswith("/"):
            address = address[:-1]
        if address.startswith("["):
            matched = re.fullmatch(r"\[([^\]]+)\]:([0-9]+)(?::([^:]*):(.*))?", address)
            if not matched:
                raise ValueError("无法识别这条代理，请检查地址和端口是否完整")
            host, port, user, password = matched.groups()
            user, password = user or "", password or ""
        else:
            parts = re.split(r"[\s,，]+", address, maxsplit=3)
            if len(parts) == 1:
                parts = address.split(":", 3)
            if len(parts) not in {2, 4}:
                raise ValueError("无法识别这条代理，请粘贴完整的代理信息")
            host, port = parts[:2]
            if len(parts) == 4:
                user, password = parts[2:]
    return {"proxy_type": kind, "proxy_host": host, "proxy_port": port,
            "proxy_user": user, "proxy_password": password}


def proxy_values(data: dict) -> dict:
    if "proxy" in data:
        data = parse_proxy_text(data["proxy"])
    kind = data.get("proxy_type", "no_proxy")
    if kind == "no_proxy":
        return {"proxy_soft": "no_proxy"}
    if not isinstance(kind, str) or kind not in {"http", "https", "socks5"}:
        raise ValueError("代理类型无效")
    host = data.get("proxy_host", "")
    if not isinstance(host, str):
        raise ValueError("代理地址无效")
    host = host.strip()
    if not host or len(host) > 253:
        raise ValueError("代理地址请填写 IP 或域名，不含协议、端口或账号")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", host):
            raise ValueError("代理地址请填写 IP 或域名，不含协议、端口或账号")
    raw_port = data.get("proxy_port", "")
    if not isinstance(raw_port, (str, int)) or not re.fullmatch(r"[0-9]{1,5}", str(raw_port)):
        raise ValueError("代理端口应为 1–65535 的整数")
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise ValueError("代理端口应为 1–65535 的整数")
    credentials = {}
    for field in ("proxy_user", "proxy_password"):
        value = data.get(field, "")
        if not isinstance(value, str) or len(value) > 256 or any(c in value for c in "\r\n\0"):
            raise ValueError("代理认证信息无效或过长")
        credentials[field] = value
    if credentials["proxy_password"] and not credentials["proxy_user"]:
        raise ValueError("填写代理密码时也需要填写用户名")
    return {"proxy_soft": "other", "proxy_type": kind, "proxy_host": host,
            "proxy_port": str(port), **credentials}


def input_values(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("操作格式无效")
    kind = data.get("type")
    if not isinstance(kind, str):
        raise ValueError("操作类型无效")
    if kind in {"click", "wheel", "mouse_down", "mouse_move", "mouse_up"}:
        def number(name, default, message):
            value = data.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError(message)
            try:
                value = float(value)
            except (ValueError, OverflowError) as exc:
                raise ValueError(message) from exc
            if not math.isfinite(value):
                raise ValueError(message)
            return value

        x, y = number("x", -1, "操作坐标无效"), number("y", -1, "操作坐标无效")
        if not (0 <= x < 1280 and 0 <= y < 800):
            raise ValueError("操作坐标无效")
        result = {"type": kind, "x": x, "y": y}
        if kind in {"click", "mouse_down", "mouse_up"}:
            button = data.get("button", "left")
            if not isinstance(button, str) or button not in {"left", "right", "middle"}:
                raise ValueError("鼠标按键无效")
            result["button"] = button
            if "click_count" in data:
                count = data["click_count"]
                if type(count) is not int or not 1 <= count <= 3:
                    raise ValueError("鼠标点击次数无效")
                result["click_count"] = count
        if kind == "wheel":
            dx, dy = number("dx", 0, "滚动距离无效"), number("dy", 0, "滚动距离无效")
            if not (-2000 <= dx <= 2000 and -2000 <= dy <= 2000):
                raise ValueError("滚动距离无效")
            result.update(dx=dx, dy=dy)
        if "modifiers" in data:
            modifiers = data["modifiers"]
            if (not isinstance(modifiers, list) or len(modifiers) > 4
                    or any(not isinstance(key, str) or key not in {"Shift", "Alt", "Control", "Meta"}
                           for key in modifiers) or len(set(modifiers)) != len(modifiers)):
                raise ValueError("鼠标修饰键无效")
            result["modifiers"] = modifiers[:]
        return result
    if kind == "text":
        text = data.get("text")
        if not isinstance(text, str) or len(text) > 10000:
            raise ValueError("输入文字过长")
        return {"type": kind, "text": text}
    if kind == "key":
        key = data.get("key", "")
        if not isinstance(key, str) or not 1 <= len(key) <= 50:
            raise ValueError("键盘按键无效")
        modifiers, remaining = set(), key
        while "+" in remaining:
            prefix, suffix = remaining.split("+", 1)
            if prefix not in {"Shift", "Alt", "Control", "Meta"}:
                break
            if prefix in modifiers:
                raise ValueError("键盘修饰键重复")
            modifiers.add(prefix)
            remaining = suffix
        names = {"Space", "Backspace", "Tab", "Enter", "Escape", "Delete", "Insert", "Home", "End",
                 "PageUp", "PageDown", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
                 "Shift", "Alt", "Control", "Meta", "CapsLock", "NumLock", "ScrollLock",
                 "Pause", "PrintScreen", "ContextMenu", "Backquote", "Minus", "Equal", "BracketLeft",
                 "BracketRight", "Backslash", "Semicolon", "Quote", "Comma", "Period", "Slash"}
        names.update(f"F{index}" for index in range(1, 13))
        names.update(f"Key{letter}" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        names.update(f"Digit{digit}" for digit in range(10))
        if remaining not in names and not (len(remaining) == 1 and 32 <= ord(remaining) <= 126):
            raise ValueError("键盘按键无效")
        result = {"type": kind, "key": key}
        if "primary" in data:
            if type(data["primary"]) is not bool:
                raise ValueError("主快捷键标识无效")
            result["primary"] = data["primary"]
        return result
    if kind == "navigate":
        return {"type": kind, "url": valid_url(data.get("url", ""))}
    if kind == "close_tab" or (kind == "tab" and "id" in data):
        page_id = data.get("id")
        if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 1:
            raise ValueError("标签页标识无效")
        return {"type": kind, "id": page_id}
    if kind == "tab":
        index = data.get("index", -1)
        if type(index) is not int or not 0 <= index <= 100:
            raise ValueError("标签页无效")
        return {"type": kind, "index": index}
    if kind in {"new_tab", "back", "reload", "reset_input"}:
        return {"type": kind}
    raise ValueError("不支持的操作")


def input_generation(data: dict) -> int | None:
    if "generation" not in data:
        return None
    generation = data["generation"]
    if type(generation) is not int or generation < 0:
        raise ValueError("操作连接版本无效")
    return generation


def input_source(data: dict) -> str | None:
    if "source" not in data:
        return None
    source = data["source"]
    if not isinstance(source, str) or not re.fullmatch(r"[a-f0-9]{32}", source):
        raise ValueError("操作来源标识无效")
    return source


def input_sequence(data: dict) -> int | None:
    if "sequence" not in data:
        return None
    sequence = data["sequence"]
    if type(sequence) is not int or not 1 <= sequence < 2 ** 53:
        raise ValueError("操作顺序标识无效")
    return sequence


def create_app(config=None, ads=None, browser_factory=None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY", ""),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", ""),
        DATABASE=os.environ.get("TASK_DATABASE", os.path.join(app.instance_path, "tasks.sqlite3")),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
        MAX_CONTENT_LENGTH=65536,
        MAX_TASKS=int(os.environ.get("MAX_TASKS", "4")),
        SOCK_SERVER_OPTIONS={"ping_interval": 25, "max_message_size": 65536},
    )
    if config:
        app.config.update(config)
    if not app.config["SECRET_KEY"] or not app.config["ADMIN_PASSWORD"]:
        raise ValueError("请配置 FLASK_SECRET_KEY 和 ADMIN_PASSWORD")
    ads = ads or AdsPower(os.environ.get("ADSPOWER_BASE_URL", "http://127.0.0.1:50325"),
                         os.environ.get("ADSPOWER_API_KEY", ""))
    store = Store(app.config["DATABASE"])
    kwargs = {"browser_factory": browser_factory} if browser_factory else {}
    manager = TaskManager(store, ads, max_tasks=app.config["MAX_TASKS"], **kwargs)
    app.extensions.update(task_store=store, task_manager=manager)
    login_attempts = defaultdict(deque)
    stream_slots = threading.BoundedSemaphore(8)  # Leave four Gunicorn threads for control requests.
    revoked_admin_streams = set()
    sock = Sock(app)

    @app.before_request
    def protect():
        session.setdefault("csrf", secrets.token_urlsafe(32))
        if request.path.startswith(("/api/admin/", "/admin/")) and not session.get("admin"):
            abort(401)
        if request.method == "POST":
            token = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(token, session["csrf"]):
                abort(403)
        if request.endpoint in {"user_control", "admin_control"}:
            origin = urlsplit(request.headers.get("Origin", ""))
            if origin.scheme not in {"http", "https"} or origin.netloc != request.host:
                abort(403)
            is_admin = request.endpoint == "admin_control"
            task = (admin_task(request.view_args["task_id"]) if is_admin
                    else get_task(request.view_args["token"]))
            runtime = manager.runtime(task["id"])
            if is_admin:
                if not runtime.admin_attached or runtime.closing or not session.get("admin_stream_id"):
                    raise TaskConflict("请先打开管理员浏览器")
            else:
                runtime.authorize(controller())
            if not stream_slots.acquire(blocking=False):
                abort(429)
            g.control_slot = True
            g.control_runtime = runtime

    @app.teardown_request
    def release_control_slot(exc):
        if getattr(g, "control_slot", False):
            g.control_slot = False
            stream_slots.release()

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob:; style-src 'self'; "
            "script-src 'self'; frame-ancestors 'none'; form-action 'self'; "
            "connect-src 'self' " + request.host_url.replace("http", "ws", 1).rstrip("/")
        )
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(TaskConflict)
    def conflict(exc):
        return jsonify(error=str(exc)), 409

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(429)
    def http_error(exc):
        return jsonify(error={400: "请求格式无效", 401: "请先登录", 403: "操作验证失败，请刷新页面",
                              404: "任务不存在", 413: "请求过大", 429: "请求过于频繁"}[exc.code]), exc.code

    def json_body():
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("请求必须为 JSON 对象")
        return data

    def get_task(token):
        task = store.by_token(token)
        if not task:
            abort(404)
        return task

    def controller():
        session.setdefault("controller", secrets.token_urlsafe(32))
        return hashlib.sha256(session["controller"].encode()).hexdigest()

    def public_task(task):
        return {key: task[key] for key in
                ("code", "title", "state", "message", "expires_at", "checked_at")}

    @app.get("/")
    def index():
        return render_template("admin.html", admin=bool(session.get("admin")), csrf=session["csrf"])

    @app.post("/api/login")
    def login():
        attempts = login_attempts[request.remote_addr]
        now = time.monotonic()
        while attempts and now - attempts[0] > 60:
            attempts.popleft()
        if len(attempts) >= 5:
            abort(429)
        attempts.append(now)
        password = json_body().get("password", "")
        if not isinstance(password, str) or not hmac.compare_digest(
                password.encode(), app.config["ADMIN_PASSWORD"].encode()):
            return jsonify(error="管理密码不正确"), 401
        session["admin"] = True
        session["admin_stream_id"] = secrets.token_urlsafe(32)
        return jsonify(ok=True)

    @app.post("/api/logout")
    def logout():
        if session.get("admin_stream_id"):
            revoked_admin_streams.add(session["admin_stream_id"])
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/admin/tasks")
    def list_tasks():
        return jsonify(tasks=store.list())

    def admin_task(task_id):
        task = store.get(task_id)
        if not task:
            abort(404)
        return task

    @app.post("/api/admin/tasks/<int:task_id>/cookies")
    @app.post("/api/admin/tasks/<int:task_id>/cookies/download")
    def admin_cookies(task_id):
        task = admin_task(task_id)
        if not task["profile_id"]:
            raise TaskConflict("浏览器环境尚未创建或已释放，无法读取 Cookie")
        runtime = manager.runtimes.get(task_id)
        if not runtime:
            raise TaskConflict("请先点击「管理员打开」，连接就绪后再读取 Cookie")
        try:
            cookies = runtime.cookies()
        except BrowserError:
            return jsonify(error="Cookie 读取失败，请检查浏览器连接后重试"), 502
        if request.path.endswith("/download"):
            filename = f"cookies-{task['code']}.json"
            return Response(json.dumps(cookies, ensure_ascii=False, indent=2),
                            mimetype="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        return jsonify(cookies=cookies, count=len(cookies), fetched_at=time.time())

    @app.get("/admin/tasks/<int:task_id>/browser")
    def admin_browser_page(task_id):
        task = admin_task(task_id)
        session.setdefault("admin_stream_id", secrets.token_urlsafe(32))
        return render_template("task.html", task=task, token=task["token"],
                               csrf=session["csrf"], admin_browser=True)

    @app.get("/api/admin/tasks/<int:task_id>/browser")
    def admin_browser_state(task_id):
        session.setdefault("admin_stream_id", secrets.token_urlsafe(32))
        task = admin_task(task_id)
        value = public_task(task)
        runtime = manager.runtimes.get(task_id)
        value.update(admin_open=False, browser_ready=False, can_control=False,
                     browser_message="浏览器已关闭，可重新打开原环境", tabs=[], input_error="",
                     network_warning=None, stream_generation=-1)
        if runtime:
            with runtime.permissions_gate:
                opened = runtime.thread.is_alive() and runtime.admin_attached and not runtime.closing
                ready = opened and runtime.browser_ready
                value.update(admin_open=opened, browser_ready=ready,
                             can_control=ready and task["state"] not in {"starting", "active", "verifying"},
                             browser_message=runtime.browser_error or (
                                 "浏览器正在关闭，请稍后重新打开" if runtime.closing and runtime.thread.is_alive()
                                 else "正在连接原浏览器" if opened and not ready else value["browser_message"]),
                             tabs=runtime.tabs[:] if ready else [], input_error=runtime.input_error,
                             frame_warning=runtime.frame_warning, stream_generation=runtime.admin_generation)
                value["network_warning"] = runtime.network_warning if ready else None
                if opened:
                    runtime.last_admin_seen = time.monotonic()
        value["can_open"] = bool(task["profile_id"] or task["state"] == "starting") and not (
            runtime and runtime.closing and runtime.thread.is_alive())
        return jsonify(task=value)

    @app.post("/api/admin/tasks/<int:task_id>/browser/open")
    def admin_browser_open(task_id):
        admin_task(task_id)
        manager.open_admin(task_id)
        return jsonify(ok=True), 202

    @app.post("/api/admin/tasks/<int:task_id>/browser/close")
    def admin_browser_close(task_id):
        with manager.lock:
            admin_task(task_id)
            runtime = manager.runtimes.get(task_id)
            if runtime and runtime.thread.is_alive():
                runtime.detach_admin()
        return jsonify(ok=True)

    @app.post("/api/admin/tasks/<int:task_id>/browser/input")
    def admin_browser_input(task_id):
        admin_task(task_id)
        data = json_body()
        manager.runtime(task_id).admin_input(input_values(data), generation=input_generation(data),
                                             source=input_source(data), sequence=input_sequence(data))
        return jsonify(ok=True)

    @app.get("/api/admin/tasks/<int:task_id>/browser/frame")
    def admin_browser_frame(task_id):
        admin_task(task_id)
        runtime = manager.runtime(task_id)
        with runtime.permissions_gate:
            if not runtime.admin_attached or runtime.closing:
                raise TaskConflict("请先打开管理员浏览器")
            runtime.last_admin_seen = time.monotonic()
            if not runtime.frame:
                return Response(status=204)
            return Response(runtime.frame, mimetype="image/jpeg")

    def frame_stream(runtime, allowed):
        if not allowed():
            raise TaskConflict("画面访问权限已收回")
        if not stream_slots.acquire(blocking=False):
            abort(429)
        released = False
        release_lock = threading.Lock()

        def release():
            nonlocal released
            with release_lock:
                if not released:
                    released = True
                    stream_slots.release()

        def frames():
            version, last_sent = -1, 0
            try:
                while allowed():
                    with runtime.frame_changed:
                        runtime.frame_changed.wait_for(
                            lambda: runtime.frame_version != version, timeout=0.2)
                        frame, current = runtime.frame, runtime.frame_version
                    if not allowed():
                        break
                    now = time.monotonic()
                    if frame and (current != version or now - last_sent >= 5):
                        version, last_sent = current, now
                        yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                               + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                    elif not frame:
                        version = current
            finally:
                release()

        response = Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame",
                            headers={"X-Accel-Buffering": "no"})
        response.call_on_close(release)
        return response

    @app.get("/api/admin/tasks/<int:task_id>/browser/stream")
    def admin_browser_stream(task_id):
        admin_task(task_id)
        runtime = manager.runtime(task_id)
        session.setdefault("admin_stream_id", secrets.token_urlsafe(32))
        identity, generation = session["admin_stream_id"], runtime.admin_generation

        def allowed():
            return (identity not in revoked_admin_streams and runtime.admin_attached
                    and not runtime.closing and generation == runtime.admin_generation)

        return frame_stream(runtime, allowed)

    @app.post("/api/admin/tasks")
    def create_task():
        task = manager.create(task_values(json_body()))
        return jsonify(task=task), 201

    @app.post("/api/admin/tasks/<int:task_id>/cancel")
    def cancel_task(task_id):
        if not store.get(task_id):
            abort(404)
        manager.runtime(task_id).cancel()
        return jsonify(ok=True)

    @app.post("/api/admin/tasks/<int:task_id>/reopen")
    def reopen_task(task_id):
        admin_task(task_id)
        manager.reopen(task_id)
        return jsonify(ok=True), 202

    def remove_profile(task_id, delete_record=False):
        with manager.lock:
            task = store.get(task_id)
            if not task:
                abort(404)
            runtime = manager.runtimes.get(task_id)
            if task["state"] in {"starting", "active", "verifying"} or (
                    runtime and runtime.thread.is_alive()):
                raise TaskConflict("浏览器尚未结束，请先取消任务并等待关闭")
            if task["profile_id"]:
                try:
                    ads.delete(task["profile_id"])
                except Exception:
                    return jsonify(error="环境释放失败，任务记录已保留，请检查 AdsPower 服务"), 502
                store.update(task_id, profile_id=None)
            if delete_record:
                store.delete(task_id)
                manager.runtimes.pop(task_id, None)
        return jsonify(ok=True)

    @app.post("/api/admin/tasks/<int:task_id>/release")
    def release_profile(task_id):
        return remove_profile(task_id)

    @app.post("/api/admin/tasks/<int:task_id>/delete")
    def delete_task(task_id):
        if json_body().get("confirm") is not True:
            raise ValueError("请确认永久删除任务记录和浏览器环境")
        return remove_profile(task_id, delete_record=True)

    @app.get("/t/<token>")
    def task_page(token):
        task = get_task(token)
        controller()
        return render_template("task.html", task=public_task(task), token=token, csrf=session["csrf"])

    @app.get("/api/task/<token>")
    def task_state(token):
        task = get_task(token)
        value = public_task(task)
        value["mine"] = bool(task["controller"] and task["controller"] == controller())
        value["claimed"] = bool(task["controller"])
        runtime = manager.runtimes.get(task["id"])
        if value["mine"] and task["state"] == "active" and runtime:
            with runtime.permissions_gate:
                value["tabs"] = runtime.tabs[:]
                value["input_error"] = runtime.input_error
                value["frame_warning"] = runtime.frame_warning
                value["network_warning"] = runtime.network_warning
                value["stream_generation"] = runtime.user_generation
        return jsonify(task=value)

    @app.post("/api/task/<token>/claim")
    def claim(token):
        manager.runtime(get_task(token)["id"]).claim(controller())
        return jsonify(ok=True)

    @app.post("/api/task/<token>/input")
    def browser_input(token):
        data = json_body()
        manager.runtime(get_task(token)["id"]).input(controller(), input_values(data),
                                                     generation=input_generation(data), source=input_source(data),
                                                     sequence=input_sequence(data))
        return jsonify(ok=True)

    @app.post("/api/task/<token>/submit")
    def submit(token):
        manager.runtime(get_task(token)["id"]).submit(controller())
        return jsonify(ok=True)

    @app.get("/api/task/<token>/frame")
    def frame(token):
        runtime = manager.runtime(get_task(token)["id"])
        with runtime.permissions_gate:
            runtime.authorize(controller())
            if not runtime.frame:
                return Response(status=204)
            return Response(runtime.frame, mimetype="image/jpeg")

    @app.get("/api/task/<token>/stream")
    def stream(token):
        runtime = manager.runtime(get_task(token)["id"])
        identity, generation = controller(), runtime.user_generation

        def allowed():
            try:
                runtime.authorize(identity)
            except TaskConflict:
                return False
            return not runtime.closing and generation == runtime.user_generation

        return frame_stream(runtime, allowed)

    def serve_control(ws, is_admin, task_id=None, token=None):
        runtime = g.control_runtime
        identity = session.get("admin_stream_id") if is_admin else controller()
        generation = runtime.admin_generation if is_admin else runtime.user_generation

        def allowed():
            if is_admin:
                return (identity not in revoked_admin_streams and runtime.admin_attached
                        and not runtime.closing and generation == runtime.admin_generation)
            try:
                runtime.authorize(identity)
            except TaskConflict:
                return False
            return not runtime.closing and generation == runtime.user_generation

        def state_value():
            with runtime.permissions_gate:
                response = admin_browser_state(task_id) if is_admin else task_state(token)
                return response.get_json()["task"]

        def message(raw):
            if not isinstance(raw, str):
                raise ValueError("实时请求必须为文字 JSON")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("实时请求必须为 JSON 对象")
            return value

        try:
            auth = message(ws.receive(timeout=5))
            supplied = auth.get("csrf", "")
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, session["csrf"]):
                raise ValueError("实时连接验证失败，请刷新页面")
        except ValueError:
            ws.close(reason=1008)
            return
        source = runtime.register_input_channel()
        try:
            last_state, last_state_at, last_sent_at, version = "", 0, 0, -1
            pending_frames = 0
            chunked_frames = auth.get("frame_chunks") is True
            outgoing_frame, outgoing_offset = None, 0
            pending_chunks = set()
            while ws.connected:
                raw = ws.receive(timeout=0.02)
                if not allowed():
                    ws.send(json.dumps({"type": "state", "task": state_value()}, ensure_ascii=False))
                    ws.close(reason=1008)
                    return
                if raw is not None:
                    request_id = None
                    try:
                        value = message(raw)
                        if value.get("type") == "frame_ack":
                            if not chunked_frames:
                                pending_frames = max(0, pending_frames - 1)
                            elif (type(value.get("id")) is int
                                    and value["id"] == version and type(value.get("offset")) is int
                                    and value["offset"] in pending_chunks):
                                pending_chunks.remove(value["offset"])
                                pending_frames = max(0, pending_frames - 1)
                        elif value.get("type") == "input":
                            if "id" in value:
                                request_id = value["id"]
                                if type(request_id) is not int or request_id < 1:
                                    request_id = None
                                    raise ValueError("实时操作标识无效")
                            data = value.get("action")
                            if not isinstance(data, dict):
                                raise ValueError("操作格式无效")
                            action = input_values(data)
                            input_source(data)  # The WebSocket uses its server-owned channel identity instead.
                            # Old clients have no request IDs and never held mouse buttons.
                            # Keep their already accepted one-shot input compatible.
                            action_source = source if request_id is not None or action["type"] in {
                                "mouse_down", "mouse_move", "mouse_up", "reset_input"} else None
                            with runtime.permissions_gate:
                                if not allowed():
                                    raise TaskConflict("操作权限已收回")
                                if is_admin:
                                    runtime.admin_input(action, source=action_source,
                                                        generation=input_generation(data), sequence=input_sequence(data))
                                else:
                                    runtime.input(identity, action, source=action_source,
                                                  generation=input_generation(data), sequence=input_sequence(data))
                            if request_id is not None:
                                ws.send(json.dumps({"type": "input_ack", "id": request_id}))
                        else:
                            raise ValueError("不支持的实时操作")
                    except (ValueError, TypeError, TaskConflict) as exc:
                        response = {"type": "error", "error": str(exc) if not isinstance(exc, TypeError)
                                    else "操作格式无效"}
                        if request_id is not None:
                            response["id"] = request_id
                        ws.send(json.dumps(response, ensure_ascii=False))
                now = time.monotonic()
                if now - last_state_at >= 0.04:
                    serialized = json.dumps({"type": "state", "task": state_value()}, ensure_ascii=False)
                    if serialized != last_state or now - last_sent_at >= 1:
                        ws.send(serialized)
                        last_state, last_sent_at = serialized, now
                    last_state_at = now
                if pending_frames < (2 if chunked_frames else 4):
                    if outgoing_frame is None and (not chunked_frames or pending_frames == 0):
                        with runtime.frame_changed:
                            frame, current = runtime.frame, runtime.frame_version
                        if frame and current != version and allowed():
                            version = current
                            if chunked_frames:
                                outgoing_frame, outgoing_offset = frame, 0
                                ws.send(json.dumps({"type": "frame_start", "id": version,
                                                    "size": len(frame)}))
                            else:
                                ws.send(frame)
                                pending_frames += 1
                    if outgoing_frame is not None and allowed():
                        # Bound picture bytes ahead of control replies on a slow link.
                        end = min(outgoing_offset + 16384, len(outgoing_frame))
                        ws.send(outgoing_frame[outgoing_offset:end])
                        outgoing_offset = end
                        pending_chunks.add(end)
                        if end == len(outgoing_frame):
                            outgoing_frame = None
                        pending_frames += 1
        finally:
            runtime.unregister_input_channel(source)

    @sock.route("/api/task/<token>/control")
    def user_control(ws, token):
        serve_control(ws, False, token=token)

    @sock.route("/api/admin/tasks/<int:task_id>/browser/control")
    def admin_control(ws, task_id):
        serve_control(ws, True, task_id=task_id)

    @app.get("/health")
    def health():
        return jsonify(ok=True)

    return app

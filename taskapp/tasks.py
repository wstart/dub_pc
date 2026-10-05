from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
import time
import traceback
from concurrent.futures import Future, TimeoutError as CookieTimeout

from playwright.sync_api import Error as PlaywrightError, TimeoutError as CaptureTimeout

from .browser import BrowserError, RemoteBrowser, is_web_url


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class TaskConflict(RuntimeError):
    pass


class TaskRuntime:
    def __init__(self, task_id: int, store, ads, browser_factory=RemoteBrowser,
                 proxy_config=None, resume=False, reopen=False):
        self.task_id, self.store, self.ads = task_id, store, ads
        self.browser_factory = browser_factory
        self.proxy_config = proxy_config
        self.resume = resume
        self.reopen = reopen
        # Browser execution and revocation remain serialized by gate. Network
        # admission only takes the short metadata lock, never a Playwright RPC.
        self.gate = threading.RLock()
        self.permissions_gate = threading.RLock()
        self.commands = queue.Queue(maxsize=80)
        self.cookie_requests = queue.Queue(maxsize=2)
        self.input_channels = set()
        self.input_epochs = {}
        self.input_sequences = {}
        self.input_owner = None
        self.input_buttons = set()
        self.input_deadline = 0
        self.input_release_pending = False
        self.input_release_owners = set()
        self.input_release_failures = 0
        self.input_release_retry_at = 0
        # Fresh runtime instances must not reuse the generation of a closed task.
        # Leave ample increment room within JavaScript's exactly represented integers.
        self.user_generation = secrets.randbits(48) + 1
        self.frame = b""
        self.frame_changed = threading.Condition()
        self.frame_version = 0
        self.frame_warning = ""
        self.network_warning = None
        self.tabs = []
        self.input_error = ""
        self.admin_attached = resume and not reopen
        self.admin_generation = secrets.randbits(48) + 1
        self.last_admin_seen = time.monotonic()
        self.browser_ready = False
        self.closing = False
        self.browser_error = ""
        self.saved_tabs = store.get(task_id)["browser_tabs"]
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def publish_frame(self, frame: bytes):
        with self.permissions_gate, self.frame_changed:
            if not self.closing and (self.store.get(self.task_id)["state"] == "active"
                                     or self.admin_attached):
                self.frame = frame
                self.frame_version += 1
                self.frame_warning = ""
                self.frame_changed.notify_all()

    def claim(self, controller: str):
        with self.permissions_gate:
            task = self.store.get(self.task_id)
            if task["state"] != "active" or time.time() >= task["expires_at"]:
                raise TaskConflict("任务当前不可操作")
            if task["controller"] and task["controller"] != controller:
                raise TaskConflict("任务已由另一个会话领取")
            self.store.update(self.task_id, controller=controller)

    def authorize(self, controller: str):
        with self.permissions_gate:
            task = self.store.get(self.task_id)
            if (task["state"] != "active" or not task["controller"]
                    or task["controller"] != controller
                    or time.time() >= task["expires_at"]):
                raise TaskConflict("操作权限已锁定，或任务未由此会话领取")

    def register_input_channel(self):
        with self.permissions_gate:
            source = object()
            self.input_channels.add(source)
            return source

    def unregister_input_channel(self, source):
        with self.permissions_gate:
            self.input_channels.discard(source)
            if self.input_owner is not None and self.input_owner[2] is source:
                self.input_release_owners.add(self.input_owner)
            for owner in list(self.input_epochs):
                if owner[2] is source:
                    self.input_epochs.pop(owner)
            for owner in list(self.input_sequences):
                if owner[2] is source:
                    self.input_sequences.pop(owner)

    def _queue_input(self, role, generation, action, source, sequence):
        if source is not None and not isinstance(source, str) and source not in self.input_channels:
            raise TaskConflict("操作连接已断开，请重新连接")
        owner = (role, generation, source)
        if sequence is not None and sequence <= self.input_sequences.get(owner, 0):
            raise TaskConflict("操作已过期或重复，请重新操作")
        if action["type"] == "reset_input":
            # A safety release must not depend on a free slot in the input queue.
            self.input_epochs[owner] = self.input_epochs.get(owner, 0) + 1
            self.input_release_owners.add(owner)
            if sequence is not None:
                self.input_sequences[owner] = sequence
            return
        if (self.input_owner is not None and self.input_owner != owner
                and not self.input_release_pending and self.input_owner not in self.input_release_owners
                and time.monotonic() < self.input_deadline):
            raise TaskConflict("另一个连接正在拖动，请在原窗口松开鼠标后重试")
        payload = (generation, action, source, self.input_epochs.get(owner, 0))
        kind = "input" if role == "user" else "admin_input"
        try:
            merged = False
            if action["type"] == "mouse_move":
                # Replace only an adjacent move; discrete input and source/role
                # changes remain barriers.  Queue.get uses the same mutex.
                with self.commands.mutex:
                    if self.commands.queue:
                        previous_kind, previous = self.commands.queue[-1]
                        if (previous_kind == kind and len(previous) == 4
                                and previous[0] == generation and previous[2:] == payload[2:]
                                and previous[1]["type"] == "mouse_move"
                                and previous[1].get("modifiers", []) == action.get("modifiers", [])):
                            self.commands.queue[-1] = (kind, payload)
                            merged = True
            if not merged:
                self.commands.put_nowait((kind, payload))
            if sequence is not None:
                self.input_sequences[owner] = sequence
        except queue.Full as exc:
            if action["type"] == "mouse_up":
                self.input_epochs[owner] = self.input_epochs.get(owner, 0) + 1
                self.input_release_owners.add(owner)
                if sequence is not None:
                    self.input_sequences[owner] = sequence
                return
            raise TaskConflict("操作过快，请稍后再试") from exc

    def input(self, controller: str, action: dict, source=None, generation=None, sequence=None):
        with self.permissions_gate:
            self.authorize(controller)
            if generation is not None and generation != self.user_generation:
                raise TaskConflict("操作连接版本已过期，请重新连接")
            self._queue_input("user", self.user_generation, action, source, sequence)

    def attach_admin(self):
        with self.gate, self.permissions_gate:
            if self.closing:
                raise TaskConflict("浏览器正在关闭，请稍后重新打开")
            self.admin_attached = True
            self.last_admin_seen = time.monotonic()
            self.store.update(self.task_id, admin_open=1)
            logger.info("Task %s event=admin_attached state=%s", self.task_id,
                        self.store.get(self.task_id)["state"])

    def detach_admin(self):
        with self.gate, self.permissions_gate:
            self.admin_attached = False
            self.admin_generation += 1
            if self.input_owner is not None and self.input_owner[0] == "admin":
                self.input_release_owners.add(self.input_owner)
            self.store.update(self.task_id, admin_open=0)
            logger.info("Task %s event=admin_detached state=%s", self.task_id,
                        self.store.get(self.task_id)["state"])
            if self.store.get(self.task_id)["state"] not in {"starting", "active", "verifying"}:
                self.frame = b""

    def authorize_admin(self):
        with self.permissions_gate:
            if (not self.admin_attached or not self.browser_ready or self.closing
                    or self.store.get(self.task_id)["state"] in {"starting", "active", "verifying"}):
                raise TaskConflict("用户操作期间仅允许观察；结束后管理员才能操作")

    def admin_input(self, action: dict, source=None, generation=None, sequence=None):
        with self.permissions_gate:
            self.authorize_admin()
            if generation is not None and generation != self.admin_generation:
                raise TaskConflict("操作连接版本已过期，请重新连接")
            self.last_admin_seen = time.monotonic()
            self._queue_input("admin", self.admin_generation, action, source, sequence)

    def _release_input(self, browser):
        with self.permissions_gate:
            needs_release = self.input_owner is not None or bool(self.input_buttons)
        if needs_release:
            try:
                if hasattr(browser, "reset_input"):
                    browser.reset_input()
            except Exception as exc:
                logger.warning("Task %s event=input_release_failed error=%s",
                               self.task_id, type(exc).__name__)
                with self.permissions_gate:
                    self.input_release_pending = True
                    self.input_release_failures += 1
                    self.input_release_retry_at = time.monotonic() + 0.2
                    self.input_error = "鼠标操作正在恢复，请稍候再试"
                return False
        with self.permissions_gate:
            self.input_owner = None
            self.input_buttons.clear()
            self.input_deadline = 0
            self.input_release_failures = 0
            self.input_release_retry_at = 0
        return True

    def _input_is_valid(self, owner, epoch=None):
        role, generation, source = owner
        task = self.store.get(self.task_id)
        valid_role = (generation == self.user_generation and task["state"] == "active"
                      and time.time() < task["expires_at"] if role == "user" else
                      generation == self.admin_generation and self.admin_attached
                      and task["state"] not in {"starting", "active", "verifying"})
        return (valid_role and not self.closing
                and (source is None or isinstance(source, str) or source in self.input_channels)
                and (epoch is None or epoch == self.input_epochs.get(owner, 0)))

    def _flush_input_release(self, browser):
        with self.permissions_gate:
            owner = self.input_owner
            generations = self.user_generation, self.admin_generation
            markers = {item: self.input_epochs.get(item, 0)
                       for item in self.input_release_owners}
            release = owner is not None and (
                self.input_release_pending or owner in markers or not self._input_is_valid(owner)
                or time.monotonic() >= self.input_deadline)
            if release:
                if time.monotonic() < self.input_release_retry_at:
                    return False
        if release:
            if not self._release_input(browser):
                with self.permissions_gate:
                    if self.input_release_failures >= 3:
                        raise BrowserError("无法释放鼠标操作，浏览器已安全关闭，请管理员重新打开")
                return False
        with self.permissions_gate:
            if generations == (self.user_generation, self.admin_generation):
                self.input_release_pending = False
            # reset/disconnect can arrive during the release RPC. Only consume
            # markers captured before it; a newer reset epoch must survive.
            for item, epoch in markers.items():
                if self.input_epochs.get(item, 0) == epoch:
                    self.input_release_owners.discard(item)
        return True

    def _dispatch_input(self, browser, role, payload):
        generation, action = payload[:2]
        source = payload[2] if len(payload) > 2 else None
        epoch = payload[3] if len(payload) > 3 else 0
        owner = (role, generation, source)
        with self.permissions_gate:
            if not self._input_is_valid(owner, epoch):
                return
        if not self._flush_input_release(browser):
            return
        with self.permissions_gate:
            if not self._input_is_valid(owner, epoch):
                return
            if self.input_owner is not None and self.input_owner != owner:
                self.input_error = "另一个连接正在拖动，请在原窗口松开鼠标后重试"
                return  # An observer or old socket cannot end another source's drag.
        kind = action["type"]
        if kind in {"navigate", "tab", "close_tab", "new_tab", "back", "reload", "reset_input"}:
            if not self._release_input(browser):
                return
        with self.permissions_gate:
            if not self._input_is_valid(owner, epoch):
                return
            if kind == "mouse_down":
                # Reserve before RPC so a disconnect/reset arriving while down
                # is in flight can request actor-owned cleanup of this source.
                self.input_owner = owner
                self.input_buttons.add(action.get("button", "left"))
                self.input_deadline = time.monotonic() + 15
        started = time.monotonic()
        try:
            browser.input(action)
        except Exception as exc:
            logger.warning("Task %s event=input_failed mode=%s action=%s error=%s",
                           self.task_id, role, kind, type(exc).__name__)
            with self.permissions_gate:
                self.input_error = "操作未完成，请检查网址或重试"
                if self.input_owner is None:
                    self.input_owner = owner
            self._release_input(browser)
        else:
            with self.permissions_gate:
                self.input_error = ""
                if kind == "mouse_up":
                    self.input_buttons.discard(action.get("button", "left"))
                    if not self.input_buttons:
                        self.input_owner, self.input_deadline = None, 0
                elif self.input_owner is not None and kind in {"mouse_down", "mouse_move", "wheel"}:
                    self.input_deadline = time.monotonic() + 15
            self._flush_input_release(browser)
            return True
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= 1:
                logger.warning("Task %s event=input_slow mode=%s action=%s elapsed=%.3f pending_commands=%s",
                               self.task_id, role, kind, elapsed, self.commands.qsize())

    def cookies(self) -> list[dict]:
        with self.permissions_gate:
            if not self.thread.is_alive() or self.closing or not self.browser_ready:
                raise TaskConflict("请先点击「管理员打开」，连接就绪后再读取 Cookie")
            result = Future()
            try:
                self.cookie_requests.put_nowait(result)
            except queue.Full as exc:
                raise TaskConflict("Cookie 正在读取，请稍后再试") from exc
        try:
            return result.result(timeout=5)
        except CookieTimeout as exc:
            result.cancel()
            raise TaskConflict("Cookie 读取超时，请稍后重试；浏览器操作权限未改变") from exc

    def submit(self, controller: str):
        with self.gate, self.permissions_gate:
            self.authorize(controller)
            self.store.update(self.task_id, state="completed", message="用户已完成任务，操作权限已锁定",
                              checked_at=time.time())
            self.user_generation += 1
            self.input_release_pending = True
            # Discard all pending user commands. An in-flight operation is serialized
            # by gate, so completion always occurs after user input has finished.
            while True:
                try:
                    self.commands.get_nowait()
                except queue.Empty:
                    break
            self.frame = b""
            logger.info("Task %s event=user_completed", self.task_id)

    def reopen_user(self):
        with self.gate, self.permissions_gate:
            if self.closing or not self.browser_ready:
                raise TaskConflict("浏览器正在启动或关闭，请稍后再试")
            task = self.store.get(self.task_id)
            if task["state"] in {"starting", "active", "verifying"}:
                raise TaskConflict("任务已经开放")
            self.user_generation += 1
            self.admin_generation += 1
            self.input_release_pending = True
            while True:
                try:
                    self.commands.get_nowait()
                except queue.Empty:
                    break
            self.input_error = ""
            self.store.update(self.task_id, state="active", message="管理员已重新开放，请继续操作",
                              checked_at=None, expires_at=time.time() + task["lifetime_minutes"] * 60)
            logger.info("Task %s event=user_reopened same_browser=True", self.task_id)

    def cancel(self):
        with self.gate:
            self.detach_admin()
            with self.permissions_gate:
                self.input_release_pending = True
                if self.store.get(self.task_id)["state"] in {"starting", "active", "verifying"}:
                    self.store.update(self.task_id, state="cancelled", message="管理员已取消任务")
                    self.frame = b""

    def save_tabs(self, tabs: list[dict], allow_empty: bool = False) -> None:
        saved = [{"url": tab["url"], "active": tab["active"]}
                 for tab in tabs if is_web_url(tab["url"])]
        if saved or allow_empty:
            serialized = json.dumps(saved, ensure_ascii=False)
            with self.permissions_gate:
                if serialized != self.saved_tabs:
                    self.store.update(self.task_id, browser_tabs=serialized)
                    self.saved_tabs = serialized

    def run(self):
        browser = None
        profile_id = None
        phase = "create_profile"
        close_reason = "task_ended"
        try:
            task = self.store.get(self.task_id)
            profile_id = (task["profile_id"] if self.resume else self.ads.create(
                {**task, "user_proxy_config": self.proxy_config}))
            self.proxy_config = None
            self.store.update(self.task_id, profile_id=profile_id)
            if not self.resume and self.store.get(self.task_id)["state"] != "starting":
                return
            phase = "start_profile"
            logger.info("Task %s profile=%s event=profile_start_requested resume=%s",
                        self.task_id, profile_id, self.resume)
            endpoint = (self.ads.start(profile_id, restore_tabs=True) if self.resume
                        else self.ads.start(profile_id))
            phase = "connect_browser"
            browser = self.browser_factory(endpoint, None if self.resume else task["start_url"])
            if isinstance(browser, RemoteBrowser):
                browser.bind_diagnostics(self.task_id, profile_id)
                if self.resume:
                    phase = "restore_tabs"
                    browser.restore_tabs(json.loads(self.saved_tabs), task["start_url"])
            logger.info("Task %s profile=%s event=browser_ready resume=%s",
                        self.task_id, profile_id, self.resume)
            with self.gate, self.permissions_gate:
                if not self.resume and self.store.get(self.task_id)["state"] != "starting":
                    return
                self.browser_ready = True
                if not self.resume or self.reopen:
                    if self.store.get(self.task_id)["state"] != "starting":
                        return
                    self.store.update(self.task_id, state="active", message=(
                        "管理员已重新开放，请继续操作" if self.reopen else "浏览器已就绪，请领取任务"))
            native_frames = False
            if isinstance(browser, RemoteBrowser):
                try:
                    browser.start_frames(self.publish_frame)
                    native_frames = True
                except PlaywrightError as exc:
                    logger.warning("Task %s event=screencast_unavailable error=%s",
                                   self.task_id, type(exc).__name__)
            next_frame = 0
            while True:
                phase = "wait"
                with self.gate:
                    self._flush_input_release(browser)
                    with self.permissions_gate:
                        admin_idle = self.admin_attached and time.monotonic() - self.last_admin_seen >= 900
                    if admin_idle:
                        self.detach_admin()
                    with self.permissions_gate:
                        task = self.store.get(self.task_id)
                        if task["state"] not in {"active", "verifying"} and not self.admin_attached:
                            self.closing = True
                            return
                        expired = (task["state"] in {"active", "verifying"}
                                   and time.time() >= task["expires_at"])
                        if expired:
                            self.store.update(self.task_id, state="expired", message="任务已到期，操作权限已收回")
                            self.input_release_pending = True
                    if expired:
                        self._flush_input_release(browser)
                        with self.permissions_gate:
                            self.frame = b""
                        continue
                try:
                    kind, action = self.commands.get(timeout=0.001 if native_frames else 0.02)
                except queue.Empty:
                    kind, action = None, None
                try:
                    cookie_result = self.cookie_requests.get_nowait()
                except queue.Empty:
                    cookie_result = None
                if cookie_result is not None and cookie_result.set_running_or_notify_cancel():
                    try:
                        cookie_result.set_result(browser.cookies())
                    except Exception:
                        cookie_result.set_exception(BrowserError("Cookie 读取失败，请检查浏览器连接后重试"))
                input_applied = False
                if kind == "input":
                    phase = "user_input"
                    with self.gate:
                        input_applied = self._dispatch_input(browser, "user", action)
                elif kind == "admin_input":
                    phase = "admin_input"
                    with self.gate:
                        input_applied = self._dispatch_input(browser, "admin", action)
                if native_frames:
                    phase = "screencast"
                    browser.pump_frames()
                if kind:
                    next_frame = 0  # Publish tab changes immediately after an operation.
                if time.monotonic() >= next_frame:
                    with self.permissions_gate:
                        visible = self.store.get(self.task_id)["state"] == "active" or self.admin_attached
                    if visible:
                        phase = "snapshot"
                        try:
                            if native_frames:
                                tabs = browser.tab_state()
                            else:
                                frame, tabs = browser.snapshot()
                                self.publish_frame(frame)
                            network_warning = (browser.network_state()
                                               if hasattr(browser, "network_state") else None)
                            with self.permissions_gate:
                                self.tabs = tabs
                                self.network_warning = network_warning
                                self.save_tabs(tabs, allow_empty=(
                                    input_applied and action[1]["type"] == "close_tab"
                                    and not self.input_error))
                        except CaptureTimeout:
                            with self.permissions_gate:
                                if not self.frame_warning:
                                    logger.warning("Task %s event=capture_timeout retry=True", self.task_id)
                                self.frame_warning = "画面暂时延迟，正在恢复；操作权限未关闭"
                    next_frame = time.monotonic() + (0.2 if native_frames else 0.5)
        except Exception as exc:
            close_reason = "runtime_error"
            logger.error("Task %s profile=%s event=runtime_failed error=%s phase=%s "
                         "state=%s resume=%s admin_attached=%s pending_commands=%s stack=%s",
                         self.task_id, profile_id, type(exc).__name__, phase,
                         self.store.get(self.task_id)["state"], self.resume,
                         self.admin_attached, self.commands.qsize(),
                         "".join(traceback.format_tb(exc.__traceback__)))
            with self.gate, self.permissions_gate:
                self.browser_error = "浏览器连接异常，请关闭后重新打开或检查 AdsPower 服务"
                task = self.store.get(self.task_id)
                if task["state"] in {"starting", "active", "verifying"}:
                    message = (str(exc) if isinstance(exc, (BrowserError, ValueError))
                               else "浏览器连接发生异常，请联系管理员查看日志")
                    self.store.update(self.task_id, state="error", message=message,
                                      checked_at=time.time())
        finally:
            self.proxy_config = None
            with self.gate:
                if browser:
                    self._release_input(browser)
                with self.permissions_gate:
                    self.input_channels.clear()
                    self.input_epochs.clear()
                    self.input_sequences.clear()
                    self.closing = True
                    self.browser_ready = False
                    self.admin_attached = False
                    self.store.update(self.task_id, admin_open=0)
                    self.frame = b""
                    while True:
                        try:
                            result = self.cookie_requests.get_nowait()
                        except queue.Empty:
                            break
                        if result.set_running_or_notify_cancel():
                            result.set_exception(TaskConflict("浏览器已关闭，请先点击「管理员打开」"))
            if browser:
                if isinstance(browser, RemoteBrowser):
                    try:
                        self.save_tabs(browser.tab_state())
                    except Exception as exc:
                        logger.warning("Task %s event=save_tabs_failed error=%s",
                                       self.task_id, type(exc).__name__)
                try:
                    browser.close()
                except Exception:
                    logging.exception("Unable to disconnect task %s", self.task_id)
            if profile_id:
                try:
                    logger.info("Task %s profile=%s event=profile_stop_requested reason=%s state=%s",
                                self.task_id, profile_id, close_reason, self.store.get(self.task_id)["state"])
                    self.ads.stop(profile_id)
                    logger.info("Task %s profile=%s event=profile_stopped", self.task_id, profile_id)
                except Exception:
                    logging.exception("Unable to stop task profile %s", self.task_id)


class TaskManager:
    def __init__(self, store, ads, browser_factory=RemoteBrowser, max_tasks=4):
        self.store, self.ads = store, ads
        self.browser_factory, self.max_tasks = browser_factory, max_tasks
        self.lock = threading.Lock()
        self.runtimes = {}
        profiles = store.recover()
        if profiles:
            def stop_orphans():
                for profile in profiles:
                    try:
                        ads.stop(profile)
                    except Exception:
                        logging.exception("Unable to close interrupted profile")
            threading.Thread(target=stop_orphans, daemon=True).start()

    def create(self, values: dict) -> dict:
        with self.lock:
            self.runtimes = {key: runtime for key, runtime in self.runtimes.items()
                             if runtime.thread.is_alive()}
            if len(self.runtimes) >= self.max_tasks:
                raise TaskConflict("同时运行任务已达上限，请等待现有任务结束")
            task = self.store.create(values)
            runtime = TaskRuntime(task["id"], self.store, self.ads, self.browser_factory,
                                  proxy_config=values.get("user_proxy_config"))
            self.runtimes[task["id"]] = runtime
            runtime.start()
            return task

    def runtime(self, task_id: int) -> TaskRuntime:
        runtime = self.runtimes.get(task_id)
        if not runtime or not runtime.thread.is_alive():
            raise TaskConflict("浏览器会话已经结束")
        return runtime

    def open_admin(self, task_id: int):
        with self.lock:
            task = self.store.get(task_id)
            if not task:
                raise TaskConflict("任务不存在")
            runtime = self.runtimes.get(task_id)
            if runtime and runtime.thread.is_alive():
                runtime.attach_admin()
                return
            if task["state"] in {"starting", "active", "verifying"}:
                raise TaskConflict("用户浏览器会话尚未就绪")
            if not task["profile_id"]:
                raise TaskConflict("浏览器环境已释放，无法再次打开")
            self.runtimes = {key: value for key, value in self.runtimes.items() if value.thread.is_alive()}
            if len(self.runtimes) >= self.max_tasks:
                raise TaskConflict("同时运行浏览器已达上限，请先关闭其他会话")
            runtime = TaskRuntime(task_id, self.store, self.ads, self.browser_factory, resume=True)
            self.store.update(task_id, admin_open=1)
            self.runtimes[task_id] = runtime
            runtime.start()

    def reopen(self, task_id: int):
        with self.lock:
            task = self.store.get(task_id)
            if not task or not task["profile_id"]:
                raise TaskConflict("浏览器环境已释放，无法重新开放")
            if task["state"] in {"starting", "active", "verifying"}:
                raise TaskConflict("任务已经开放")
            runtime = self.runtimes.get(task_id)
            if runtime and runtime.thread.is_alive():
                runtime.reopen_user()
                return
            self.runtimes = {key: value for key, value in self.runtimes.items() if value.thread.is_alive()}
            if len(self.runtimes) >= self.max_tasks:
                raise TaskConflict("同时运行浏览器已达上限，请先关闭其他会话")
            self.store.update(task_id, state="starting", message="正在重新开放原浏览器",
                              checked_at=None, expires_at=time.time() + task["lifetime_minutes"] * 60)
            runtime = TaskRuntime(task_id, self.store, self.ads, self.browser_factory, resume=True, reopen=True)
            self.runtimes[task_id] = runtime
            logger.info("Task %s event=user_reopened same_browser=False", task_id)
            runtime.start()

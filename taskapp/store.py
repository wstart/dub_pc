from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY,
                    code TEXT UNIQUE NOT NULL,
                    token TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    start_url TEXT NOT NULL,
                    check_url TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    ignore_case INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'starting',
                    message TEXT NOT NULL DEFAULT '正在准备浏览器',
                    profile_id TEXT,
                    controller TEXT,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    checked_at REAL
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
            if "proxy_label" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN proxy_label TEXT NOT NULL DEFAULT ''")
            if "admin_open" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN admin_open INTEGER NOT NULL DEFAULT 0")
            if "browser_tabs" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN browser_tabs TEXT NOT NULL DEFAULT '[]'")
            if "lifetime_minutes" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN lifetime_minutes INTEGER NOT NULL DEFAULT 60")
                db.execute("UPDATE tasks SET lifetime_minutes=MAX(5,MIN(1440,CAST((expires_at-created_at)/60 AS INTEGER)))")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, values: dict) -> dict:
        proxy = values.get("user_proxy_config", {})
        host = proxy.get("proxy_host", "")
        if ":" in host:
            host = f"[{host}]"
        proxy_label = (f"{proxy['proxy_type']}://{host}:{proxy['proxy_port']}"
                       if proxy.get("proxy_soft") == "other" else "")
        with self.connect() as db:
            cursor = db.execute(
                """INSERT INTO tasks
                (code,token,title,start_url,check_url,pattern,content_type,
                 ignore_case,created_at,expires_at,proxy_label,lifetime_minutes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (secrets.token_hex(6).upper(), secrets.token_urlsafe(32),
                 values["title"], values["start_url"], "", "", "", 0, time.time(),
                 time.time() + values["lifetime_minutes"] * 60, proxy_label, values["lifetime_minutes"]),
            )
            task_id = cursor.lastrowid
        return self.get(task_id)

    def get(self, task_id: int) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def by_token(self, token: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 200").fetchall()
        return [dict(row) for row in rows]

    def update(self, task_id: int, **values) -> None:
        allowed = {"state", "message", "profile_id", "checked_at", "controller", "admin_open", "expires_at", "browser_tabs"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid task update")
        with self.connect() as db:
            fields = ",".join(f"{key}=?" for key in values)
            db.execute(f"UPDATE tasks SET {fields} WHERE id=?",
                       (*values.values(), task_id))

    def delete(self, task_id: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def recover(self) -> list[str]:
        with self.connect() as db:
            profiles = [row[0] for row in db.execute(
                "SELECT DISTINCT profile_id FROM tasks WHERE (state IN ('starting','active','verifying')"
                " OR admin_open=1)"
                " AND profile_id IS NOT NULL"
            )]
            db.execute(
                "UPDATE tasks SET state='error', message='服务重启，任务已锁定。请联系管理员重新开放。'"
                " WHERE state IN ('starting','active','verifying')"
            )
            db.execute("UPDATE tasks SET admin_open=0 WHERE admin_open=1")
        return profiles

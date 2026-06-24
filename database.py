"""SQLite storage for bot admins and the audit log.

The bot's own database only tracks *who* is allowed to manage the kitten
catalog and what they did -- the catalog itself lives on the website's
MySQL database, reachable only through catalog_api.py. Telegram's chat ID
is the actual security boundary (only a chat that owns telegram_id can act
as that admin); the login/password collected during /register is not used
to authenticate anything yet -- it's a CRM-style identity record and a
foundation for a future web panel.
"""

from __future__ import annotations

import os
import sqlite3
import hashlib
import logging

DB_PATH = os.getenv("DB_PATH", "sphynx_crm_bot.db")

logger = logging.getLogger("sphynx_crm.db")


def get_db_connection() -> sqlite3.Connection:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        login TEXT UNIQUE,
        password_hash TEXT,
        status TEXT DEFAULT 'pending_approval', -- pending_approval, approved, rejected, banned
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_telegram_id INTEGER,
        actor_label TEXT, -- snapshot of @username/full name at the time of the action
        action TEXT,
        details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    conn.commit()
    conn.close()
    logger.info("Database initialized successfully.")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt.hex() + ":" + pwd_hash.hex()


def register_admin(telegram_id: int, username: str, login: str, password_plain: str) -> bool:
    pwd_hash = hash_password(password_plain)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO admins (telegram_id, username, login, password_hash, status) "
            "VALUES (?, ?, ?, ?, 'pending_approval')",
            (telegram_id, username, login, pwd_hash),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def is_login_taken(login: str) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM admins WHERE login = ?", (login,))
    row = cursor.fetchone()
    conn.close()
    return row is not None


def get_admin(telegram_id: int) -> sqlite3.Row | None:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM admins WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def get_all_admins() -> list[sqlite3.Row]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM admins ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return rows


def update_admin_status(telegram_id: int, status: str) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE admins SET status = ? WHERE telegram_id = ?", (status, telegram_id))
    conn.commit()
    conn.close()


def add_audit_log(actor_telegram_id: int, actor_label: str, action: str, details: str = "") -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO audit_log (actor_telegram_id, actor_label, action, details) VALUES (?, ?, ?, ?)",
        (actor_telegram_id, actor_label, action, details),
    )
    conn.commit()
    conn.close()


def get_audit_log(limit: int = 20) -> list[sqlite3.Row]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows

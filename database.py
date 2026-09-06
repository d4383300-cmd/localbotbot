import os
import sqlite3
from datetime import datetime, timedelta

DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH = "bot_data.db"

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance INTEGER DEFAULT 0,
                warns INTEGER DEFAULT 0,
                links_today INTEGER DEFAULT 0,
                last_link_date TEXT,
                is_blocked INTEGER DEFAULT 0,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_members (
                user_id INTEGER PRIMARY KEY,
                username TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                real_name TEXT,
                age INTEGER,
                status TEXT DEFAULT 'pending'
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS marriages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user1_id INTEGER UNIQUE,
                user1_name TEXT,
                user2_id INTEGER UNIQUE,
                user2_name TEXT,
                married_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_activity (
                user_id INTEGER,
                date TEXT,
                msg_count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reward_history (
                date TEXT PRIMARY KEY,
                awarded INTEGER DEFAULT 0
            )
        """)
        conn.commit()

def get_user(user_id: int, username: str = "", first_name: str = ""):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        if not user:
            cursor.execute(
                "INSERT INTO users (user_id, username, first_name, balance, joined_at) VALUES (?, ?, ?, 0, ?)",
                (user_id, username, first_name, now_str)
            )
            conn.commit()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            user = cursor.fetchone()
        else:
            if username or first_name:
                cursor.execute(
                    "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                    (username or user["username"], first_name or user["first_name"], user_id)
                )
                conn.commit()
        return dict(user)

def update_balance(user_id: int, amount: int) -> int:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()
        cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        return cursor.fetchone()[0]

def check_and_increment_links(user_id: int) -> int:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    user = get_user(user_id)
    links_today = user["links_today"]

    if user["last_link_date"] != today:
        links_today = 0

    links_today += 1
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET links_today = ?, last_link_date = ? WHERE user_id = ?",
            (links_today, today, user_id)
        )
        conn.commit()
    return links_today

def set_blocked(user_id: int, status: int = 1):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_blocked = ? WHERE user_id = ?", (status, user_id))
        conn.commit()

def is_blocked(user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
        return bool(res and res[0] == 1)

def save_member(user_id: int, username: str):
    if username:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO chat_members (user_id, username) VALUES (?, ?)",
                (user_id, username)
            )
            conn.commit()

def get_all_members():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM chat_members WHERE username IS NOT NULL")
        return [row[0] for row in cursor.fetchall()]

def create_application(user_id: int, username: str, real_name: str, age: int) -> int:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO applications (user_id, username, real_name, age) VALUES (?, ?, ?, ?)",
            (user_id, username, real_name, age)
        )
        conn.commit()
        return cursor.lastrowid

def get_application(app_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM applications WHERE id = ?", (app_id,))
        res = cursor.fetchone()
        return dict(res) if res else None

def update_app_status(app_id: int, status: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE applications SET status = ? WHERE id = ?", (status, app_id))
        conn.commit()

# --- БРАКИ И РАЗВОД ---

def get_marriage(user_id: int):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM marriages WHERE user1_id = ? OR user2_id = ?",
            (user_id, user_id)
        )
        res = cursor.fetchone()
        return dict(res) if res else None

def create_marriage(user1_id: int, user1_name: str, user2_id: int, user2_name: str):
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO marriages (user1_id, user1_name, user2_id, user2_name, married_at) VALUES (?, ?, ?, ?, ?)",
            (user1_id, user1_name, user2_id, user2_name, now_str)
        )
        conn.commit()

def delete_marriage(user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM marriages WHERE user1_id = ? OR user2_id = ?", (user_id, user_id))
        conn.commit()
        return cursor.rowcount > 0

def get_all_marriages():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM marriages ORDER BY married_at ASC")
        return [dict(row) for row in cursor.fetchall()]

# --- СТАТИСТИКА СООБЩЕНИЙ ---

def record_message(user_id: int, msk_date_str: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO daily_activity (user_id, date, msg_count)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, date) DO UPDATE SET msg_count = msg_count + 1
        """, (user_id, msk_date_str))
        conn.commit()

def get_user_stats(user_id: int, today_msk: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        # За сегодня
        cursor.execute("SELECT msg_count FROM daily_activity WHERE user_id = ? AND date = ?", (user_id, today_msk))
        day_row = cursor.fetchone()
        day_msgs = day_row[0] if day_row else 0

        # За неделю (последние 7 дней)
        cursor.execute("""
            SELECT SUM(msg_count) FROM daily_activity 
            WHERE user_id = ? AND date >= date(?, '-6 day')
        """, (user_id, today_msk))
        week_row = cursor.fetchone()
        week_msgs = week_row[0] if week_row and week_row[0] else 0

        # За все время
        cursor.execute("SELECT SUM(msg_count) FROM daily_activity WHERE user_id = ?", (user_id,))
        all_row = cursor.fetchone()
        all_msgs = all_row[0] if all_row and all_row[0] else 0

        return {
            "day": day_msgs,
            "week": week_msgs,
            "all": all_msgs
        }

def get_top_daily(date_str: str, limit: int = 5):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT d.user_id, d.msg_count, u.first_name, u.username
            FROM daily_activity d
            JOIN users u ON d.user_id = u.user_id
            WHERE d.date = ?
            ORDER BY d.msg_count DESC
            LIMIT ?
        """, (date_str, limit))
        return [dict(row) for row in cursor.fetchall()]

def is_reward_given(date_str: str) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT awarded FROM reward_history WHERE date = ?", (date_str,))
        res = cursor.fetchone()
        return bool(res and res[0] == 1)

def mark_reward_given(date_str: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO reward_history (date, awarded) VALUES (?, 1)", (date_str,))
        conn.commit()

init_db()

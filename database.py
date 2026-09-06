import sqlite3
from datetime import datetime

DB_PATH = "bot_data.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
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
                is_blocked INTEGER DEFAULT 0
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
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            cursor.execute(
                "INSERT INTO users (user_id, username, first_name, balance) VALUES (?, ?, ?, 0)",
                (user_id, username, first_name)
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
    with sqlite3.connect(DB_PATH) as conn:
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
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET links_today = ?, last_link_date = ? WHERE user_id = ?",
            (links_today, today, user_id)
        )
        conn.commit()
    return links_today

def set_blocked(user_id: int, status: int = 1):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_blocked = ? WHERE user_id = ?", (status, user_id))
        conn.commit()

def is_blocked(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
        return bool(res and res[0] == 1)

def save_member(user_id: int, username: str):
    if username:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO chat_members (user_id, username) VALUES (?, ?)",
                (user_id, username)
            )
            conn.commit()

def get_all_members():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM chat_members WHERE username IS NOT NULL")
        return [row[0] for row in cursor.fetchall()]

def create_application(user_id: int, username: str, real_name: str, age: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO applications (user_id, username, real_name, age) VALUES (?, ?, ?, ?)",
            (user_id, username, real_name, age)
        )
        conn.commit()
        return cursor.lastrowid

def get_application(app_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM applications WHERE id = ?", (app_id,))
        res = cursor.fetchone()
        return dict(res) if res else None

def update_app_status(app_id: int, status: str):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE applications SET status = ? WHERE id = ?", (status, app_id))
        conn.commit()

# --- ФУНКЦИИ БРАКОВ ---

def get_marriage(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM marriages WHERE user1_id = ? OR user2_id = ?",
            (user_id, user_id)
        )
        res = cursor.fetchone()
        return dict(res) if res else None

def create_marriage(user1_id: int, user1_name: str, user2_id: int, user2_name: str):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO marriages (user1_id, user1_name, user2_id, user2_name, married_at) VALUES (?, ?, ?, ?, ?)",
            (user1_id, user1_name, user2_id, user2_name, datetime.utcnow())
        )
        conn.commit()

def get_all_marriages():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM marriages ORDER BY married_at ASC")
        return [dict(row) for row in cursor.fetchall()]

# --- АКТИВНОСТЬ И СТАТИСТИКА СООБЩЕНИЙ ---

def record_message(user_id: int):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO daily_activity (user_id, date, msg_count)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, date) DO UPDATE SET msg_count = msg_count + 1
        """, (user_id, today))
        conn.commit()

def get_top_daily(limit: int = 5):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT d.user_id, d.msg_count, u.first_name, u.username
            FROM daily_activity d
            JOIN users u ON d.user_id = u.user_id
            WHERE d.date = ?
            ORDER BY d.msg_count DESC
            LIMIT ?
        """, (today, limit))
        return [dict(row) for row in cursor.fetchall()]

def get_yesterday_top():
    yesterday = (datetime.utcnow() - datetime.resolution).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT d.user_id, d.msg_count, u.first_name
            FROM daily_activity d
            JOIN users u ON d.user_id = u.user_id
            WHERE d.date = ?
            ORDER BY d.msg_count DESC
            LIMIT 1
        """, (yesterday,))
        res = cursor.fetchone()
        return dict(res) if res else None

def is_reward_given(date_str: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT awarded FROM reward_history WHERE date = ?", (date_str,))
        res = cursor.fetchone()
        return bool(res and res[0] == 1)

def mark_reward_given(date_str: str):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO reward_history (date, awarded) VALUES (?, 1)", (date_str,))
        conn.commit()

init_db()

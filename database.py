import os
import sqlite3
import psycopg2
import psycopg2.extras
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH = "bot_data.db"

def get_db():
    if DATABASE_URL:
        # Режим постоянной базы данных Render PostgreSQL
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        return conn, "pg"
    else:
        # Локальный SQLite с режимом WAL против сброса данных
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn, "sqlite"

def init_db():
    conn, mode = get_db()
    cursor = conn.cursor()
    if mode == "pg":
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                balance BIGINT DEFAULT 0,
                warns INT DEFAULT 0,
                links_today INT DEFAULT 0,
                last_link_date TEXT,
                is_blocked INT DEFAULT 0,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS chat_members (
                user_id BIGINT PRIMARY KEY,
                username TEXT
            );
            CREATE TABLE IF NOT EXISTS applications (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                real_name TEXT,
                age INT,
                status TEXT DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS marriages (
                id SERIAL PRIMARY KEY,
                user1_id BIGINT UNIQUE,
                user1_name TEXT,
                user2_id BIGINT UNIQUE,
                user2_name TEXT,
                married_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS daily_activity (
                user_id BIGINT,
                date TEXT,
                msg_count INT DEFAULT 0,
                PRIMARY KEY (user_id, date)
            );
            CREATE TABLE IF NOT EXISTS reward_history (
                date TEXT PRIMARY KEY,
                awarded INT DEFAULT 0
            );
        """)
    else:
        cursor.executescript("""
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
            );
            CREATE TABLE IF NOT EXISTS chat_members (
                user_id INTEGER PRIMARY KEY,
                username TEXT
            );
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                real_name TEXT,
                age INTEGER,
                status TEXT DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS marriages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user1_id INTEGER UNIQUE,
                user1_name TEXT,
                user2_id INTEGER UNIQUE,
                user2_name TEXT,
                married_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS daily_activity (
                user_id INTEGER,
                date TEXT,
                msg_count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            );
            CREATE TABLE IF NOT EXISTS reward_history (
                date TEXT PRIMARY KEY,
                awarded INTEGER DEFAULT 0
            );
        """)
        conn.commit()
    conn.close()

def get_user(user_id: int, username: str = "", first_name: str = ""):
    conn, mode = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor) if mode == "pg" else conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = %s" if mode == "pg" else "SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    if not user:
        q = "INSERT INTO users (user_id, username, first_name, balance, joined_at) VALUES (%s, %s, %s, 0, %s)" if mode == "pg" else \
            "INSERT INTO users (user_id, username, first_name, balance, joined_at) VALUES (?, ?, ?, 0, ?)"
        cursor.execute(q, (user_id, username, first_name, now_str))
        if mode == "sqlite": conn.commit()
        cursor.execute("SELECT * FROM users WHERE user_id = %s" if mode == "pg" else "SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = cursor.fetchone()
    else:
        if username or first_name:
            q = "UPDATE users SET username = %s, first_name = %s WHERE user_id = %s" if mode == "pg" else \
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?"
            cursor.execute(q, (username or user["username"], first_name or user["first_name"], user_id))
            if mode == "sqlite": conn.commit()

    res = dict(user)
    conn.close()
    return res

def update_balance(user_id: int, amount: int) -> int:
    conn, mode = get_db()
    cursor = conn.cursor()
    q = "UPDATE users SET balance = balance + %s WHERE user_id = %s" if mode == "pg" else \
        "UPDATE users SET balance = balance + ? WHERE user_id = ?"
    cursor.execute(q, (amount, user_id))
    q_sel = "SELECT balance FROM users WHERE user_id = %s" if mode == "pg" else "SELECT balance FROM users WHERE user_id = ?"
    cursor.execute(q_sel, (user_id,))
    new_bal = cursor.fetchone()[0]
    if mode == "sqlite": conn.commit()
    conn.close()
    return new_bal

def check_and_increment_links(user_id: int) -> int:
    today = datetime.utcnow().strftime("%Y-%m-%d")
    user = get_user(user_id)
    links_today = user["links_today"]

    if user["last_link_date"] != today:
        links_today = 0
    links_today += 1

    conn, mode = get_db()
    cursor = conn.cursor()
    q = "UPDATE users SET links_today = %s, last_link_date = %s WHERE user_id = %s" if mode == "pg" else \
        "UPDATE users SET links_today = ?, last_link_date = ? WHERE user_id = ?"
    cursor.execute(q, (links_today, today, user_id))
    if mode == "sqlite": conn.commit()
    conn.close()
    return links_today

def set_blocked(user_id: int, status: int = 1):
    conn, mode = get_db()
    cursor = conn.cursor()
    q = "UPDATE users SET is_blocked = %s WHERE user_id = %s" if mode == "pg" else "UPDATE users SET is_blocked = ? WHERE user_id = ?"
    cursor.execute(q, (status, user_id))
    if mode == "sqlite": conn.commit()
    conn.close()

def is_blocked(user_id: int) -> bool:
    conn, mode = get_db()
    cursor = conn.cursor()
    q = "SELECT is_blocked FROM users WHERE user_id = %s" if mode == "pg" else "SELECT is_blocked FROM users WHERE user_id = ?"
    cursor.execute(q, (user_id,))
    res = cursor.fetchone()
    conn.close()
    return bool(res and res[0] == 1)

def save_member(user_id: int, username: str):
    if not username: return
    conn, mode = get_db()
    cursor = conn.cursor()
    if mode == "pg":
        cursor.execute("INSERT INTO chat_members (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username", (user_id, username))
    else:
        cursor.execute("INSERT OR REPLACE INTO chat_members (user_id, username) VALUES (?, ?)", (user_id, username))
        conn.commit()
    conn.close()

def get_all_members():
    conn, mode = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM chat_members WHERE username IS NOT NULL")
    res = [row[0] for row in cursor.fetchall()]
    conn.close()
    return res

def create_application(user_id: int, username: str, real_name: str, age: int) -> int:
    conn, mode = get_db()
    cursor = conn.cursor()
    if mode == "pg":
        cursor.execute("INSERT INTO applications (user_id, username, real_name, age) VALUES (%s, %s, %s, %s) RETURNING id", (user_id, username, real_name, age))
        app_id = cursor.fetchone()[0]
    else:
        cursor.execute("INSERT INTO applications (user_id, username, real_name, age) VALUES (?, ?, ?, ?)", (user_id, username, real_name, age))
        conn.commit()
        app_id = cursor.lastrowid
    conn.close()
    return app_id

def get_application(app_id: int):
    conn, mode = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor) if mode == "pg" else conn.cursor()
    q = "SELECT * FROM applications WHERE id = %s" if mode == "pg" else "SELECT * FROM applications WHERE id = ?"
    cursor.execute(q, (app_id,))
    res = cursor.fetchone()
    conn.close()
    return dict(res) if res else None

def update_app_status(app_id: int, status: str):
    conn, mode = get_db()
    cursor = conn.cursor()
    q = "UPDATE applications SET status = %s WHERE id = %s" if mode == "pg" else "UPDATE applications SET status = ? WHERE id = ?"
    cursor.execute(q, (status, app_id))
    if mode == "sqlite": conn.commit()
    conn.close()

def get_marriage(user_id: int):
    conn, mode = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor) if mode == "pg" else conn.cursor()
    q = "SELECT * FROM marriages WHERE user1_id = %s OR user2_id = %s" if mode == "pg" else "SELECT * FROM marriages WHERE user1_id = ? OR user2_id = ?"
    cursor.execute(q, (user_id, user_id))
    res = cursor.fetchone()
    conn.close()
    return dict(res) if res else None

def create_marriage(user1_id: int, user1_name: str, user2_id: int, user2_name: str):
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn, mode = get_db()
    cursor = conn.cursor()
    q = "INSERT INTO marriages (user1_id, user1_name, user2_id, user2_name, married_at) VALUES (%s, %s, %s, %s, %s)" if mode == "pg" else \
        "INSERT INTO marriages (user1_id, user1_name, user2_id, user2_name, married_at) VALUES (?, ?, ?, ?, ?)"
    cursor.execute(q, (user1_id, user1_name, user2_id, user2_name, now_str))
    if mode == "sqlite": conn.commit()
    conn.close()

def delete_marriage(user_id: int) -> bool:
    conn, mode = get_db()
    cursor = conn.cursor()
    q = "DELETE FROM marriages WHERE user1_id = %s OR user2_id = %s" if mode == "pg" else "DELETE FROM marriages WHERE user1_id = ? OR user2_id = ?"
    cursor.execute(q, (user_id, user_id))
    deleted = cursor.rowcount > 0
    if mode == "sqlite": conn.commit()
    conn.close()
    return deleted

def get_all_marriages():
    conn, mode = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor) if mode == "pg" else conn.cursor()
    cursor.execute("SELECT * FROM marriages ORDER BY married_at ASC")
    res = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return res

def record_message(user_id: int, msk_date_str: str):
    conn, mode = get_db()
    cursor = conn.cursor()
    if mode == "pg":
        cursor.execute("""
            INSERT INTO daily_activity (user_id, date, msg_count) VALUES (%s, %s, 1)
            ON CONFLICT(user_id, date) DO UPDATE SET msg_count = daily_activity.msg_count + 1
        """, (user_id, msk_date_str))
    else:
        cursor.execute("""
            INSERT INTO daily_activity (user_id, date, msg_count) VALUES (?, ?, 1)
            ON CONFLICT(user_id, date) DO UPDATE SET msg_count = msg_count + 1
        """, (user_id, msk_date_str))
        conn.commit()
    conn.close()

def get_user_stats(user_id: int, today_msk: str):
    conn, mode = get_db()
    cursor = conn.cursor()
    q_day = "SELECT msg_count FROM daily_activity WHERE user_id = %s AND date = %s" if mode == "pg" else \
            "SELECT msg_count FROM daily_activity WHERE user_id = ? AND date = ?"
    cursor.execute(q_day, (user_id, today_msk))
    d_row = cursor.fetchone()
    day_msgs = d_row[0] if d_row else 0

    if mode == "pg":
        cursor.execute("SELECT SUM(msg_count) FROM daily_activity WHERE user_id = %s AND to_date(date, 'YYYY-MM-DD') >= to_date(%s, 'YYYY-MM-DD') - INTERVAL '6 day'", (user_id, today_msk))
    else:
        cursor.execute("SELECT SUM(msg_count) FROM daily_activity WHERE user_id = ? AND date >= date(?, '-6 day')", (user_id, today_msk))
    w_row = cursor.fetchone()
    week_msgs = w_row[0] if w_row and w_row[0] else 0

    q_all = "SELECT SUM(msg_count) FROM daily_activity WHERE user_id = %s" if mode == "pg" else "SELECT SUM(msg_count) FROM daily_activity WHERE user_id = ?"
    cursor.execute(q_all, (user_id,))
    a_row = cursor.fetchone()
    all_msgs = a_row[0] if a_row and a_row[0] else 0
    conn.close()
    return {"day": day_msgs, "week": week_msgs, "all": all_msgs}

def get_top_daily(date_str: str, limit: int = 5):
    conn, mode = get_db()
    cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor) if mode == "pg" else conn.cursor()
    q = """
        SELECT d.user_id, d.msg_count, u.first_name, u.username
        FROM daily_activity d
        JOIN users u ON d.user_id = u.user_id
        WHERE d.date = %s
        ORDER BY d.msg_count DESC LIMIT %s
    """ if mode == "pg" else """
        SELECT d.user_id, d.msg_count, u.first_name, u.username
        FROM daily_activity d
        JOIN users u ON d.user_id = u.user_id
        WHERE d.date = ?
        ORDER BY d.msg_count DESC LIMIT ?
    """
    cursor.execute(q, (date_str, limit))
    res = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return res

def is_reward_given(date_str: str) -> bool:
    conn, mode = get_db()
    cursor = conn.cursor()
    q = "SELECT awarded FROM reward_history WHERE date = %s" if mode == "pg" else "SELECT awarded FROM reward_history WHERE date = ?"
    cursor.execute(q, (date_str,))
    res = cursor.fetchone()
    conn.close()
    return bool(res and res[0] == 1)

def mark_reward_given(date_str: str):
    conn, mode = get_db()
    cursor = conn.cursor()
    if mode == "pg":
        cursor.execute("INSERT INTO reward_history (date, awarded) VALUES (%s, 1) ON CONFLICT (date) DO UPDATE SET awarded = 1", (date_str,))
    else:
        cursor.execute("INSERT OR REPLACE INTO reward_history (date, awarded) VALUES (?, 1)", (date_str,))
        conn.commit()
    conn.close()

init_db()

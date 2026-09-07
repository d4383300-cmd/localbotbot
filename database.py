def init_db():
    conn, mode = get_db()
    cursor = conn.cursor()
    if mode == "pg":
        # 1. Создаем таблицы, если их нет
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
        # 2. Автоматическая миграция существующих таблиц (добавляет отсутствующие колонки)
        cursor.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS balance BIGINT DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS warns INT DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS links_today INT DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_link_date TEXT;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS is_blocked INT DEFAULT 0;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
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

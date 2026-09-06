const Database = require('better-sqlite3');
const path = require('path');

const db = new Database(path.join(__dirname, 'bot_data.db'));

// Создание необходимых таблиц
db.exec(`
  CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    balance INTEGER DEFAULT 0,
    warns INTEGER DEFAULT 0,
    links_today INTEGER DEFAULT 0,
    last_link_date TEXT,
    is_blocked INTEGER DEFAULT 0
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
`);

module.exports = {
  getUser(userId, username = '', firstName = '') {
    let user = db.prepare('SELECT * FROM users WHERE user_id = ?').get(userId);
    if (!user) {
      db.prepare(
        'INSERT INTO users (user_id, username, first_name, balance) VALUES (?, ?, ?, 0)'
      ).run(userId, username, firstName);
      user = db.prepare('SELECT * FROM users WHERE user_id = ?').get(userId);
    }
    return user;
  },

  updateBalance(userId, amount) {
    db.prepare('UPDATE users SET balance = balance + ? WHERE user_id = ?').run(amount, userId);
    return db.prepare('SELECT balance FROM users WHERE user_id = ?').get(userId).balance;
  },

  checkAndIncrementLinks(userId) {
    const today = new Date().toISOString().slice(0, 10);
    const user = this.getUser(userId);

    let linksToday = user.links_today;
    if (user.last_link_date !== today) {
      linksToday = 0;
    }
    linksToday += 1;

    db.prepare('UPDATE users SET links_today = ?, last_link_date = ? WHERE user_id = ?')
      .run(linksToday, today, userId);

    return linksToday;
  },

  addWarn(userId) {
    db.prepare('UPDATE users SET warns = warns + 1 WHERE user_id = ?').run(userId);
    return db.prepare('SELECT warns FROM users WHERE user_id = ?').get(userId).warns;
  },

  setBlocked(userId, status = 1) {
    db.prepare('UPDATE users SET is_blocked = ? WHERE user_id = ?').run(status, userId);
  },

  isBlocked(userId) {
    const user = db.prepare('SELECT is_blocked FROM users WHERE user_id = ?').get(userId);
    return user && user.is_blocked === 1;
  },

  saveMember(userId, username) {
    if (username) {
      db.prepare('INSERT OR REPLACE INTO chat_members (user_id, username) VALUES (?, ?)')
        .run(userId, username);
    }
  },

  getAllMembers() {
    return db.prepare('SELECT username FROM chat_members WHERE username IS NOT NULL').all();
  },

  createApplication(userId, username, realName, age) {
    const info = db.prepare(
      'INSERT INTO applications (user_id, username, real_name, age) VALUES (?, ?, ?, ?)'
    ).run(userId, username, realName, age);
    return info.lastInsertRowid;
  },

  getApplication(appId) {
    return db.prepare('SELECT * FROM applications WHERE id = ?').get(appId);
  },

  updateAppStatus(appId, status) {
    db.prepare('UPDATE applications SET status = ? WHERE id = ?').run(status, appId);
  }
};
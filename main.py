import os
import re
import time
import random
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import aiosqlite
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatPermissions,
    Message,
    CallbackQuery,
    ChatMemberUpdated
)

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8642763436:AAFCMLXHsFjwnXfiZ_N3yWzZIKJ--oszfhk")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "-1004373765011"))
MODERATOR_ID = int(os.getenv("MODERATOR_ID", "7505593850"))
CHAT_INVITE_LINK = "https://t.me/+Un85Q4TUznc4YWYy"

WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 8080))

# Подключение к БД (PostgreSQL на Render / Neon или локальный SQLite)
DATABASE_URL = os.getenv("DATABASE_URL")
SQLITE_PATH = "bot_database.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Хранилище временных данных в RAM
user_messages_timestamps: Dict[int, List[float]] = {}
user_stickers_history: Dict[int, List[int]] = {}

# Активные задачи по работе: { task_message_id: { "user_id": int, "answer": int, "expire_at": float } }
active_math_tasks: Dict[int, dict] = {}

# ================= УНИВЕРСАЛЬНАЯ БАЗА ДАННЫХ =================
pg_pool: Optional[asyncpg.Pool] = None

async def init_db():
    global pg_pool
    if DATABASE_URL:
        # Корректировка URL для asyncpg при необходимости
        db_url = DATABASE_URL.replace("postgres://", "postgresql://")
        pg_pool = await asyncpg.create_pool(dsn=db_url)
        async with pg_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    balance BIGINT DEFAULT 0,
                    warns INT DEFAULT 0,
                    links_today INT DEFAULT 0,
                    last_link_date TEXT,
                    is_banned INT DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS known_chat_members (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT
                );
            """)
    else:
        async with aiosqlite.connect(SQLITE_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER DEFAULT 0,
                    warns INTEGER DEFAULT 0,
                    links_today INTEGER DEFAULT 0,
                    last_link_date TEXT,
                    is_banned INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS known_chat_members (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT
                )
            """)
            await db.commit()

async def get_user(user_id: int) -> dict:
    today_str = datetime.now().strftime("%Y-%m-%d")
    if pg_pool:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT balance, warns, links_today, last_link_date, is_banned FROM users WHERE user_id = $1", user_id)
            if not row:
                await conn.execute(
                    "INSERT INTO users (user_id, balance, warns, links_today, last_link_date, is_banned) VALUES ($1, 0, 0, 0, $2, 0)",
                    user_id, today_str
                )
                return {"balance": 0, "warns": 0, "links_today": 0, "last_link_date": today_str, "is_banned": 0}
            return dict(row)
    else:
        async with aiosqlite.connect(SQLITE_PATH) as db:
            async with db.execute("SELECT balance, warns, links_today, last_link_date, is_banned FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await db.execute(
                        "INSERT INTO users (user_id, balance, warns, links_today, last_link_date, is_banned) VALUES (?, 0, 0, 0, ?, 0)",
                        (user_id, today_str)
                    )
                    await db.commit()
                    return {"balance": 0, "warns": 0, "links_today": 0, "last_link_date": today_str, "is_banned": 0}
                return {
                    "balance": row[0],
                    "warns": row[1],
                    "links_today": row[2],
                    "last_link_date": row[3],
                    "is_banned": row[4]
                }

async def update_balance(user_id: int, delta: int) -> int:
    today_str = datetime.now().strftime("%Y-%m-%d")
    if pg_pool:
        async with pg_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, balance, last_link_date) 
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) 
                DO UPDATE SET balance = users.balance + $2
            """, user_id, delta, today_str)
            row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
            return row["balance"]
    else:
        async with aiosqlite.connect(SQLITE_PATH) as db:
            await db.execute("""
                INSERT INTO users (user_id, balance, last_link_date) 
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) 
                DO UPDATE SET balance = users.balance + ?
            """, (user_id, delta, today_str, delta))
            await db.commit()
            async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cur:
                r = await cur.fetchone()
                return r[0]

async def save_chat_member(user: types.User):
    if pg_pool:
        async with pg_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO known_chat_members (user_id, username, full_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO UPDATE SET username = $2, full_name = $3
            """, user.id, user.username, user.full_name)
    else:
        async with aiosqlite.connect(SQLITE_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO known_chat_members (user_id, username, full_name)
                VALUES (?, ?, ?)
            """, (user.id, user.username, user.full_name))
            await db.commit()

async def get_all_members():
    if pg_pool:
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, username, full_name FROM known_chat_members WHERE user_id != $1", bot.id)
            return [tuple(r.values()) for r in rows]
    else:
        async with aiosqlite.connect(SQLITE_PATH) as db:
            async with db.execute("SELECT user_id, username, full_name FROM known_chat_members WHERE user_id != ?", (bot.id,)) as cur:
                return await cur.fetchall()

async def is_admin(chat_id: int, user_id: int) -> bool:
    if user_id == MODERATOR_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except Exception:
        return False

# ================= FSM СОСТОЯНИЯ =================
class RegistrationForm(StatesGroup):
    name = State()
    age = State()
    rules_agree = State()
    confirm = State()

class AdminAction(StatesGroup):
    waiting_for_reject_reason = State()

# ================= СИСТЕМНЫЕ СОБЫТИЯ ЧАТА =================
@dp.my_chat_member()
async def on_bot_promoted(event: ChatMemberUpdated):
    if event.chat.id == TARGET_CHAT_ID and event.new_chat_member.status == ChatMemberStatus.ADMINISTRATOR:
        await bot.send_message(
            chat_id=event.chat.id,
            text="🌿 **Система Localhaus активирована!**\nПрава администратора подтверждены. Готов к работе!",
            parse_mode=ParseMode.MARKDOWN
        )

@dp.message(F.chat.id == TARGET_CHAT_ID, F.new_chat_members)
async def on_user_join(message: Message):
    for new_user in message.new_chat_members:
        await save_chat_member(new_user)
        welcome_text = (
            f"✨ Добро пожаловать в **Localhaus**, {new_user.mention_markdown()}!\n\n"
            "📜 Обязательно прочитай правила — напиши `правила`.\n"
            "💬 Найди себе друга для общения, работай через `работа` и копи эко-валюту!"
        )
        await message.answer(welcome_text, parse_mode=ParseMode.MARKDOWN)

# ================= FSM: ЗАЯВКА В ЛС =================
@dp.message(F.chat.type == "private", CommandStart())
async def cmd_start_private(message: Message, state: FSMContext):
    u = await get_user(message.from_user.id)
    if u["is_banned"]:
        return

    await state.clear()
    await message.answer(
        "🌿 **Добро пожаловать в шлюз сообщества Localhaus!**\n\n"
        "Пройдите короткую анкету для входа в чат.\n\n"
        "👤 **Шаг 1:** Как вас зовут?",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(RegistrationForm.name)

@dp.message(F.chat.type == "private", RegistrationForm.name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("🎂 **Шаг 2:** Сколько вам полных лет? (Укажите реальный возраст цифрами)", parse_mode=ParseMode.MARKDOWN)
    await state.set_state(RegistrationForm.age)

@dp.message(F.chat.type == "private", RegistrationForm.age)
async def process_age(message: Message, state: FSMContext):
    if not message.text.isdigit() or not (10 <= int(message.text) <= 99):
        await message.answer("⚠️ Введите корректный возраст числом (от 10 до 99):")
        return
    await state.update_data(age=int(message.text))
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ознакомлен(а) и согласен(а)", callback_data="rules_ok")]
    ])
    await message.answer(
        "📜 **Шаг 3: Правила Localhaus**\n\n"
        "• Запрещен спам, лесенка (>5 сообщений за 3 сек).\n"
        "• Запрещен спам стикерами.\n"
        "• Контент 18+ и реклама строго запрещены.\n\n"
        "Подтвердите ознакомление кнопкой ниже.",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(RegistrationForm.rules_agree)

@dp.callback_query(RegistrationForm.rules_agree, F.data == "rules_ok")
async def process_rules_confirm(call: CallbackQuery, state: FSMContext):
    await call.message.delete_reply_markup()
    data = await state.get_data()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, отправить", callback_data="send_app"),
            InlineKeyboardButton(text="🔄 Заполнить заново", callback_data="restart_app")
        ]
    ])
    await call.message.answer(
        f"📋 **Ваша анкета:**\n• Имя: {data['name']}\n• Возраст: {data['age']}\n\nВсе верно?",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(RegistrationForm.confirm)
    await call.answer()

@dp.callback_query(RegistrationForm.confirm, F.data == "restart_app")
async def restart_application(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("🔄 Заполняем сначала. Как вас зовут?")
    await state.set_state(RegistrationForm.name)
    await call.answer()

@dp.callback_query(RegistrationForm.confirm, F.data == "send_app")
async def send_application(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    applicant = call.from_user
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"adm_accept:{applicant.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_reject:{applicant.id}")
        ],
        [
            InlineKeyboardButton(text="⛔ Заблокировать", callback_data=f"adm_block:{applicant.id}")
        ]
    ])
    
    group_msg = (
        f"🔔 **НОВАЯ ЗАЯВКА ОТ {applicant.mention_markdown()}** (`{applicant.id}`)\n\n"
        f"🏷 **Имя:** {data['name']}\n"
        f"🎂 **Возраст:** {data['age']}\n\n"
        f"@leymik админы примите решения!"
    )
    
    try:
        await bot.send_message(chat_id=TARGET_CHAT_ID, text=group_msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        await call.message.answer("🚀 Ваша заявка передана модераторам чата!")
    except Exception:
        await call.message.answer("⚠️ Не удалось отправить заявку в группу.")
    
    await state.clear()
    await call.answer()

# ================= МОДЕРАЦИЯ ЗАЯВОК =================
@dp.callback_query(F.data.startswith("adm_"))
async def handle_admin_decision(call: CallbackQuery, state: FSMContext):
    if not (call.from_user.id == MODERATOR_ID or await is_admin(TARGET_CHAT_ID, call.from_user.id)):
        await call.answer("⛔ Только администраторы или @leymik могут принимать решение!", show_alert=True)
        return

    action, target_user_id = call.data.split(":")
    target_user_id = int(target_user_id)

    if action == "adm_accept":
        await bot.send_message(
            chat_id=target_user_id,
            text=f"🎉 **Ваша заявка одобрена!**\nВступайте в Localhaus: {CHAT_INVITE_LINK}"
        )
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.reply(f"✅ Пользователь `[{target_user_id}]` принят модератором {call.from_user.mention_markdown()}.")
        await call.answer("Принято!")

    elif action == "adm_block":
        if pg_pool:
            async with pg_pool.acquire() as conn:
                await conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = $1", target_user_id)
        else:
            async with aiosqlite.connect(SQLITE_PATH) as db:
                await db.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (target_user_id,))
                await db.commit()
                
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.reply(f"⛔ Пользователь `[{target_user_id}]` заблокирован навсегда.")
        await call.answer("Заблокирован.")

    elif action == "adm_reject":
        await state.set_state(AdminAction.waiting_for_reject_reason)
        await state.update_data(reject_target_id=target_user_id)
        await bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=f"✍️ @leymik / {call.from_user.mention_markdown()}, укажите причину отклонения ответом на это сообщение:"
        )
        await call.answer()

@dp.message(F.chat.id == TARGET_CHAT_ID, AdminAction.waiting_for_reject_reason)
async def process_reject_reason(message: Message, state: FSMContext):
    if not (message.from_user.id == MODERATOR_ID or await is_admin(TARGET_CHAT_ID, message.from_user.id)):
        return

    data = await state.get_data()
    target_user_id = data.get("reject_target_id")
    reason = message.text

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=f"❌ **Ваша заявка отклонена.**\nПричина: {reason}",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        pass

    await message.reply("🚫 Отклонение с причиной отправлено пользователю.")
    await state.clear()

# ================= ОБРАБОТКА ПЛАТНЫХ РП ДЕЙСТВИЙ (CALLBACK) =================
@dp.callback_query(F.data.startswith("rp_pay:"))
async def process_paid_rp(call: CallbackQuery):
    _, act_key, initiator_id_str = call.data.split(":")
    initiator_id = int(initiator_id_str)

    if call.from_user.id != initiator_id:
        await call.answer("❌ Это не ваша кнопка оплаты!", show_alert=True)
        return

    user_info = await get_user(initiator_id)
    if user_info["balance"] < 30:
        await call.answer("❌ Недостаточно листочек! Требуется 30 🍃", show_alert=True)
        return

    new_bal = await update_balance(initiator_id, -30)
    await call.message.delete()

    rp_texts = {
        "smoke": "🚬 расслабленно закурил ароматную сигарету, выпуская густой клуб дыма...",
        "snus": "🧊 смачно закинул освежающий снюс под губу. В глазах засияло блаженство...",
        "drink": "🥃 налил себе премиальный напиток и осушил бокал до дна за здоровье чата!"
    }
    
    act_text = rp_texts.get(act_key, "совершил действие")
    res_msg = (
        f"✨ **Элитный клуб Localhaus** ✨\n\n"
        f"🎭 {call.from_user.mention_markdown()} {act_text}\n\n"
        f"💸 Списано: `30` 🍃 | Остаток: `{new_bal}` 🍃"
    )
    await bot.send_message(chat_id=TARGET_CHAT_ID, text=res_msg, parse_mode=ParseMode.MARKDOWN)
    await call.answer()

# ================= ОСНОВНОЙ ПАТТЕРН ЧАТА =================
@dp.message(F.chat.id == TARGET_CHAT_ID)
async def handle_main_chat(message: Message):
    user = message.from_user
    if not user or user.is_bot:
        return

    await save_chat_member(user)
    user_data = await get_user(user.id)
    now = datetime.now()
    user_is_adm = await is_admin(TARGET_CHAT_ID, user.id)

    # 1. ПРОВЕРКА ОНЛАЙНА
    if message.text and message.text.strip().lower() in ["бот ты тут?", "бот ты тут"]:
        await message.reply("Да")
        return

    # 2. ПРОВЕРКА МАТЕМАТИЧЕСКИХ ОТВЕТОВ (РАБОТА)
    if message.reply_to_message and message.reply_to_message.message_id in active_math_tasks:
        task_id = message.reply_to_message.message_id
        task = active_math_tasks[task_id]

        if task["user_id"] == user.id:
            # Проверка таймера (2 минуты)
            if time.time() > task["expire_at"]:
                del active_math_tasks[task_id]
                await message.reply("⏳ **Время вышло!** На решение давалось 2 минуты. Задание аннулировано.")
                return

            text_ans = message.text.strip()
            # Проверяем, что отправлено строго число
            if re.fullmatch(r"^-?\d+$", text_ans):
                given_val = int(text_ans)
                correct_val = task["answer"]
                del active_math_tasks[task_id]

                if given_val == correct_val:
                    new_bal = await update_balance(user.id, 3)
                    await message.reply(
                        f"🎉 **Отлично сработано!**\n"
                        f"Твой ответ: `{given_val}`\n"
                        f"Реальный ответ: `{correct_val}`\n\n"
                        f"💰 Зарплата **+3** 🍃 выдана!\n"
                        f"💼 Баланс: `{new_bal}` 🍃",
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    new_bal = await update_balance(user.id, -40)
                    await message.reply(
                        f"❌ **Неверный ответ!**\n"
                        f"Твой ответ: `{given_val}`\n"
                        f"Правильный ответ: `{correct_val}`\n\n"
                        f"⚠️ Наложен штраф **-40** листочек!\n"
                        f"💼 Баланс: `{new_bal}` 🍃",
                        parse_mode=ParseMode.MARKDOWN
                    )
                return
            else:
                # Написан посторонний текст вместо простого числа
                await message.reply("⚠️ Отвечать нужно **только числом** без букв и символов!")
                return

    # 3. ПРАВИЛА
    if message.text and message.text.strip().lower() == "правила":
        rules_text = (
            "📜 **СВОД ПРАВИЛ ЧАТА LOCALHAUS**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🌿 **1. Спам лесенкой:** Более 5 сообщений за 3 сек — мут.\n"
            "🌿 **2. Стикер-спам:** Серии стикеров удаляются ботом.\n"
            "🌿 **3. Контент 18+:** Строго запрещен (Варн).\n"
            "🌿 **4. Ссылки и реклама:** Запрещены (1 раз — предупреждение, 2 раза за день — бан).\n"
            "🌿 **5. Экономика:**\n"
            "   • `работа` — решать примеры (+3 🍃 за успех, -40 🍃 за ошибку)\n"
            "   • `[число] [ч/к]` — рулетка 50/50\n"
            "   • Отправь эмодзи `🎰` — при 777 выигрыш +200 🍃\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        await message.reply(rules_text, parse_mode=ParseMode.MARKDOWN)
        return

    # 4. АДМИН-КОМАНДЫ (бан, кик, мут, размут, калл)
    if user_is_adm and message.text:
        t_low = message.text.strip().lower()

        # КАЛЛ (тег всех участников)
        if t_low == "калл":
            members = await get_all_members()
            mentions = []
            for m_id, u_name, f_name in members:
                if u_name:
                    mentions.append(f"@{u_name}")
                else:
                    mentions.append(f"[{f_name}](tg://user?id={m_id})")
            
            if mentions:
                for i in range(0, len(mentions), 15):
                    chunk = " ".join(mentions[i:i+15])
                    await message.reply(f"📢 **Общий сбор чата!**\n{chunk}", parse_mode=ParseMode.MARKDOWN)
            else:
                await message.reply("Список участников пуст.")
            return

        # Действия по реплаю
        if message.reply_to_message:
            target = message.reply_to_message.from_user

            if t_low == "бан":
                try:
                    await bot.ban_chat_member(chat_id=TARGET_CHAT_ID, user_id=target.id)
                    await message.reply(f"🚨 Пользователь {target.mention_markdown()} исключен и помещен в ЧС.", parse_mode=ParseMode.MARKDOWN)
                except Exception as e:
                    await message.reply(f"Ошибка бана: {e}")
                return

            if t_low == "кик":
                try:
                    await bot.ban_chat_member(chat_id=TARGET_CHAT_ID, user_id=target.id)
                    await bot.unban_chat_member(chat_id=TARGET_CHAT_ID, user_id=target.id)
                    await message.reply(f"👢 Пользователь {target.mention_markdown()} исключен.", parse_mode=ParseMode.MARKDOWN)
                except Exception as e:
                    await message.reply(f"Ошибка кика: {e}")
                return

            if t_low.startswith("мут"):
                parts = t_low.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    mins = int(parts[1])
                    until_date = now + timedelta(minutes=mins)
                    try:
                        await bot.restrict_chat_member(
                            chat_id=TARGET_CHAT_ID,
                            user_id=target.id,
                            permissions=ChatPermissions(can_send_messages=False),
                            until_date=until_date
                        )
                        await message.reply(f"🔇 {target.mention_markdown()} заглушен на {mins} мин.", parse_mode=ParseMode.MARKDOWN)
                    except Exception as e:
                        await message.reply(f"Ошибка выдачи мута: {e}")
                    return

            if t_low == "размут":
                try:
                    await bot.restrict_chat_member(
                        chat_id=TARGET_CHAT_ID,
                        user_id=target.id,
                        permissions=ChatPermissions(
                            can_send_messages=True,
                            can_send_media_messages=True,
                            can_send_other_messages=True,
                            can_add_web_page_previews=True
                        )
                    )
                    await message.reply(f"🔊 С пользователя {target.mention_markdown()} сняты все ограничения!", parse_mode=ParseMode.MARKDOWN)
                except Exception as e:
                    await message.reply(f"Ошибка размута: {e}")
                return

    # 5. АНТИ-РЕКЛАМА (ССЫЛКИ)
    url_pattern = r"(https?://[^\s]+|t\.me/[^\s]+)"
    has_link = bool(message.text and re.search(url_pattern, message.text)) or bool(message.caption and re.search(url_pattern, message.caption))

    if has_link and not user_is_adm:
        try:
            await message.delete()
        except Exception:
            pass

        today_str = now.strftime("%Y-%m-%d")
        links_cnt = user_data["links_today"] if user_data["last_link_date"] == today_str else 0
        links_cnt += 1

        if pg_pool:
            async with pg_pool.acquire() as conn:
                await conn.execute("UPDATE users SET links_today = $1, last_link_date = $2 WHERE user_id = $3", links_cnt, today_str, user.id)
        else:
            async with aiosqlite.connect(SQLITE_PATH) as db:
                await db.execute("UPDATE users SET links_today = ?, last_link_date = ? WHERE user_id = ?", (links_cnt, today_str, user.id))
                await db.commit()

        if links_cnt >= 2:
            try:
                await bot.ban_chat_member(chat_id=TARGET_CHAT_ID, user_id=user.id)
                await message.answer(f"🚫 {user.mention_markdown()} забанен за повторную рекламу!", parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
        else:
            await message.answer(f"⚠️ {user.mention_markdown()}, ссылки запрещены! (Предупреждение 1/2)", parse_mode=ParseMode.MARKDOWN)
        return

    # 6. АНТИ-ФЛУД (Лесенка >5 сообщений за 3 сек)
    t_stamp = now.timestamp()
    ts_list = [t for t in user_messages_timestamps.get(user.id, []) if t_stamp - t <= 3.0]
    ts_list.append(t_stamp)
    user_messages_timestamps[user.id] = ts_list

    if len(ts_list) > 5 and not user_is_adm:
        try:
            await bot.restrict_chat_member(
                chat_id=TARGET_CHAT_ID,
                user_id=user.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=now + timedelta(minutes=5)
            )
            await message.answer(f"⏱ {user.mention_markdown()} получил мут на 5 минут за спам лесенкой!", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass
        return

    # 7. АНТИ-СПАМ СТИКЕРАМИ
    if message.sticker and not user_is_adm:
        sh = user_stickers_history.get(user.id, [])
        sh.append(message.message_id)
        user_stickers_history[user.id] = sh

        if len(sh) >= 4:
            for mid in sh:
                try:
                    await bot.delete_message(chat_id=TARGET_CHAT_ID, message_id=mid)
                except Exception:
                    pass
            user_stickers_history[user.id] = []
            await message.answer(f"🛡 {user.mention_markdown()} прошу пожалуйста не нарушать правила чата! Чтобы узнать правила чата напишите \"правила\"")
            return
    else:
        if user.id in user_stickers_history:
            user_stickers_history[user.id] = []

    # 8. СЛОТЫ TELEGRAM КАЗИНО (🎰)
    if message.dice and message.dice.emoji == "🎰":
        # Значение 64 соответствует комбинации 777 в Telegram Dice API
        if message.dice.value == 64:
            new_bal = await update_balance(user.id, 200)
            await message.reply(
                f"🔥 **ДЖЕКПОТ 777!** 🔥\n"
                f"🎰 Поздравляем, {user.mention_markdown()}!\n"
                f"🎉 Начислено: **+200** 🍃\n"
                f"💰 Баланс: `{new_bal}` 🍃",
                parse_mode=ParseMode.MARKDOWN
            )
        return

    # ================= ТЕКСТОВЫЕ КОМАНДЫ =================
    if not message.text:
        return

    msg_clean = message.text.strip()
    msg_low = msg_clean.lower()

    # РАБОТА (МАТЕМАТИКА)
    if msg_low == "работа":
        n1 = random.randint(100, 99999)
        n2 = random.randint(100, 99999)
        op = random.choice(["+", "-"])
        ans = n1 + n2 if op == "+" else n1 - n2

        task_text = (
            f"💼 **РАБОЧАЯ СМЕНА LOCALHAUS**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 Сотрудник: {user.mention_markdown()}\n"
            f"💰 Оплата за верное решение: **3** 🍃\n"
            f"⚠️ Штраф за ошибку: **-40** 🍃\n"
            f"⏱ Время: **2 минуты**\n\n"
            f"❓ **Решите пример:**\n"
            f"👉 `{n1} {op} {n2} = ?`\n\n"
            f"📌 Ответьте **реплаем** на это сообщение **только числом**!"
        )
        sent = await message.reply(task_text, parse_mode=ParseMode.MARKDOWN)
        active_math_tasks[sent.message_id] = {
            "user_id": user.id,
            "answer": ans,
            "expire_at": time.time() + 120.0
        }
        return

    # БАЛАНС
    if msg_low in ["б", "баланс"]:
        cur_u = await get_user(user.id)
        await message.reply(
            f"🏦 **Кошелек Localhaus**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 Владелец: {user.mention_markdown()}\n"
            f"🍃 Баланс: **{cur_u['balance']}** листочек\n"
            f"━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # РУЛЕТКА (ДЕП Ч/К)
    roulette_match = re.match(r"^(\d+)\s+([чк])$", msg_low)
    if roulette_match:
        bet = int(roulette_match.group(1))
        choice = roulette_match.group(2)
        cur_u = await get_user(user.id)

        if bet <= 0:
            await message.reply("Ставка должна быть больше 0!")
            return
        if cur_u["balance"] < bet:
            await message.reply(f"❌ Недостаточно листочек! Твой баланс: **{cur_u['balance']}** 🍃")
            return

        outcome = random.choice(["ч", "к"])
        c_title = "⚫ Черный" if outcome == "ч" else "🔴 Красный"

        if outcome == choice:
            new_bal = await update_balance(user.id, bet)
            res = (
                f"🎰 **РУЛЕТКА LOCALHAUS**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Выпало: **{c_title}**\n"
                f"🎉 **ПОБЕДА (2x)!** Получено: **+{bet}** 🍃\n"
                f"💰 Баланс: `{new_bal}` 🍃"
            )
        else:
            new_bal = await update_balance(user.id, -bet)
            res = (
                f"🎰 **РУЛЕТКА LOCALHAUS**\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Выпало: **{c_title}**\n"
                f"💀 **ПРОИГРЫШ (0x)!** Списано: **-{bet}** 🍃\n"
                f"💰 Баланс: `{new_bal}` 🍃"
            )
        await message.reply(res, parse_mode=ParseMode.MARKDOWN)
        return

    # КОМАНДА "ЛЮСТРА"
    if msg_low == "люстра":
        members = await get_all_members()
        if members:
            rnd = random.choice(members)
            rnd_mention = f"@{rnd[1]}" if rnd[1] else f"[{rnd[2]}](tg://user?id={rnd[0]})"
        else:
            rnd_mention = "никого не нашел 💔"
            
        await message.reply(f"Люстра люстра няш няш аф аф люблю сочно сучку {rnd_mention}", parse_mode=ParseMode.MARKDOWN)
        return

    # БЕСПЛАТНЫЕ RP-КОМАНДЫ (по реплаю)
    rp_free_actions = {
        "обнять": "обнял(а) ❤️",
        "поцеловать": "нежно поцеловал(а) в щёчку 💋",
        "выебать": "жестко и безжалостно выебал(а) 🔥"
    }
    if msg_low in rp_free_actions:
        if not message.reply_to_message:
            await message.reply("⚠️ Ответьте этой командой на сообщение того, к кому хотите применить действие!")
            return
        target_u = message.reply_to_message.from_user
        act = rp_free_actions[msg_low]
        await message.answer(
            f"🎭 {user.mention_markdown()} {act} {target_u.mention_markdown()}!",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # ПЛАТНЫЕ RP-ДЕЙСТВИЯ (30 листочек)
    paid_rp_map = {
        "покурить": ("smoke", "🚬 Покурить"),
        "закинуть снюс": ("snus", "🧊 Закинуть снюс"),
        "выпить": ("drink", "🥃 Выпить")
    }
    if msg_low in paid_rp_map:
        key, label = paid_rp_map[msg_low]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💸 Заплатить 30 🍃 ({label})", callback_data=f"rp_pay:{key}:{user.id}")]
        ])
        await message.reply(
            f"🍸 Желаете совершить действие **«{label}»**?\n\n"
            f"💰 Стоимость: **30** листочек.\n"
            f"Нажмите кнопку ниже для подтверждения:",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # 9. РАНДОМНЫЙ ДРОП ЛИСТИКОВ (3% шанс на сообщение, 10–30 листочек)
    if random.random() < 0.03:
        drop_val = random.randint(10, 30)
        await update_balance(user.id, drop_val)
        await message.reply(f"🍃 Неожиданный листопад! {user.mention_markdown()} подбирает **+{drop_val}** листочек!", parse_mode=ParseMode.MARKDOWN)

# ================= RENDER WEBHOOK SERVER =================
async def on_startup(app):
    await init_db()
    if WEBHOOK_HOST:
        await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)

async def on_shutdown(app):
    if pg_pool:
        await pg_pool.close()
    await bot.delete_webhook()
    await bot.session.close()

async def handle_webhook(request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.feed_update(bot=bot, update=update)
        return web.Response(text="OK")
    except Exception as e:
        return web.Response(text=f"Error: {e}", status=400)

async def handle_healthcheck(request):
    return web.Response(text="Localhaus Engine Running")

def main():
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.router.add_get("/", handle_healthcheck)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()

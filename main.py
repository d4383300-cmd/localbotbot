import os
import re
import time
import random
import asyncio
import aiohttp
from aiohttp import web
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatPermissions
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import database as db

BOT_TOKEN = os.getenv("BOT_TOKEN", "8642763436:AAFCMLXHsFjwnXfiZ_N3yWzZIKJ--oszfhk")
TARGET_CHAT_ID = -1004373765011
LEYMIK_ID = 7505593850
INVITE_LINK = "https://t.me/+Un85Q4TUznc4YWYy"
PORT = int(os.getenv("PORT", 3000))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Локальные структуры памяти
cached_admins = set()
last_admin_fetch = 0
user_messages = {}
user_stickers = {}
pending_rejections = {}

class Registration(StatesGroup):
    name = State()
    age = State()
    confirm = State()

# --- Кэш Администраторов ---
async def refresh_admin_cache():
    global cached_admins, last_admin_fetch
    try:
        admins = await bot.get_chat_administrators(TARGET_CHAT_ID)
        cached_admins = {admin.user.id for admin in admins}
        last_admin_fetch = time.time()
    except Exception as e:
        print(f"Ошибка получения админов: {e}")

def is_admin(user_id: int) -> bool:
    if user_id == LEYMIK_ID:
        return True
    return user_id in cached_admins

# 1. Выдача прав администратора боту
@dp.my_chat_member()
async def bot_rights_updated(event: types.ChatMemberUpdated):
    if event.chat.id == TARGET_CHAT_ID and event.new_chat_member.status == ChatMemberStatus.ADMINISTRATOR:
        await refresh_admin_cache()
        await bot.send_message(
            TARGET_CHAT_ID,
            "✨ <b>Права выданы. Готов к работе!</b>",
            parse_mode=ParseMode.HTML
        )

# 2. Приветствие новичков
@dp.message(F.chat.id == TARGET_CHAT_ID, F.new_chat_members)
async def welcome_members(message: types.Message):
    for member in message.new_chat_members:
        if member.id == bot.id:
            continue
        await message.reply(
            f"🌿 <b>Добро пожаловать в Localhaus, <a href='tg://user?id={member.id}'>{member.first_name}</a>!</b>\n"
            f"Обязательно прочитай правила — напиши <b>\"правила\"</b>, а также найди себе друга для общения! ✨",
            parse_mode=ParseMode.HTML
        )

# 3. Анкеты в ЛС
@dp.message(F.chat.type == "private", CommandStart())
async def start_cmd(message: types.Message, state: FSMContext):
    if db.is_blocked(message.from_user.id):
        return

    await state.set_state(Registration.name)
    await message.answer(
        "👋 <b>Приветствуем в приемной Localhaus!</b>\n\n"
        "Чтобы получить доступ к чату, ответь на пару вопросов.\n"
        "1️⃣ <b>Как тебя зовут?</b>",
        parse_mode=ParseMode.HTML
    )

@dp.message(F.chat.type == "private", Registration.name)
async def process_name(message: types.Message, state: FSMContext):
    if db.is_blocked(message.from_user.id):
        return
    await state.update_data(name=message.text)
    await state.set_state(Registration.age)
    await message.answer("2️⃣ <b>Сколько тебе лет?</b> (Укажи реальный возраст):", parse_mode=ParseMode.HTML)

@dp.message(F.chat.type == "private", Registration.age)
async def process_age(message: types.Message, state: FSMContext):
    if db.is_blocked(message.from_user.id):
        return
    try:
        age = int(message.text)
        if age < 10 or age > 99:
            raise ValueError
    except ValueError:
        return await message.answer("⚠️ Пожалуйста, укажи реальный возраст числом!")

    await state.update_data(age=age)
    data = await state.get_data()
    await state.set_state(Registration.confirm)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да", callback_data="form_confirm"),
            InlineKeyboardButton(text="❌ Нет", callback_data="form_restart")
        ]
    ])

    await message.answer(
        f"📋 <b>Проверь свои данные:</b>\n"
        f"• <b>Имя:</b> {data['name']}\n"
        f"• <b>Возраст:</b> {data['age']}\n\n"
        f"<i>С правилами чата обязуешься ознакомиться при входе.</i>\n\n"
        f"<b>Всё верно?</b>",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(F.data == "form_restart")
async def restart_form(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(Registration.name)
    await cb.message.edit_text("🔄 Начнем заново.\n\n1️⃣ <b>Как тебя зовут?</b>", parse_mode=ParseMode.HTML)
    await cb.answer()

@dp.callback_query(F.data == "form_confirm")
async def confirm_form(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    user = cb.from_user
    app_id = db.create_application(user.id, user.username or "", data["name"], data["age"])

    await cb.message.edit_text("✅ <b>Твоя заявка отправлена администрации! Ожидай решения.</b>", parse_mode=ParseMode.HTML)
    await cb.answer()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"adm_accept_{app_id}_{user.id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_reject_{app_id}_{user.id}"),
            InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"adm_block_{app_id}_{user.id}")
        ]
    ])

    await bot.send_message(
        TARGET_CHAT_ID,
        f"📥 <b>НОВАЯ ЗАЯВКА В LOCALHAUS!</b>\n\n"
        f"👤 <b>Кандидат:</b> @{user.username or 'нет'} (ID: <code>{user.id}</code>)\n"
        f"📝 <b>Имя:</b> {data['name']}\n"
        f"🎂 <b>Возраст:</b> {data['age']}\n\n"
        f"Модератор @Leymik, примите решение!",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )

# Причина отклонения от Леймика в ЛС
@dp.message(F.chat.type == "private")
async def private_messages_router(message: types.Message):
    user_id = message.from_user.id
    if user_id == LEYMIK_ID and LEYMIK_ID in pending_rejections:
        target_id = pending_rejections.pop(LEYMIK_ID)
        try:
            await bot.send_message(
                target_id,
                f"❌ <b>Ваша заявка в Localhaus была отклонена.</b>\n💬 <b>Причина:</b> {message.text}",
                parse_mode=ParseMode.HTML
            )
            await message.reply("✅ Причина отправлена кандидату.")
        except Exception:
            await message.reply("⚠️ Не удалось отправить причину (возможно, бот заблокирован кандидатом).")

# Решения по заявкам (только Леймик)
@dp.callback_query(F.data.startswith("adm_"))
async def process_admin_decision(cb: types.CallbackQuery):
    if cb.from_user.id != LEYMIK_ID:
        return await cb.answer("⛔ Только @Leymik может выносить вердикт!", show_alert=True)

    parts = cb.data.split("_")
    action, app_id, target_id = parts[1], int(parts[2]), int(parts[3])
    app_data = db.get_application(app_id)

    if not app_data or app_data["status"] != "pending":
        return await cb.answer("Решение уже принято ранее.")

    if action == "accept":
        db.update_app_status(app_id, "accepted")
        try:
            await bot.send_message(
                target_id,
                f"🎉 <b>Твоя заявка в Localhaus одобрена!</b>\n\nСсылка на вход:\n{INVITE_LINK}",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
        await cb.message.edit_text(f"{cb.message.text}\n\n🟢 <b>ОДОБРЕНО (@Leymik)</b>", parse_mode=ParseMode.HTML)

    elif action == "reject":
        pending_rejections[LEYMIK_ID] = target_id
        db.update_app_status(app_id, "rejected")
        await cb.message.edit_text(f"{cb.message.text}\n\n🔴 <b>ОТКЛОНЕНО (@Leymik)</b>", parse_mode=ParseMode.HTML)
        try:
            await bot.send_message(LEYMIK_ID, f"Напишите сообщение с причиной отказа для заявки #{app_id}:")
        except Exception:
            pass

    elif action == "block":
        db.update_app_status(app_id, "blocked")
        db.set_blocked(target_id, 1)
        await cb.message.edit_text(f"{cb.message.text}\n\n🚫 <b>ПОЛЬЗОВАТЕЛЬ ЗАБЛОКИРОВАН</b>", parse_mode=ParseMode.HTML)

    await cb.answer()

# 4. Модерация и игровой процесс в группе
@dp.message(F.chat.id == TARGET_CHAT_ID)
async def handle_chat_message(message: types.Message):
    global last_admin_fetch
    user_id = message.from_user.id
    text = message.text or message.caption or ""
    lower_text = text.lower().strip()

    # Тихое авто-обновление админов раз в 10 минут
    if time.time() - last_admin_fetch > 600:
        await refresh_admin_cache()

    user_admin = is_admin(user_id)

    if message.from_user.username:
        db.save_member(user_id, message.from_user.username)

    # --- Проверка онлайна ---
    if lower_text == "бот ты тут?":
        return await message.reply("Да")

    # --- Правила ---
    if lower_text == "правила":
        return await message.reply(
            "📜 <b>ПРАВИЛА ЧАТА LOCALHAUS</b> 📜\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ <b>Спам лесенкой</b>: больше 5 сообщений за 3 сек — Мут 5 мин.\n"
            "2️⃣ <b>Спам стикерами</b>: удаление стикеров + Предупреждение.\n"
            "3️⃣ <b>Ссылки и реклама</b>: 1 раз — варн, 2 раза за день — Бан.\n"
            "4️⃣ <b>18+ контент</b>: порнография/шок — Варн / Бан.\n"
            "5️⃣ Уважение к участникам и администрации чата.\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )

    # --- Спам стикерами (3+ стикера за 5 сек) ---
    if message.sticker and not user_admin:
        now = time.time()
        stickers = user_stickers.get(user_id, [])
        stickers = [s for s in stickers if now - s["time"] < 5]
        stickers.append({"time": now, "id": message.message_id})
        user_stickers[user_id] = stickers

        if len(stickers) >= 3:
            for s in stickers:
                try:
                    await bot.delete_message(TARGET_CHAT_ID, s["id"])
                except Exception:
                    pass
            user_stickers.pop(user_id, None)
            return await message.answer(
                f"⚠️ <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a>, прошу пожалуйста не нарушать правила чата!\n"
                f"Чтобы узнать правила чата напишите <b>\"правила\"</b>.",
                parse_mode=ParseMode.HTML
            )

    # --- Спам лесенкой (более 5 сообщений за 3 сек) ---
    if not user_admin:
        now = time.time()
        history = user_messages.get(user_id, [])
        history = [t for t in history if now - t < 3]
        history.append(now)
        user_messages[user_id] = history

        if len(history) > 5:
            user_messages.pop(user_id, None)
            try:
                await message.delete()
                until = datetime.utcnow() + timedelta(minutes=5)
                await bot.restrict_chat_member(
                    TARGET_CHAT_ID,
                    user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until
                )
                return await message.answer(
                    f"🔇 <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> получил мут на 5 минут за спам лесенкой!",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

    # --- Ссылки и реклама ---
    url_pattern = r"(https?://\S+|t\.me/\S+|www\.\S+|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b)"
    if re.search(url_pattern, text) and not user_admin:
        try:
            await message.delete()
        except Exception:
            pass

        count = db.check_and_increment_links(user_id)
        if count >= 2:
            try:
                await bot.ban_chat_member(TARGET_CHAT_ID, user_id)
                return await message.answer(
                    f"🚫 <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> заблокирован за рекламу.",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
        else:
            return await message.answer(
                f"⚠️ <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a>, ссылки запрещены! (Предупреждение 1/2 за день)",
                parse_mode=ParseMode.HTML
            )

    # --- Баланс ---
    if lower_text in ["б", "баланс"]:
        user = db.get_user(user_id, message.from_user.username or "", message.from_user.first_name)
        return await message.reply(
            f"🍃 <b>Кошелек: <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a></b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"💰 Баланс: <b>{user['balance']}</b> 🍁 листочек\n"
            f"━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )

    # --- Казино (пример: "100 ч" или "50 к") ---
    bet_match = re.match(r"^(\d+)\s+([чк])$", lower_text)
    if bet_match:
        bet_amount = int(bet_match.group(1))
        chosen_color = bet_match.group(2)
        user = db.get_user(user_id, message.from_user.username or "", message.from_user.first_name)

        if bet_amount <= 0:
            return await message.reply("⚠️ Ставка должна быть больше 0!")
        if user["balance"] < bet_amount:
            return await message.reply(f"❌ <b>Недостаточно листочек!</b> Баланс: <b>{user['balance']}</b> 🍁", parse_mode=ParseMode.HTML)

        outcome = random.choice(["ч", "к"])
        outcome_name = "⬛ ЧЁРНЫЙ" if outcome == "ч" else "🟥 КРАСНЫЙ"
        chosen_name = "⬛ ЧЁРНЫЙ" if chosen_color == "ч" else "🟥 КРАСНЫЙ"

        if chosen_color == outcome:
            new_bal = db.update_balance(user_id, bet_amount)
            return await message.reply(
                f"🎰 <b>РУЛЕТКА LOCALHAUS</b> 🎰\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"🎯 Выбор: <b>{chosen_name}</b>\n"
                f"🎲 Выпало: <b>{outcome_name}</b>\n\n"
                f"🔥 <b>ПОБЕДА (2x)!</b> Выигрыш: <b>+{bet_amount}</b> 🍁 листочек!\n"
                f"💰 Новый баланс: <b>{new_bal}</b> 🍁",
                parse_mode=ParseMode.HTML
            )
        else:
            new_bal = db.update_balance(user_id, -bet_amount)
            return await message.reply(
                f"🎰 <b>РУЛЕТКА LOCALHAUS</b> 🎰\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"🎯 Выбор: <b>{chosen_name}</b>\n"
                f"🎲 Выпало: <b>{outcome_name}</b>\n\n"
                f"💀 <b>ПОРАЖЕНИЕ (0x)!</b> Ставка сгорела.\n"
                f"💰 Новый баланс: <b>{new_bal}</b> 🍁",
                parse_mode=ParseMode.HTML
            )

    # --- Случайный дроп (Шанс 5%) ---
    if random.random() < 0.05:
        reward = random.randint(10, 30)
        db.update_balance(user_id, reward)
        await message.reply(
            f"🍃 <b>Удача!</b> За активность <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> находит <b>{reward}</b> 🍁 листочек!",
            parse_mode=ParseMode.HTML
        )

    # --- Команды Администратора ---
    if not user_admin:
        return

    if lower_text == "бан":
        if not message.reply_to_message:
            return await message.reply("⚠️ Ответьте этой командой на сообщение нарушителя!")
        target = message.reply_to_message.from_user
        try:
            await bot.ban_chat_member(TARGET_CHAT_ID, target.id)
            await message.reply(f"🚫 <a href='tg://user?id={target.id}'>{target.first_name}</a> исключен и добавлен в ЧС.", parse_mode=ParseMode.HTML)
        except Exception:
            await message.reply("⚠️ Ошибка выполнения бана.")

    elif lower_text == "кик":
        if not message.reply_to_message:
            return await message.reply("⚠️ Ответьте этой командой на сообщение нарушителя!")
        target = message.reply_to_message.from_user
        try:
            await bot.ban_chat_member(TARGET_CHAT_ID, target.id)
            await bot.unban_chat_member(TARGET_CHAT_ID, target.id)
            await message.reply(f"🚪 <a href='tg://user?id={target.id}'>{target.first_name}</a> был исключен.", parse_mode=ParseMode.HTML)
        except Exception:
            await message.reply("⚠️ Ошибка при исключении.")

    elif lower_text.startswith("мут"):
        mute_match = re.match(r"^мут\s+(\d+)$", lower_text)
        if not mute_match or not message.reply_to_message:
            return await message.reply("⚠️ Формат: ответьте на сообщение текстом <code>мут 10</code>", parse_mode=ParseMode.HTML)
        minutes = int(mute_match.group(1))
        target = message.reply_to_message.from_user
        until = datetime.utcnow() + timedelta(minutes=minutes)
        try:
            await bot.restrict_chat_member(
                TARGET_CHAT_ID,
                target.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until
            )
            await message.reply(f"🔇 <a href='tg://user?id={target.id}'>{target.first_name}</a> получил мут на <b>{minutes} мин.</b>", parse_mode=ParseMode.HTML)
        except Exception:
            await message.reply("⚠️ Ошибка при выдаче мута.")

    elif lower_text == "калл":
        members = db.get_all_members()
        if not members:
            return await message.reply("Список участников пуст.")
        tags = " ".join([f"@{u}" for u in members])
        await message.answer(f"📢 <b>ОБЩИЙ СБОР ЧАТА!</b>\n\n{tags}", parse_mode=ParseMode.HTML)

# --- Задача Keep-Alive (Самопинг) ---
async def keep_alive_task():
    await asyncio.sleep(10)
    if not RENDER_EXTERNAL_URL:
        return
    url = RENDER_EXTERNAL_URL.rstrip('/')
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url) as resp:
                    print(f"[Keep-Alive] Self-ping status: {resp.status}")
            except Exception as e:
                print(f"[Keep-Alive] Ping fail: {e}")
            await asyncio.sleep(300) # каждые 5 минут

# --- Webhook & Aiohttp запуск ---
async def on_startup(app):
    await refresh_admin_cache()
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/webhook"
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        print(f"Вебхук установлен: {webhook_url}")
    asyncio.create_task(keep_alive_task())

async def root_handler(request):
    return web.Response(text="Bot is running!")

def main():
    app = web.Application()
    app.router.add_get("/", root_handler)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()

import os
import re
import time
import random
import asyncio
import aiohttp
from aiohttp import web
from datetime import datetime, timedelta
import pytz

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart
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
MSK_TZ = pytz.timezone("Europe/Moscow")

cached_admins = set()
last_admin_fetch = 0
user_messages = {}
user_stickers = {}
pending_rejections = {}
active_proposals = {}

# Активные задачи работы: { message_id: { "user_id": int, "answer": int, "expires_at": float } }
active_jobs = {}

class Registration(StatesGroup):
    name = State()
    age = State()
    confirm = State()

def get_msk_now() -> datetime:
    return datetime.now(MSK_TZ)

def get_msk_today_str() -> str:
    return get_msk_now().strftime("%Y-%m-%d")

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

def format_duration(start_dt: datetime) -> str:
    diff = datetime.utcnow() - start_dt
    days = diff.days
    if days < 30:
        return f"{days} дн."
    elif days < 365:
        months = days // 30
        rem_days = days % 30
        return f"{months} мес. {rem_days} дн."
    else:
        years = days // 365
        rem_months = (days % 365) // 30
        return f"{years} г. {rem_months} мес."

# --- События статуса бота и приветствие ---

@dp.my_chat_member()
async def bot_rights_updated(event: types.ChatMemberUpdated):
    if event.chat.id == TARGET_CHAT_ID and event.new_chat_member.status == ChatMemberStatus.ADMINISTRATOR:
        await refresh_admin_cache()
        await bot.send_message(
            TARGET_CHAT_ID,
            "✨ <b>Права выданы. Готов к работе!</b>",
            parse_mode=ParseMode.HTML
        )

@dp.message(F.chat.id == TARGET_CHAT_ID, F.new_chat_members)
async def welcome_members(message: types.Message):
    for member in message.new_chat_members:
        if member.id == bot.id:
            continue
        db.get_user(member.id, member.username or "", member.first_name)
        await message.reply(
            f"🌿 <b>Добро пожаловать в Localhaus, <a href='tg://user?id={member.id}'>{member.first_name}</a>!</b>\n"
            f"Обязательно прочитай правила — напиши <b>\"правила\"</b>, а также найди себе друга для общения! ✨",
            parse_mode=ParseMode.HTML
        )

# --- Анкеты в ЛС ---

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
            await message.reply("⚠️ Не удалось доставить сообщение кандидату.")

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

# --- CALLBACKS: БРАК И ПЛАТНЫЕ РП ---

@dp.callback_query(F.data.startswith("marry_"))
async def process_marriage_callback(cb: types.CallbackQuery):
    parts = cb.data.split("_")
    action = parts[1]
    proposer_id = int(parts[2])
    target_id = int(parts[3])

    if cb.from_user.id != target_id:
        return await cb.answer("💍 Это предложение адресовано не тебе!", show_alert=True)

    proposal = active_proposals.get(proposer_id)
    if not proposal or time.time() > proposal["expires_at"]:
        active_proposals.pop(proposer_id, None)
        await cb.message.edit_text("⏳ <b>Время на ответ (2 минуты) истекло!</b> Предложение аннулировано.", parse_mode=ParseMode.HTML)
        return await cb.answer("Время вышло!")

    active_proposals.pop(proposer_id, None)
    proposer = db.get_user(proposer_id)
    target = db.get_user(target_id)

    if action == "yes":
        db.create_marriage(proposer_id, proposer["first_name"], target_id, target["first_name"])
        await cb.message.edit_text(
            f"✨ <b>СВЯЩЕННЫЙ СОЮЗ ЗАКЛЮЧЁН!</b> ✨\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💍 <b><a href='tg://user?id={proposer_id}'>{proposer['first_name']}</a></b> и "
            f"<b><a href='tg://user?id={target_id}'>{target['first_name']}</a></b> теперь официально в браке!\n\n"
            f"🎉 Поздравляем молодожёнов! Горько! 🥂💫\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )
    else:
        await cb.message.edit_text(
            f"💔 <b>ОТКАЗ В ПРЕДЛОЖЕНИИ...</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🥀 <b><a href='tg://user?id={target_id}'>{target['first_name']}</a></b> отклонил(а) предложение "
            f"<b><a href='tg://user?id={proposer_id}'>{proposer['first_name']}</a></b>.\n"
            f"Сердце разбито... Но жизнь продолжается! 🌧️\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )
    await cb.answer()

@dp.callback_query(F.data.startswith("paid_rp_"))
async def process_paid_rp(cb: types.CallbackQuery):
    parts = cb.data.split("_")
    action_type = parts[2]
    user_id = int(parts[3])

    if cb.from_user.id != user_id:
        return await cb.answer("❌ Это не твоё действие!", show_alert=True)

    user = db.get_user(user_id)
    if user["balance"] < 30:
        return await cb.answer("❌ Недостаточно листочек! Нужно 30 🍁", show_alert=True)

    new_bal = db.update_balance(user_id, -30)
    name = cb.from_user.first_name

    text_map = {
        "smoke": f"🚬 <b><a href='tg://user?id={user_id}'>{name}</a></b> медленно достаёт сигарету, чиркает зажигалкой и выпускает густой клуб дыма в потолок... 💨",
        "snus": f"🌿 <b><a href='tg://user?id={user_id}'>{name}</a></b> со смаком закидывает плотный снюс под губу и довольно закатывает глаза... 🤤✨",
        "drink": f"🥃 <b><a href='tg://user?id={user_id}'>{name}</a></b> наливает себе крепкий напиток и залпом осушает бокал до дна! За ваше здоровье! 🍻"
    }

    await cb.message.edit_text(
        f"{text_map.get(action_type)}\n\n<i>Оплата: -30 🍁 листочек (Остаток: {new_bal} 🍁)</i>",
        parse_mode=ParseMode.HTML
    )
    await cb.answer()

# --- ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ ЧАТА ---

@dp.message(F.chat.id == TARGET_CHAT_ID)
async def handle_chat_message(message: types.Message):
    global last_admin_fetch
    user_id = message.from_user.id
    text = message.text or message.caption or ""
    lower_text = text.lower().strip()

    # --- 🔒 АНТИ-ДЮП КАЗИНО 777 (ПРОВЕРКА НА ПЕРЕСЫЛКУ) ---
    if message.dice and message.dice.emoji == "🎰":
        # Проверяем, переслано ли сообщение
        is_forwarded = bool(
            message.forward_date or 
            message.forward_from or 
            message.forward_from_chat or 
            getattr(message, 'forward_origin', None)
        )
        if is_forwarded:
            # Пересланный дайс игнорируется! Дюп заблокирован.
            return

        # Значение 64 в анимации 🎰 — это именно три семерки (777)
        if message.dice.value == 64:
            new_bal = db.update_balance(user_id, 200)
            return await message.reply(
                f"🎰🔥 <b>ДЖЕКПОТ! ТРИ СЕМЁРКИ (777)!</b> 🔥🎰\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎉 Честная удача! <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> сам крутанул слот и сорвал джекпот!\n"
                f"💰 <b>Выигрыш: +200</b> 🍁 листочек на баланс!\n"
                f"🍃 Текущий кошелек: <b>{new_bal}</b> 🍁\n"
                f"━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML
            )
        return

    # Запись активности
    msk_today = get_msk_today_str()
    db.get_user(user_id, message.from_user.username or "", message.from_user.first_name)
    db.record_message(user_id, msk_today)

    if time.time() - last_admin_fetch > 600:
        await refresh_admin_cache()

    user_admin = is_admin(user_id)

    if message.from_user.username:
        db.save_member(user_id, message.from_user.username)

    # --- ПРОВЕРКА ОТВЕТА НА РАБОТУ ---
    if message.reply_to_message and message.reply_to_message.message_id in active_jobs:
        job = active_jobs[message.reply_to_message.message_id]
        if job["user_id"] != user_id:
            return await message.reply("⚠️ Это задание решает другой работник!")

        if time.time() > job["expires_at"]:
            del active_jobs[message.reply_to_message.message_id]
            return await message.reply("⏳ Время на решение (2 минуты) вышло! Пример аннулирован.")

        raw_text = text.strip()
        if not re.match(r"^-?\d+$", raw_text):
            return await message.reply("⚠️ Ответ должен содержать <b>только одно число</b> без букв и знаков!", parse_mode=ParseMode.HTML)

        user_ans = int(raw_text)
        correct_ans = job["answer"]
        del active_jobs[message.reply_to_message.message_id]

        if user_ans == correct_ans:
            new_bal = db.update_balance(user_id, 3)
            return await message.reply(
                f"✅ <b>ВЕРНО! ПРИМЕР РЕШЁН!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 Правильный ответ: <b>{correct_ans}</b>\n"
                f"💼 Зарплата: <b>+3</b> 🍁 листочка зачислено!\n"
                f"💰 Текущий баланс: <b>{new_bal}</b> 🍁",
                parse_mode=ParseMode.HTML
            )
        else:
            new_bal = db.update_balance(user_id, -40)
            return await message.reply(
                f"❌ <b>ОШИБКА В ВЫЧИСЛЕНИЯХ!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Твой ответ: <code>{user_ans}</code>\n"
                f"Правильный ответ: <b>{correct_ans}</b>\n\n"
                f"📉 Штраф за брак: <b>-40</b> 🍁 листочек!\n"
                f"💰 Текущий баланс: <b>{new_bal}</b> 🍁",
                parse_mode=ParseMode.HTML
            )

    # Онлайн проверка
    if lower_text == "бот ты тут?":
        return await message.reply("Да")

    # Правила
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

    # --- Спам стикерами ---
    if message.sticker and not user_admin:
        now = time.time()
        stickers = user_stickers.get(user_id, [])
        stickers = [s for s in stickers if now - s["time"] < 5]
        stickers.append({"time": now, "id": message.message_id})
        user_stickers[user_id] = stickers

        if len(stickers) >= 3:
            for s in stickers:
                try: await bot.delete_message(TARGET_CHAT_ID, s["id"])
                except Exception: pass
            user_stickers.pop(user_id, None)
            return await message.answer(
                f"⚠️ <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a>, прошу пожалуйста не нарушать правила чата!\n"
                f"Чтобы узнать правила чата напишите <b>\"правила\"</b>.",
                parse_mode=ParseMode.HTML
            )

    # --- Спам лесенкой ---
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
                    f"⚠️ <b>ВНИМАНИЕ!</b>\n"
                    f"🔇 <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> получает мут на 5 минут за спам лесенкой!",
                    parse_mode=ParseMode.HTML
                )
            except Exception: pass

    # --- Анти-ссылки ---
    url_pattern = r"(https?://\S+|t\.me/\S+|www\.\S+|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b)"
    if re.search(url_pattern, text) and not user_admin:
        try: await message.delete()
        except Exception: pass

        count = db.check_and_increment_links(user_id)
        if count >= 2:
            try:
                await bot.ban_chat_member(TARGET_CHAT_ID, user_id)
                return await message.answer(
                    f"🚫 <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> заблокирован за повторную рекламу.",
                    parse_mode=ParseMode.HTML
                )
            except Exception: pass
        else:
            return await message.answer(
                f"⚠️ <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a>, ссылки запрещены! (Предупреждение 1/2 за день)",
                parse_mode=ParseMode.HTML
            )

    # --- 👑 УПРАВЛЕНИЕ БАЛАНСОМ (ТОЛЬКО ДЛЯ LEYMIK) ---
    if user_id == LEYMIK_ID:
        # Снять баланс
        remove_match = re.match(r"^снять\s+баланс\s+(\d+)$", lower_text)
        if remove_match:
            if not message.reply_to_message:
                return await message.reply("⚠️ Ответь этой командой на сообщение того, у кого нужно забрать листочки!")
            target = message.reply_to_message.from_user
            amount = int(remove_match.group(1))
            t_data = db.get_user(target.id, target.username or "", target.first_name)
            
            # Снимаем не больше чем есть (до нуля)
            actual_remove = min(amount, t_data["balance"])
            new_bal = db.update_balance(target.id, -actual_remove)
            return await message.reply(
                f"⚖️ <b>ИЗЪЯТИЕ СРЕДСТВ АДМИНИСТРАЦИЕЙ</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👑 Модератор: <b>@Leymik</b>\n"
                f"👤 Нарушитель: <b><a href='tg://user?id={target.id}'>{target.first_name}</a></b>\n"
                f"📉 Списано: <b>-{actual_remove}</b> 🍁 листочек\n"
                f"💰 Текущий остаток игрока: <b>{new_bal}</b> 🍁\n"
                f"━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML
            )

        # Выдать баланс
        give_match = re.match(r"^выдать\s+баланс\s+(\d+)$", lower_text)
        if give_match:
            if not message.reply_to_message:
                return await message.reply("⚠️ Ответь этой командой на сообщение того, кому выдать листочки!")
            target = message.reply_to_message.from_user
            amount = int(give_match.group(1))
            db.get_user(target.id, target.username or "", target.first_name)
            new_bal = db.update_balance(target.id, amount)
            return await message.reply(
                f"🎁 <b>ВЫДАЧА СРЕДСТВ АДМИНИСТРАЦИЕЙ</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👑 Модератор: <b>@Leymik</b>\n"
                f"👤 Получатель: <b><a href='tg://user?id={target.id}'>{target.first_name}</a></b>\n"
                f"📈 Начислено: <b>+{amount}</b> 🍁 листочек\n"
                f"💰 Новый баланс игрока: <b>{new_bal}</b> 🍁\n"
                f"━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML
            )

    # --- КОМАНДА "РАБОТА" ---
    if lower_text == "работа":
        num1 = random.randint(100, 99999)
        num2 = random.randint(100, 99999)
        op = random.choice(["+", "-"])
        correct = num1 + num2 if op == "+" else num1 - num2

        sent_job = await message.reply(
            f"💼 <b>РАБОЧАЯ СМЕНА В LOCALHAUS</b> 💼\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Оплата за верное решение: <b>3 🍁 листочка</b>\n"
            f"⚠️ Штраф за ошибку: <b>-40 🍁 листочек</b>\n"
            f"⏳ Время на решение: <b>2 минуты</b>\n\n"
            f"🧮 <b>Реши пример:</b>\n"
            f"👉 <code>{num1} {op} {num2} = ?</code>\n\n"
            f"<i>Для ответа ответьте (reply) на это сообщение ТОЛЬКО числом!</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )
        active_jobs[sent_job.message_id] = {
            "user_id": user_id,
            "answer": correct,
            "expires_at": time.time() + 120
        }
        return

    # --- КОМАНДА "ЛЮСТРА" ---
    if lower_text == "люстра":
        members = db.get_all_members()
        random_user = random.choice(members) if members else "кого-то из присутствующих"
        target_tag = f"@{random_user}" if random_user != "кого-то из присутствующих" else random_user
        return await message.answer(
            f"Люстра люстра няш няш аф аф люблю сочно сучку {target_tag}"
        )

    # --- БЕСПЛАТНЫЕ РП ДЕЙСТВИЯ: ОБНЯТЬ, ПОЦЕЛОВАТЬ, ВЫЕБАТЬ, ЛИЗЬ ---
    rp_actions = {
        "обнять": ("обнял(а)", "крепко обнимает и согревает теплом"),
        "поцеловать": ("поцеловал(а)", "нежно и чувственно целует в губы"),
        "выебать": ("выебал(а)", "жестко и без лишних прелюдий выебал(а)"),
        "лизь": ("лизнул(а)", "игриво и влажно лизнул(а) за ушком")
    }
    if lower_text in rp_actions:
        if not message.reply_to_message:
            return await message.reply("🎭 Ответь этой командой на сообщение того, к кому обращено действие!")
        target = message.reply_to_message.from_user
        act_word, act_desc = rp_actions[lower_text]
        return await message.answer(
            f"✨ <b>РП ДЕЙСТВИЕ</b> ✨\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🐾 <b><a href='tg://user?id={user_id}'>{message.from_user.first_name}</a></b> {act_desc} "
            f"<b><a href='tg://user?id={target.id}'>{target.first_name}</a></b>! 💖\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )

    # --- ПЛАТНЫЕ РП ДЕЙСТВИЯ (30 листочек): ПОКУРИТЬ, ЗАКИНУТЬ СНЮС, ВЫПИТЬ ---
    if lower_text in ["покурить", "закинуть снюс", "выпить"]:
        act_type = "smoke" if lower_text == "покурить" else "snus" if lower_text == "закинуть снюс" else "drink"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🍁 Заплатить 30 листочек", callback_data=f"paid_rp_{act_type}_{user_id}")]
        ])
        return await message.reply(
            f"🚬 <b>ПЛАТНОЕ РП: {text.upper()}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Для выполнения этого действия необходимо заплатить <b>30 🍁 листочек</b>.\n"
            f"Нажмите кнопку ниже для оплаты:",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

    # --- КОМАНДА "КТО ТЫ" ---
    if lower_text == "кто ты":
        if not message.reply_to_message:
            return await message.reply("🔍 Ответь этой командой на сообщение пользователя!")
        target = message.reply_to_message.from_user
        t_data = db.get_user(target.id, target.username or "", target.first_name)
        t_stats = db.get_user_stats(target.id, msk_today)
        
        rank = "👑 Главный Модератор" if target.id == LEYMIK_ID else "🛡️ Администратор" if is_admin(target.id) else "👤 Пользователь"
        
        joined_str = t_data.get("joined_at", "Неизвестно")
        try:
            dt_obj = datetime.strptime(str(joined_str).split(".")[0], "%Y-%m-%d %H:%M:%S")
            joined_formatted = dt_obj.strftime("%d.%m.%Y в %H:%M UTC")
        except Exception:
            joined_formatted = str(joined_str)

        m_info = db.get_marriage(target.id)
        if m_info:
            partner_id = m_info["user2_id"] if m_info["user1_id"] == target.id else m_info["user1_id"]
            partner_name = m_info["user2_name"] if m_info["user1_id"] == target.id else m_info["user1_name"]
            marriage_status = f"В браке с <a href='tg://user?id={partner_id}'>{partner_name}</a> 💍"
        else:
            marriage_status = "Холост / Не замужем 🕊️"

        return await message.reply(
            f"👤 <b>ДОСЬЕ УЧАСТНИКА LOCALHAUS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷 <b>Имя:</b> <a href='tg://user?id={target.id}'>{target.first_name}</a>\n"
            f"🆔 <b>ID:</b> <code>{target.id}</code>\n"
            f"🎖 <b>Ранг:</b> <b>{rank}</b>\n"
            f"💍 <b>Семейное положение:</b> {marriage_status}\n"
            f"💰 <b>Баланс:</b> <b>{t_data['balance']}</b> 🍁 листочек\n"
            f"📅 <b>Первый визит:</b> {joined_formatted}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>АКТИВНОСТЬ СООБЩЕНИЙ:</b>\n"
            f"• <b>За сегодня:</b> {t_stats['day']} сообщ.\n"
            f"• <b>За 7 дней:</b> {t_stats['week']} сообщ.\n"
            f"• <b>За всё время:</b> {t_stats['all']} сообщ.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )

    # --- БРАК, РАЗВОД, БРАКИ ---
    if lower_text == "брак":
        if not message.reply_to_message:
            return await message.reply("💍 Ответь этой командой на сообщение того, кому делаешь предложение!")
        target = message.reply_to_message.from_user
        if target.id == user_id:
            return await message.reply("😅 Нельзя заключить брак с самим собой!")
        if target.id == bot.id:
            return await message.reply("🤖 Моё сердце принадлежит коду!")

        if db.get_marriage(user_id):
            return await message.reply("⚠️ Ты уже состоишь в браке! Чтобы сделать новое предложение, сначала напиши <b>\"развод\"</b>.", parse_mode=ParseMode.HTML)
        if db.get_marriage(target.id):
            return await message.reply(f"💔 <a href='tg://user?id={target.id}'>{target.first_name}</a> уже в браке!", parse_mode=ParseMode.HTML)

        if user_id in active_proposals:
            existing = active_proposals[user_id]
            if time.time() < existing["expires_at"]:
                rem = int(existing["expires_at"] - time.time())
                return await message.reply(f"⏳ Твоё прошлое предложение ещё в силе! Подожди <b>{rem} сек.</b>", parse_mode=ParseMode.HTML)

        active_proposals[user_id] = {
            "target_id": target.id,
            "expires_at": time.time() + 120
        }

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="💖 Согласиться", callback_data=f"marry_yes_{user_id}_{target.id}"),
                InlineKeyboardButton(text="💔 Отказаться", callback_data=f"marry_no_{user_id}_{target.id}")
            ]
        ])

        return await message.answer(
            f"💍 <b>МИНУТОЧКУ ВНИМАНИЯ!</b> 💍\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🌹 <b><a href='tg://user?id={user_id}'>{message.from_user.first_name}</a></b> делает предложение руки и сердца "
            f"<b><a href='tg://user?id={target.id}'>{target.first_name}</a></b>!\n\n"
            f"⏳ <i>У вас есть ровно 2 минуты на ответ...</i> ✨\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

    if lower_text == "развод":
        m_info = db.get_marriage(user_id)
        if not m_info:
            return await message.reply("🕊️ Ты не состоишь в браке!")
        partner_id = m_info["user2_id"] if m_info["user1_id"] == user_id else m_info["user1_id"]
        partner_name = m_info["user2_name"] if m_info["user1_id"] == user_id else m_info["user1_name"]
        db.delete_marriage(user_id)
        return await message.answer(
            f"📜 <b>РАСТОРЖЕНИЕ БРАКА</b> 📜\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💔 <b><a href='tg://user?id={user_id}'>{message.from_user.first_name}</a></b> объявил(а) о разводе с "
            f"<b><a href='tg://user?id={partner_id}'>{partner_name}</a></b>.\n"
            f"Брачный союз расторгнут. 🕊️\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )

    if lower_text == "браки":
        marriages = db.get_all_marriages()
        if not marriages:
            return await message.reply("🕊️ В чате пока нет супружеских пар. Будьте первыми!")
        lines = ["💍 <b>СЕМЕЙНЫЙ СОЮЗ LOCALHAUS</b> 💍\n━━━━━━━━━━━━━━━━━━━━━━"]
        for idx, m in enumerate(marriages, 1):
            dt = datetime.strptime(str(m["married_at"]).split(".")[0], "%Y-%m-%d %H:%M:%S")
            duration = format_duration(dt)
            lines.append(
                f"{idx}. <b><a href='tg://user?id={m['user1_id']}'>{m['user1_name']}</a></b> 💖 "
                f"<b><a href='tg://user?id={m['user2_id']}'>{m['user2_name']}</a></b> — вместе <b>{duration}</b>"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        return await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

    # --- ПЕРЕВОД ЛИСТОЧКОВ: "п (число)" ---
    transfer_match = re.match(r"^п\s+(\d+)$", lower_text)
    if transfer_match:
        if not message.reply_to_message:
            return await message.reply("🍁 Ответь этой командой на сообщение того, кому переводишь листочки!")
        target = message.reply_to_message.from_user
        if target.id == user_id:
            return await message.reply("😅 Нельзя переводить валюту самому себе!")
        if target.id == bot.id:
            return await message.reply("🍃 Спасибо, но боту валюта не нужна!")

        amount = int(transfer_match.group(1))
        if amount <= 0:
            return await message.reply("⚠️ Сумма должна быть больше 0!")

        sender = db.get_user(user_id, message.from_user.username or "", message.from_user.first_name)
        if sender["balance"] < amount:
            return await message.reply(f"❌ <b>Недостаточно листочек!</b> Баланс: <b>{sender['balance']}</b> 🍁", parse_mode=ParseMode.HTML)

        db.get_user(target.id, target.username or "", target.first_name)
        new_sender_bal = db.update_balance(user_id, -amount)
        new_target_bal = db.update_balance(target.id, amount)

        return await message.answer(
            f"💸 <b>УСПЕШНЫЙ ПЕРЕВОД ЛИСТОЧЕК</b> 🍁\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Отправитель: <b><a href='tg://user?id={user_id}'>{message.from_user.first_name}</a></b>\n"
            f"🎁 Получатель: <b><a href='tg://user?id={target.id}'>{target.first_name}</a></b>\n"
            f"💰 Сумма: <b>{amount}</b> 🍁 листочек\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Твой остаток: <b>{new_sender_bal}</b> 🍁",
            parse_mode=ParseMode.HTML
        )

    # --- СТАТИСТИКА: "стата" ---
    if lower_text == "стата":
        top_users = db.get_top_daily(msk_today, 5)
        if not top_users:
            return await message.reply("📊 Сегодня еще никто не проявлял активности.")
        now_msk_str = get_msk_now().strftime("%d.%m.%Y %H:%M")
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        lines = [f"🏆 <b>ТОП-5 АКТИВА НА {now_msk_str} (МСК)</b> 🏆\n━━━━━━━━━━━━━━━━━━━━━━"]
        for idx, u in enumerate(top_users):
            medal = medals[idx] if idx < len(medals) else f"{idx+1}."
            lines.append(f"{medal} <b><a href='tg://user?id={u['user_id']}'>{u['first_name']}</a></b> — <b>{u['msg_count']}</b> сообщ.")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━\n⏰ <i>Итоги ровно в 00:00 по МСК! Победитель получает +500 🍁</i>")
        return await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

    # --- БАЛАНС ---
    if lower_text in ["б", "баланс"]:
        user = db.get_user(user_id, message.from_user.username or "", message.from_user.first_name)
        return await message.reply(
            f"🍃 <b>Кошелек: <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a></b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"💰 Баланс: <b>{user['balance']}</b> 🍁 листочек\n"
            f"━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )

    # --- РУЛЕТКА ДЕП ("100 ч" / "50 к") ---
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

    # --- ДРОП ЗА АКТИВНОСТЬ: ШАНС 3% ---
    if random.random() < 0.03:
        reward = random.randint(10, 30)
        db.update_balance(user_id, reward)
        await message.reply(
            f"🍃 <b>Удача!</b> За активность <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> находит <b>{reward}</b> 🍁 листочек!",
            parse_mode=ParseMode.HTML
        )

    # --- АДМИН КОМАНДЫ ДЛЯ ЧАТА ---
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
            return await message.reply("⚠️ Формат: ответьте текстом <code>мут 10</code> на сообщение", parse_mode=ParseMode.HTML)
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

    elif lower_text == "размут":
        if not message.reply_to_message:
            return await message.reply("⚠️ Ответьте этой командой на сообщение пользователя!")
        target = message.reply_to_message.from_user
        try:
            await bot.restrict_chat_member(
                TARGET_CHAT_ID,
                target.id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True
                )
            )
            await message.reply(f"🔊 С пользователя <a href='tg://user?id={target.id}'>{target.first_name}</a> сняты все ограничения!", parse_mode=ParseMode.HTML)
        except Exception:
            await message.reply("⚠️ Ошибка при снятии мута.")

    elif lower_text == "калл":
        members = db.get_all_members()
        if not members:
            return await message.reply("Список участников пуст.")
        tags = " ".join([f"@{u}" for u in members])
        await message.answer(f"📢 <b>ОБЩИЙ СБОР ЧАТА!</b>\n\n{tags}", parse_mode=ParseMode.HTML)

# --- ЕЖЕДНЕВНЫЙ ОТЧЁТ РОВНО В 00:00 ПО МСК ---
async def midnight_msk_scheduler():
    while True:
        now_msk = get_msk_now()
        tomorrow_msk = (now_msk + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_until_midnight = (tomorrow_msk - now_msk).total_seconds()

        await asyncio.sleep(seconds_until_midnight)

        ended_day = (get_msk_now() - timedelta(minutes=5)).strftime("%Y-%m-%d")
        display_date = (get_msk_now() - timedelta(minutes=5)).strftime("%d.%m.%Y")
        today_date_str = get_msk_now().strftime("%d.%m.%Y")

        if not db.is_reward_given(ended_day):
            top_users = db.get_top_daily(ended_day, 5)
            if top_users:
                medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
                lines = [
                    f"👑 <b>ИТОГИ ДНЯ ЗА {display_date} (00:00 МСК)</b> 👑\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Полночь наступила! Дата: <b>{today_date_str} 00:00 МСК</b>\n"
                    f"Вот наши самые активные участники за прошедшие 24 часа:\n"
                ]
                for idx, u in enumerate(top_users):
                    medal = medals[idx] if idx < len(medals) else f"{idx+1}."
                    lines.append(f"{medal} <b><a href='tg://user?id={u['user_id']}'>{u['first_name']}</a></b> — <b>{u['msg_count']}</b> сообщ.")

                winner = top_users[0]
                db.update_balance(winner["user_id"], 500)
                lines.append(
                    f"\n🎉 Победитель дня — <b><a href='tg://user?id={winner['user_id']}'>{winner['first_name']}</a></b>!\n"
                    f"🎁 Награда: <b>+500</b> 🍁 листочек успешно зачислена на баланс!\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Новый день начался! Общайтесь активнее! ✨"
                )
                try:
                    await bot.send_message(TARGET_CHAT_ID, "\n".join(lines), parse_mode=ParseMode.HTML)
                except Exception as e:
                    print(f"Ошибка отправки итогов: {e}")

            db.mark_reward_given(ended_day)
        await asyncio.sleep(5)

# --- KEEP-ALIVE ТАСКА ДЛЯ RENDER ---
async def keep_alive_task():
    await asyncio.sleep(10)
    if not RENDER_EXTERNAL_URL:
        return
    url = RENDER_EXTERNAL_URL.rstrip('/')
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url):
                    pass
            except Exception:
                pass
            await asyncio.sleep(300)

async def on_startup(app):
    await refresh_admin_cache()
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/webhook"
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        print(f"Вебхук активен: {webhook_url}")
    asyncio.create_task(keep_alive_task())
    asyncio.create_task(midnight_msk_scheduler())

async def root_handler(request):
    return web.Response(text="Localhaus Bot is Alive!")

def main():
    app = web.Application()
    app.router.add_get("/", root_handler)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()

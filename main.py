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

cached_admins = set()
last_admin_fetch = 0
user_messages = {}
user_stickers = {}
pending_rejections = {}

class Registration(StatesGroup):
    name = State()
    age = State()
    confirm = State()

async def refresh_admin_cache():
    global cached_admins, last_admin_fetch
    try:
        admins = await bot.get_chat_administrators(TARGET_CHAT_ID)
        cached_admins = {admin.user.id for admin in admins}
        last_admin_fetch = time.time()
    except Exception as e:
        print(f"Ошибка админов: {e}")

def is_admin(user_id: int) -> bool:
    if user_id == LEYMIK_ID:
        return True
    return user_id in cached_admins

def format_duration(start_dt: datetime) -> str:
    now = datetime.utcnow()
    diff = now - start_dt
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

# --- КНОПКИ ДЛЯ БРАКА ---

@dp.callback_query(F.data.startswith("marry_"))
async def process_marriage_callback(cb: types.CallbackQuery):
    parts = cb.data.split("_")
    action = parts[1]
    proposer_id = int(parts[2])
    target_id = int(parts[3])

    if cb.from_user.id != target_id:
        return await cb.answer("💍 Это предложение адресовано не тебе!", show_alert=True)

    proposer = db.get_user(proposer_id)
    target = db.get_user(target_id)

    if action == "yes":
        db.create_marriage(
            proposer_id, proposer["first_name"],
            target_id, target["first_name"]
        )
        await cb.message.edit_text(
            f"✨ <b>СВЯЩЕННЫЙ СОЮЗ ЗАКЛЮЧЁН!</b> ✨\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💍 <b><a href='tg://user?id={proposer_id}'>{proposer['first_name']}</a></b> и "
            f"<b><a href='tg://user?id={target_id}'>{target['first_name']}</a></b> теперь официально в браке!\n\n"
            f"🎉 Поздравляем молодоженов! Горько! 🥂💫\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )
    else:
        await cb.message.edit_text(
            f"💔 <b>ГОРЕЧЬ И РАЗОЧАРОВАНИЕ...</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🥀 <b><a href='tg://user?id={target_id}'>{target['first_name']}</a></b> ответил(а) отказом на предложение "
            f"<b><a href='tg://user?id={proposer_id}'>{proposer['first_name']}</a></b>.\n"
            f"Сердце разбито, но жизнь в Localhaus продолжается... 🌧️\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )
    await cb.answer()

# --- ОСНОВНОЙ ЧАТ ---

@dp.message(F.chat.id == TARGET_CHAT_ID)
async def handle_chat_message(message: types.Message):
    global last_admin_fetch
    user_id = message.from_user.id
    text = message.text or message.caption or ""
    lower_text = text.lower().strip()

    # Фиксация активности для топа
    db.get_user(user_id, message.from_user.username or "", message.from_user.first_name)
    db.record_message(user_id)

    if time.time() - last_admin_fetch > 600:
        await refresh_admin_cache()

    user_admin = is_admin(user_id)

    if message.from_user.username:
        db.save_member(user_id, message.from_user.username)

    if lower_text == "бот ты тут?":
        return await message.reply("Да")

    if lower_text == "правила":
        return await message.reply(
            "📜 <b>ПРАВИЛА ЧАТА LOCALHAUS</b> 📜\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ <b>Спам лесенкой</b>: больше 5 сообщений за 3 сек — Предупреждение / Мут 5 мин.\n"
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

    # --- Спам текстом лесенкой (> 5 сообщений за 3 сек) ---
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
                    f"🔇 <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> получает предупреждение и мут на 5 минут за спам лесенкой!",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

    # --- Анти-ссылки ---
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

    # --- ФУНКЦИЯ БРАКОВ: "брак" и "браки" ---

    if lower_text == "брак":
        if not message.reply_to_message:
            return await message.reply("💍 Ответь этой командой на сообщение того, кому хочешь сделать предложение!")
        
        target = message.reply_to_message.from_user
        if target.id == user_id:
            return await message.reply("😅 Нельзя заключить брак с самим собой!")
        if target.id == bot.id:
            return await message.reply("🤖 Я всего лишь скромный бот, моё сердце занято кодом!")

        target_data = db.get_user(target.id, target.username or "", target.first_name)
        
        if db.get_marriage(user_id):
            return await message.reply("⚠️ Ты уже состоишь в законном браке!")
        if db.get_marriage(target.id):
            return await message.reply(f"💔 <a href='tg://user?id={target.id}'>{target.first_name}</a> уже в браке с другим человеком!", parse_mode=ParseMode.HTML)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="💖 Согласиться", callback_data=f"marry_yes_{user_id}_{target.id}"),
                InlineKeyboardButton(text="💔 Отказаться", callback_data=f"marry_no_{user_id}_{target.id}")
            ]
        ])

        return await message.answer(
            f"💍 <b>МИНУТОЧКУ ВНИМАНИЯ!</b> 💍\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🌹 <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> искренне и с трепетом делает предложение руки и сердца "
            f"<a href='tg://user?id={target.id}'>{target.first_name}</a>!\n\n"
            f"<i>Ждём решения второй половинки... согласны ли вы связать свои судьбы в Localhaus?</i> ✨\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

    if lower_text == "браки":
        marriages = db.get_all_marriages()
        if not marriages:
            return await message.reply("🕊️ В чате пока нет ни одной супружеской пары. Будьте первыми!")

        lines = ["💍 <b>СЕМЕЙНЫЙ СОЮЗ LOCALHAUS</b> 💍\n━━━━━━━━━━━━━━━━━━━━━━"]
        for idx, m in enumerate(marriages, 1):
            dt = datetime.strptime(m["married_at"].split(".")[0], "%Y-%m-%d %H:%M:%S")
            duration = format_duration(dt)
            lines.append(
                f"{idx}. <b><a href='tg://user?id={m['user1_id']}'>{m['user1_name']}</a></b> 💖 "
                f"<b><a href='tg://user?id={m['user2_id']}'>{m['user2_name']}</a></b> — вместе уже <b>{duration}</b>"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        return await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

    # --- ПЕРЕДАЧА ЛИСТОЧКОВ: "п (число)" ---

    transfer_match = re.match(r"^п\s+(\d+)$", lower_text)
    if transfer_match:
        if not message.reply_to_message:
            return await message.reply("🍁 Ответь этой командой на сообщение того, кому переводишь листочки!")
        
        target = message.reply_to_message.from_user
        if target.id == user_id:
            return await message.reply("😅 Переводить листочки самому себе бессмысленно!")
        if target.id == bot.id:
            return await message.reply("🍃 Спасибо, но боту валюта не нужна!")

        amount = int(transfer_match.group(1))
        if amount <= 0:
            return await message.reply("⚠️ Сумма перевода должна быть больше 0!")

        sender = db.get_user(user_id, message.from_user.username or "", message.from_user.first_name)
        if sender["balance"] < amount:
            return await message.reply(f"❌ <b>Недостаточно листочек!</b> Твой баланс: <b>{sender['balance']}</b> 🍁", parse_mode=ParseMode.HTML)

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
            f"Остаток на балансе: <b>{new_sender_bal}</b> 🍁",
            parse_mode=ParseMode.HTML
        )

    # --- СТАТИСТИКА СООБЩЕНИЙ ЗА ДЕНЬ: "стата" ---

    if lower_text == "стата":
        top_users = db.get_top_daily(5)
        if not top_users:
            return await message.reply("📊 Сегодня еще никто не проявлял активности.")

        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        lines = [
            "🏆 <b>ТОП-5 АКТИВА ЗА ДЕНЬ В LOCALHAUS</b> 🏆\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        ]
        for idx, u in enumerate(top_users):
            medal = medals[idx] if idx < len(medals) else f"{idx+1}."
            lines.append(f"{medal} <b><a href='tg://user?id={u['user_id']}'>{u['first_name']}</a></b> — <b>{u['msg_count']}</b> сообщ.")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━\n🎁 <i>Лидер дня по итогам суток получает +500 🍁 листочек!</i>")
        return await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

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
                f"🎯 Выбор: <b>${chosen_name}</b>\n"
                f"🎲 Выпало: <b>${outcome_name}</b>\n\n"
                f"🔥 <b>ПОБЕДА (2x)!</b> Выигрыш: <b>+{bet_amount}</b> 🍁 листочек!\n"
                f"💰 Новый баланс: <b>{new_bal}</b> 🍁",
                parse_mode=ParseMode.HTML
            )
        else:
            new_bal = db.update_balance(user_id, -bet_amount)
            return await message.reply(
                f"🎰 <b>РУЛЕТКА LOCALHAUS</b> 🎰\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"🎯 Выбор: <b>${chosen_name}</b>\n"
                f"🎲 Выпало: <b>${outcome_name}</b>\n\n"
                f"💀 <b>ПОРАЖЕНИЕ (0x)!</b> Ставка сгорела.\n"
                f"💰 Новый баланс: <b>{new_bal}</b> 🍁",
                parse_mode=ParseMode.HTML
            )

    # --- Дроп за активность (5%) ---
    if random.random() < 0.05:
        reward = random.randint(10, 30)
        db.update_balance(user_id, reward)
        await message.reply(
            f"🍃 <b>Удача!</b> За активность <a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> находит <b>{reward}</b> 🍁 листочек!",
            parse_mode=ParseMode.HTML
        )

    # --- АДМИН КОМАНДЫ ---
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

    elif lower_text == "размут":
        if not message.reply_to_message:
            return await message.reply("⚠️ Ответьте этой командой на сообщение того, кого хотите размутить!")
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

# --- ЕЖЕДНЕВНАЯ ВЫДАЧА НАГРАДЫ ЗА ТОП-1 (500 листочек) ---
async def daily_reward_checker():
    while True:
        await asyncio.sleep(60)
        yesterday_str = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        if not db.is_reward_given(yesterday_str):
            top_user = db.get_yesterday_top()
            if top_user and top_user["msg_count"] > 0:
                db.update_balance(top_user["user_id"], 500)
                try:
                    await bot.send_message(
                        TARGET_CHAT_ID,
                        f"👑 <b>ИТОГИ ДНЯ: САМЫЙ АКТИВНЫЙ УЧАСТНИК!</b> 👑\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🥇 Вчера лидером общения стал(а) <b><a href='tg://user?id={top_user['user_id']}'>{top_user['first_name']}</a></b> "
                        f"с результатом в <b>{top_user['msg_count']}</b> сообщений!\n\n"
                        f"🎁 Награда: <b>+500</b> 🍁 листочек начислена на баланс! Поздравляем! 🎉\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━",
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass
            db.mark_reward_given(yesterday_str)

# --- KEEP-ALIVE ТАСКА ---
async def keep_alive_task():
    await asyncio.sleep(10)
    if not RENDER_EXTERNAL_URL:
        return
    url = RENDER_EXTERNAL_URL.rstrip('/')
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url) as resp:
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
    asyncio.create_task(daily_reward_checker())

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

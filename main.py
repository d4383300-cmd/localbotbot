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
from aiogram.filters import CommandStart, CommandObject
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
active_jobs = {}
deputy_punishments = {}
event_user_messages = {}

# Активный ивент от модератора
active_event = None

# Активный авто-квест в чате: { "type": "slot"|"math", "reward": int, "answer": int|None, "message_id": int }
current_auto_quest = None

# Активные дуэли КНБ:
# { duel_id: { "p1_id": int, "p2_id": int, "bet": int, "status": "pending"|"playing", "p1_choice": str|None, "p2_choice": str|None, "expires_at": float, "msg_id": int } }
active_rps_duels = {}
duel_counter = 1

class Registration(StatesGroup):
    name = State()
    age = State()
    confirm = State()

def get_msk_now() -> datetime:
    return datetime.now(MSK_TZ)

def get_msk_today_str() -> str:
    return get_msk_now().strftime("%Y-%m-%d")

# Функция красивого отображения никнейма
def get_user_mention(user_dict: dict) -> str:
    uid = user_dict["user_id"]
    uname = user_dict.get("username")
    fname = user_dict.get("first_name") or "Игрок"
    if uname:
        return f"@{uname}"
    return f"<a href='tg://user?id={uid}'>{fname}</a>"

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

def can_moderate(user_id: int) -> bool:
    if user_id == LEYMIK_ID or is_admin(user_id):
        return True
    u = db.get_user(user_id)
    if (u.get("rank_level") or 0) >= 1 and u.get("is_frozen", 0) == 0:
        return True
    return False

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

# --- Защита от краша чата (Leymik защищен) ---
async def check_deputy_limits(user_id: int, user_mention: str) -> bool:
    if user_id == LEYMIK_ID or is_admin(user_id):
        return True

    u = db.get_user(user_id)
    if (u.get("rank_level") or 0) < 1:
        return True

    now = time.time()
    actions = deputy_punishments.get(user_id, [])
    actions = [t for t in actions if now - t < 300]
    actions.append(now)
    deputy_punishments[user_id] = actions

    if len(actions) >= 3:
        db.set_frozen(user_id, 1)
        deputy_punishments.pop(user_id, None)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="❄️ Разморозить", callback_data=f"deputy_unfreeze_{user_id}"),
                InlineKeyboardButton(text="🚫 Снять ранг", callback_data=f"deputy_demote_{user_id}")
            ]
        ])

        await bot.send_message(
            TARGET_CHAT_ID,
            f"🚨 <b>ТРЕВОГА АНТИ-КРАШ СИСТЕМЫ!</b> 🚨\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <b>Модератор {user_mention}</b> произвёл 3 наказания за 5 минут!\n\n"
            f"❄️ <b>Его ранг и права АВТОМАТИЧЕСКИ ЗАМОРОЖЕНЫ!</b>\n\n"
            f"👑 @Leymik, выберите действие:",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )
        return False
    return True

# --- АВТООДОБРЕНИЕ И ВЫХОД РЕФЕРАЛОВ ---
@dp.chat_join_request(F.chat.id == TARGET_CHAT_ID)
async def auto_approve_join(update: types.ChatJoinRequest):
    try:
        await update.approve()
        user = update.from_user
        u = db.get_user(user.id, user.username or "", user.first_name)
        mention = get_user_mention(u)

        await bot.send_message(
            TARGET_CHAT_ID,
            f"🌿 <b>Добро пожаловать в Localhaus, {mention}!</b>\n"
            f"Обязательно прочитай правила — напиши <b>\"правила\"</b>, а также найди себе друга для общения! ✨",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print(f"Ошибка автоодобрения заявки: {e}")

@dp.message(F.chat.id == TARGET_CHAT_ID, F.left_chat_member)
async def handle_left_chat_member(message: types.Message):
    left_user = message.left_chat_member
    u_info = db.get_user(left_user.id)
    ref_id = u_info.get("referrer_id")
    if ref_id:
        db.penalize_referrer(ref_id, rubles=10, atoms=5)
        try:
            await bot.send_message(
                ref_id,
                f"⚠️ <b>Штраф за выход реферала!</b>\n"
                f"Пользователь {get_user_mention(u_info)} покинул чат.\n"
                f"📉 Списано: <b>-10 рублей</b> и <b>-5 атомов (алмазов)</b>.",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

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
        u = db.get_user(member.id, member.username or "", member.first_name)
        mention = get_user_mention(u)
        await message.reply(
            f"🌿 <b>Добро пожаловать в Localhaus, {mention}!</b>\n"
            f"Обязательно прочитай правила — напиши <b>\"правила\"</b>, а также найди себе друга для общения! ✨",
            parse_mode=ParseMode.HTML
        )

# --- ГЛАВНОЕ МЕНЮ В ЛС: /start ---
@dp.message(F.chat.type == "private", CommandStart())
async def start_cmd(message: types.Message, command: CommandObject):
    user_id = message.from_user.id
    if db.is_blocked(user_id):
        return

    db.get_user(user_id, message.from_user.username or "", message.from_user.first_name)

    if command.args and command.args.startswith("ref_"):
        try:
            ref_id = int(command.args.split("_")[1])
            if ref_id != user_id:
                db.set_referrer(user_id, ref_id)
        except Exception:
            pass

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Кинуть заявку в чат", callback_data="menu_apply")],
        [InlineKeyboardButton(text="💸 Заработок", callback_data="menu_earn")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile")]
    ])

    await message.answer(
        f"👋 <b>Добро пожаловать в приёмную Localhaus!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Выбери нужный раздел с помощью кнопок ниже:\n\n"
        f"• <b>Кинуть заявку в чат</b> — заполнить анкету на вступление.\n"
        f"• <b>Заработок</b> — реферальная система (рубли и алмазы).\n"
        f"• <b>Профиль</b> — твой баланс и вывод.\n"
        f"━━━━━━━━━━━━━━━━━━━━━━",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )

@dp.callback_query(F.data == "menu_apply")
async def handle_menu_apply(cb: types.CallbackQuery, state: FSMContext):
    await state.set_state(Registration.name)
    await cb.message.answer(
        "📝 <b>Анкета кандидата в Localhaus:</b>\n"
        "1️⃣ <b>Как тебя зовут?</b>",
        parse_mode=ParseMode.HTML
    )
    await cb.answer()

@dp.callback_query(F.data == "menu_earn")
async def handle_menu_earn(cb: types.CallbackQuery):
    bot_me = await bot.get_me()
    ref_link = f"https://t.me/{bot_me.username}?start=ref_{cb.from_user.id}"

    text = (
        f"💸 <b>ПАРТНЁРСКАЯ ПРОГРАММА LOCALHAUS</b> 💸\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <b>Твоя персональная ссылка:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"📖 <b>Инструкция:</b>\n"
        f"1. Отправь ссылку друзьям.\n"
        f"2. Они подают заявку и заходят в чат.\n"
        f"3. При одобрении ты получаешь <b>10 рублей</b> и <b>5 алмазов 💎</b>!\n"
        f"⚠️ <b>Внимание:</b> если приглашённый выйдет из чата — штраф <b>-10 ₽</b> и <b>-5 алмазов</b>!\n\n"
        f"💳 <b>Вывод средств:</b> от <b>250 рублей</b>.\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    await cb.message.answer(text, parse_mode=ParseMode.HTML)
    await cb.answer()

@dp.callback_query(F.data == "menu_profile")
async def handle_menu_profile(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    u_data = db.get_user(user_id)
    ref_count = db.get_referrals_count(user_id)

    rubles = u_data.get("rubles", 0)
    rubles_total = u_data.get("rubles_total", 0)
    atoms = u_data.get("atoms", 0)

    text = (
        f"👤 <b>ЛИЧНЫЙ ПРОФИЛЬ ЗАРАБОТКА</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Доступно к выводу:</b> {rubles} ₽\n"
        f"💎 <b>Баланс алмазов (атомов):</b> {atoms} ⚛️\n"
        f"📊 <b>Заработано за всё время:</b> {rubles_total} ₽\n"
        f"👥 <b>Приглашено друзей:</b> {ref_count} чел.\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    kb_list = []
    if rubles >= 250:
        text += "🎉 <b>Порог вывода достигнут!</b> Нажмите кнопку ниже для связи с администратором:"
        kb_list.append([InlineKeyboardButton(text="💬 Написать @Leymik для вывода", url="https://t.me/Leymik")])
    else:
        rem = 250 - rubles
        text += f"ℹ️ До минимального вывода осталось: <b>{rem} ₽</b>."

    kb = InlineKeyboardMarkup(inline_keyboard=kb_list) if kb_list else None
    await cb.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await cb.answer()

# --- Шаги анкеты в ЛС ---
@dp.message(F.chat.type == "private", Registration.name)
async def process_name(message: types.Message, state: FSMContext):
    if db.is_blocked(message.from_user.id): return
    await state.update_data(name=message.text)
    await state.set_state(Registration.age)
    await message.answer("2️⃣ <b>Сколько тебе лет?</b> (Укажи реальный возраст):", parse_mode=ParseMode.HTML)

@dp.message(F.chat.type == "private", Registration.age)
async def process_age(message: types.Message, state: FSMContext):
    if db.is_blocked(message.from_user.id): return
    try:
        age = int(message.text)
        if age < 10 or age > 99: raise ValueError
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
    u = db.get_user(user.id, user.username or "", user.first_name)
    app_id = db.create_application(user.id, user.username or "", data["name"], data["age"])

    await cb.message.edit_text("✅ <b>Твоя заявка отправлена администрации! Ожидай решения.</b>", parse_mode=ParseMode.HTML)
    await cb.answer()

    mention = get_user_mention(u)
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
        f"👤 <b>Кандидат:</b> {mention} (ID: <code>{user.id}</code>)\n"
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

        u_info = db.get_user(target_id)
        if u_info.get("referrer_id"):
            db.reward_referrer(u_info["referrer_id"], rubles=10, atoms=5)
            try:
                await bot.send_message(
                    u_info["referrer_id"],
                    f"🎉 <b>Ваш приглашенный друг был принят в Localhaus!</b>\n"
                    f"💰 Вам начислено <b>+10 рублей</b> и <b>+5 алмазов 💎</b>!",
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

# --- Разморозка Суженого через кнопки ---
@dp.callback_query(F.data.startswith("deputy_"))
async def process_deputy_freeze_cb(cb: types.CallbackQuery):
    if cb.from_user.id != LEYMIK_ID:
        return await cb.answer("⛔ Только @Leymik принимает решения по модераторам!", show_alert=True)

    parts = cb.data.split("_")
    sub_action = parts[1]
    target_id = int(parts[2])
    target = db.get_user(target_id)
    t_mention = get_user_mention(target)

    if sub_action == "unfreeze":
        db.set_frozen(target_id, 0)
        deputy_punishments.pop(target_id, None)
        await cb.message.edit_text(
            f"☀️ <b>РАЗМОРОЗКА!</b>\n\n"
            f"👑 @Leymik снял заморозку с <b>{t_mention}</b>.\n"
            f"Полномочия восстановлены.",
            parse_mode=ParseMode.HTML
        )
    elif sub_action == "demote":
        db.demote_rank(target_id)
        deputy_punishments.pop(target_id, None)
        await cb.message.edit_text(
            f"🚫 <b>РАНГ АННУЛИРОВАН!</b>\n\n"
            f"👑 @Leymik снял звание с <b>{t_mention}</b>.",
            parse_mode=ParseMode.HTML
        )
    await cb.answer()

# --- ЗАВЕРШЕНИЕ РОЗЫГРЫША ---
async def execute_finish_giveaway(gid: int):
    g_info = db.get_giveaway_by_id(gid)
    if not g_info or g_info.get("is_active") == 0:
        return False, "Розыгрыш уже завершен или не существует."

    participants = db.get_giveaway_participants(gid)
    db.finish_giveaway(gid)

    try:
        await bot.unpin_chat_message(TARGET_CHAT_ID, g_info["message_id"])
    except Exception:
        pass

    curr_icon = "⚛️ алмазов (атомов)" if g_info["currency"] == "атомы" else "🍁 листочек"

    if not participants:
        await bot.send_message(
            TARGET_CHAT_ID,
            f"🏁 <b>РОЗЫГРЫШ #{gid} ЗАВЕРШЁН</b> 🏁\n\n"
            f"К сожалению, никто не принял участие в раздаче <b>{g_info['amount']} {curr_icon}</b>.",
            parse_mode=ParseMode.HTML
        )
    else:
        winner_id = random.choice(participants)
        winner = db.get_user(winner_id)
        w_mention = get_user_mention(winner)
        if g_info["currency"] == "атомы":
            db.update_atoms(winner_id, g_info["amount"])
        else:
            db.update_balance(winner_id, g_info["amount"])

        res_msg = await bot.send_message(
            TARGET_CHAT_ID,
            f"🎊 <b>ИТОГИ РОЗЫГРЫША #{gid}!</b> 🎊\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎁 <b>Приз:</b> <b>{g_info['amount']} {curr_icon}</b>\n"
            f"📝 <b>Описание:</b> {g_info['description']}\n\n"
            f"👑 <b>Счастливый победитель:</b>\n"
            f"👉 <b>{w_mention}</b>\n\n"
            f"💰 Приз успешно зачислен на баланс! Поздравляем! 🎉\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )
        try:
            await bot.pin_chat_message(TARGET_CHAT_ID, res_msg.message_id)
        except Exception:
            pass

    return True, "Успешно завершено."

@dp.callback_query(F.data.startswith("giveaway_join_"))
async def handle_giveaway_join(cb: types.CallbackQuery):
    gid = int(cb.data.split("_")[2])
    user_id = cb.from_user.id
    db.get_user(user_id, cb.from_user.username or "", cb.from_user.first_name)

    success = db.add_giveaway_participant(gid, user_id)
    if not success:
        return await cb.answer("⚠️ Вы уже участвуете в этом розыгрыше!", show_alert=True)

    count = db.get_giveaway_participants_count(gid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎉 Принять участие ({count})", callback_data=f"giveaway_join_{gid}")]
    ])
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass
    await cb.answer("✅ Вы успешно зарегистрированы в розыгрыше!")

@dp.callback_query(F.data.startswith("giveaway_end_"))
async def handle_giveaway_end(cb: types.CallbackQuery):
    if cb.from_user.id != LEYMIK_ID:
        return await cb.answer("⛔ Только @Leymik может завершить розыгрыш!", show_alert=True)

    gid = int(cb.data.split("_")[2])
    ok, text = await execute_finish_giveaway(gid)
    if ok:
        await cb.message.edit_text(f"✅ Розыгрыш #{gid} успешно завершен! Итоги закреплены в чате.")
    else:
        await cb.message.edit_text(f"⚠️ {text}")
    await cb.answer()

# --- КНОПКИ БРАКА ---
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
    p_mention = get_user_mention(proposer)
    t_mention = get_user_mention(target)

    if action == "yes":
        db.create_marriage(proposer_id, p_mention, target_id, t_mention)
        await cb.message.edit_text(
            f"✨ <b>СВЯЩЕННЫЙ СОЮЗ ЗАКЛЮЧЁН!</b> ✨\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💍 <b>{p_mention}</b> и <b>{t_mention}</b> теперь официально в браке!\n\n"
            f"🎉 Поздравляем молодожёнов! Горько! 🥂💫\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )
    else:
        await cb.message.edit_text(
            f"💔 <b>ОТКАЗ В ПРЕДЛОЖЕНИИ...</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🥀 <b>{t_mention}</b> отклонил(а) предложение <b>{p_mention}</b>.\n"
            f"Сердце разбито... Но жизнь продолжается! 🌧️\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )
    await cb.answer()

# --- МЕХАНИКА ДУЭЛИ В КНБ НА 2 МИНУТЫ + 1 МИНУТУ НА ВЫБОР ---
@dp.callback_query(F.data.startswith("duel_accept_"))
async def handle_duel_accept(cb: types.CallbackQuery):
    duel_id = int(cb.data.split("_")[2])
    duel = active_rps_duels.get(duel_id)
    if not duel:
        return await cb.answer("Дуэль устарела или уже завершена.")

    if cb.from_user.id != duel["p2_id"]:
        return await cb.answer("🎯 Это не твой вызов!", show_alert=True)

    if time.time() > duel["expires_at"]:
        active_rps_duels.pop(duel_id, None)
        await cb.message.edit_text("⏳ Время ожидания ответа на дуэль (2 минуты) истекло!")
        return await cb.answer("Время вышло!")

    p1 = db.get_user(duel["p1_id"])
    p2 = db.get_user(duel["p2_id"])
    bet = duel["bet"]

    if p1["balance"] < bet or p2["balance"] < bet:
        active_rps_duels.pop(duel_id, None)
        return await cb.message.edit_text("❌ У одного из участников недостаточно листочек для дуэли!")

    duel["status"] = "playing"
    duel["choice_expires"] = time.time() + 60 # 1 минута на выбор

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🪨 Камень", callback_data=f"rps_pick_{duel_id}_камень"),
            InlineKeyboardButton(text="✂️ Ножницы", callback_data=f"rps_pick_{duel_id}_ножницы"),
            InlineKeyboardButton(text="📄 Бумага", callback_data=f"rps_pick_{duel_id}_бумага")
        ]
    ])

    p1_m = get_user_mention(p1)
    p2_m = get_user_mention(p2)

    await cb.message.edit_text(
        f"⚔️ <b>ДУЭЛЬ КНБ НАЧАЛАСЬ!</b> ⚔️\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Участники: <b>{p1_m}</b> vs <b>{p2_m}</b>\n"
        f"💰 Ставка: <b>{bet} 🍁 листочек</b> (Банк: {bet*2} 🍁)\n\n"
        f"👇 <b>Сделайте ваш скрытый выбор ниже!</b>\n"
        f"⏳ У вас есть ровно <b>1 минута</b> на раздумие, иначе — автопоражение!",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )
    await cb.answer("Вызов принят! Делайте выбор.")

    # Таймер на 1 минуту на раздумие
    asyncio.create_task(duel_timeout_watcher(duel_id, cb.message.message_id))

async def duel_timeout_watcher(duel_id: int, message_id: int):
    await asyncio.sleep(60)
    duel = active_rps_duels.get(duel_id)
    if not duel or duel.get("status") != "playing":
        return

    # Если кто-то не успел сделать выбор
    p1_pick = duel["p1_choice"]
    p2_pick = duel["p2_choice"]
    bet = duel["bet"]
    p1 = db.get_user(duel["p1_id"])
    p2 = db.get_user(duel["p2_id"])
    p1_m = get_user_mention(p1)
    p2_m = get_user_mention(p2)

    if not p1_pick and not p2_pick:
        active_rps_duels.pop(duel_id, None)
        try:
            await bot.edit_message_text(
                "⏳ <b>Никто не сделал выбор за 1 минуту!</b> Дуэль аннулирована, ставки возвращены.",
                chat_id=TARGET_CHAT_ID,
                message_id=message_id,
                parse_mode=ParseMode.HTML
            )
        except Exception: pass
        return

    if not p1_pick:
        # P1 не выбрал -> P2 победил
        db.update_balance(duel["p2_id"], bet)
        db.update_balance(duel["p1_id"], -bet)
        active_rps_duels.pop(duel_id, None)
        try:
            await bot.edit_message_text(
                f"⏳ <b>Время на выбор вышло!</b>\n{p1_m} не сделал выбор за минуту и получает автопоражение!\n\n"
                f"👑 <b>Победитель:</b> {p2_m} (+{bet} 🍁)!",
                chat_id=TARGET_CHAT_ID,
                message_id=message_id,
                parse_mode=ParseMode.HTML
            )
        except Exception: pass
        return

    if not p2_pick:
        # P2 не выбрал -> P1 победил
        db.update_balance(duel["p1_id"], bet)
        db.update_balance(duel["p2_id"], -bet)
        active_rps_duels.pop(duel_id, None)
        try:
            await bot.edit_message_text(
                f"⏳ <b>Время на выбор вышло!</b>\n{p2_m} не сделал выбор за минуту и получает автопоражение!\n\n"
                f"👑 <b>Победитель:</b> {p1_m} (+{bet} 🍁)!",
                chat_id=TARGET_CHAT_ID,
                message_id=message_id,
                parse_mode=ParseMode.HTML
            )
        except Exception: pass
        return

@dp.callback_query(F.data.startswith("rps_pick_"))
async def handle_rps_pick(cb: types.CallbackQuery):
    parts = cb.data.split("_")
    duel_id = int(parts[2])
    choice = parts[3]
    duel = active_rps_duels.get(duel_id)

    if not duel or duel.get("status") != "playing":
        return await cb.answer("Дуэль завершена или не активна.")

    u_id = cb.from_user.id
    if u_id not in [duel["p1_id"], duel["p2_id"]]:
        return await cb.answer("Ты не участвуешь в этой дуэли!", show_alert=True)

    if u_id == duel["p1_id"]:
        if duel["p1_choice"]:
            return await cb.answer("Вы уже сделали выбор!", show_alert=True)
        duel["p1_choice"] = choice
    elif u_id == duel["p2_id"]:
        if duel["p2_choice"]:
            return await cb.answer("Вы уже сделали выбор!", show_alert=True)
        duel["p2_choice"] = choice

    await cb.answer(f"Твой скрытый выбор: {choice.upper()} принят!")

    # Если оба сделали выбор - подводим итоги
    if duel["p1_choice"] and duel["p2_choice"]:
        active_rps_duels.pop(duel_id, None)
        p1 = db.get_user(duel["p1_id"])
        p2 = db.get_user(duel["p2_id"])
        p1_m = get_user_mention(p1)
        p2_m = get_user_mention(p2)
        bet = duel["bet"]

        c1 = duel["p1_choice"]
        c2 = duel["p2_choice"]
        icons = {"камень": "🪨 КАМЕНЬ", "ножницы": "✂️ НОЖНИЦЫ", "бумага": "📄 БУМАГА"}

        if c1 == c2:
            return await cb.message.edit_text(
                f"🤝 <b>НИЧЬЯ В ДУЭЛИ КНБ!</b> 🤝\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{p1_m} выбрал: <b>{icons[c1]}</b>\n"
                f"{p2_m} выбрал: <b>{icons[c2]}</b>\n\n"
                f"Выборы совпали! Ставки <b>{bet} 🍁 листочек</b> возвращены обоим игрокам!",
                parse_mode=ParseMode.HTML
            )

        win1 = (c1 == "камень" and c2 == "ножницы") or \
               (c1 == "ножницы" and c2 == "бумага") or \
               (c1 == "бумага" and c2 == "камень")

        if win1:
            winner_m = p1_m
            loser_m = p2_m
            db.update_balance(duel["p1_id"], bet)
            db.update_balance(duel["p2_id"], -bet)
        else:
            winner_m = p2_m
            loser_m = p1_m
            db.update_balance(duel["p2_id"], bet)
            db.update_balance(duel["p1_id"], -bet)

        await cb.message.edit_text(
            f"🏆 <b>РЕЗУЛЬТАТ ДУЭЛИ КНБ!</b> 🏆\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{p1_m} выбрал: <b>{icons[c1]}</b>\n"
            f"{p2_m} выбрал: <b>{icons[c2]}</b>\n\n"
            f"👑 <b>Победитель:</b> {winner_m} забирает куш <b>+{bet*2} 🍁 листочек</b>!\n"
            f"💀 <b>Проигравший:</b> {loser_m} теряет свою ставку.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )

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
    name = get_user_mention(user)

    text_map = {
        "smoke": f"🚬 <b>{name}</b> медленно достаёт сигарету, чиркает зажигалкой и выпускает густой клуб дыма в потолок... 💨",
        "snus": f"🌿 <b>{name}</b> со смаком закидывает плотный снюс под губу и довольно закатывает глаза... 🤤✨",
        "drink": f"🥃 <b>{name}</b> наливает себе крепкий напиток и залпом осушает бокал до дна! За ваше здоровье! 🍻"
    }

    await cb.message.edit_text(
        f"{text_map.get(action_type)}\n\n<i>Оплата: -30 🍁 листочек (Остаток: {new_bal} 🍁)</i>",
        parse_mode=ParseMode.HTML
    )
    await cb.answer()

# --- ОБРАБОТЧИК СООБЩЕНИЙ ЧАТА ---
@dp.message(F.chat.id == TARGET_CHAT_ID)
async def handle_chat_message(message: types.Message):
    global last_admin_fetch, active_event, current_auto_quest, duel_counter
    user_id = message.from_user.id
    text = message.text or message.caption or ""
    lower_text = text.lower().strip()

    # Синхронизируем пользователя в БД
    sender_data = db.get_user(user_id, message.from_user.username or "", message.from_user.first_name)
    sender_mention = get_user_mention(sender_data)

    msk_today = get_msk_today_str()
    db.record_message(user_id, msk_today)

    if message.from_user.username:
        db.save_member(user_id, message.from_user.username)

    if time.time() - last_admin_fetch > 600:
        await refresh_admin_cache()

    user_admin = is_admin(user_id)
    user_can_mod = can_moderate(user_id)

    # --- ОБРАБОТКА ДАЙСОВ (ФУТБОЛ, БАСКЕТБОЛ, КАЗИНО 777) ---
    if message.dice:
        is_forwarded = bool(
            message.forward_date or message.forward_from or 
            message.forward_from_chat or getattr(message, 'forward_origin', None)
        )
        if is_forwarded: return # Дюпы заблокированы

        # 1. АВТО-КВЕСТ 777
        if current_auto_quest and current_auto_quest.get("type") == "slot":
            if message.dice.emoji == "🎰" and message.dice.value == 64:
                reward_atoms = current_auto_quest["reward"]
                try:
                    await bot.unpin_chat_message(TARGET_CHAT_ID, current_auto_quest["message_id"])
                except Exception:
                    pass
                current_auto_quest = None
                new_at = db.update_atoms(user_id, reward_atoms)
                return await message.reply(
                    f"🎉 <b>АВТО-КВЕСТ ВЫПОЛНЕН!</b> 🎉\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎰 {sender_mention} первым выбил 777!\n"
                    f"💎 Награда: <b>+{reward_atoms} алмазов (атомов)</b> на баланс!\n"
                    f"Твой баланс: <b>{new_at}</b> ⚛️\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━",
                    parse_mode=ParseMode.HTML
                )

        # 2. ИВЕНТ ФУТБОЛ / БАСКЕТБОЛ (ЧЕТКАЯ ПРОВЕРКА)
        if active_event and active_event.get("active"):
            req_emoji = "⚽" if active_event["type"] == "football" else "🏀"
            if message.dice.emoji == req_emoji:
                now = time.time()
                e_history = event_user_messages.get(user_id, [])
                e_history = [t for t in e_history if now - t < 5]
                e_history.append(now)
                event_user_messages[user_id] = e_history

                if len(e_history) > 5:
                    event_user_messages.pop(user_id, None)
                    try:
                        await message.delete()
                        until = datetime.utcnow() + timedelta(minutes=1)
                        await bot.restrict_chat_member(
                            TARGET_CHAT_ID,
                            user_id,
                            permissions=ChatPermissions(can_send_messages=False),
                            until_date=until
                        )
                        return await message.answer(
                            f"🔇 {sender_mention} получает мут на 1 минуту за спам в ивенте (>5 бросков за 5 сек)!",
                            parse_mode=ParseMode.HTML
                        )
                    except Exception: pass

                # ТОЧНАЯ ПРОВЕРКА ГОЛА:
                # В футболе ⚽: значения 3, 4, 5 — ГОЛ!
                # В баскетболе 🏀: значения 4, 5 — ПОПАДАНИЕ!
                is_goal = False
                val = message.dice.value
                if req_emoji == "⚽" and val in [3, 4, 5]:
                    is_goal = True
                elif req_emoji == "🏀" and val in [4, 5]:
                    is_goal = True

                if is_goal:
                    current_goals = active_event["scores"].get(user_id, 0) + 1
                    active_event["scores"][user_id] = current_goals
                    remaining = active_event["target"] - current_goals

                    if remaining <= 0:
                        reward = active_event["reward"]
                        new_bal = db.update_balance(user_id, reward)
                        active_event["active"] = False
                        try:
                            await bot.unpin_chat_message(TARGET_CHAT_ID, active_event["message_id"])
                        except Exception: pass
                        active_event = None
                        return await message.reply(
                            f"🏆 <b>ИВЕНТ ЗАВЕРШЁН! ПОБЕДИТЕЛЬ ОПРЕДЕЛЁН!</b> 🏆\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎉 <b>{sender_mention}</b> первым забил необходимое количество голов!\n"
                            f"💰 <b>Награда: +{reward} 🍁 листочек</b> зачислена на баланс!\n"
                            f"🍃 Баланс победителя: <b>{new_bal}</b> 🍁\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━",
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        target_text = "в ворота" if req_emoji == "⚽" else "в кольцо"
                        return await message.reply(
                            f"🎯 <b>ТОЧНЫЙ ГОЛ!</b> ({current_goals}/{active_event['target']})\n"
                            f"👤 Игрок: <b>{sender_mention}</b>\n"
                            f"⚽ Осталось забить {target_text}: <b>{remaining}</b> раз(а)!",
                            parse_mode=ParseMode.HTML
                        )
                return

        # 3. КАЗИНО 777
        if message.dice.emoji == "🎰":
            dice_val = message.dice.value
            if dice_val == 64:
                new_bal = db.update_balance(user_id, 100)
                return await message.reply(
                    f"🎰🔥 <b>ДЖЕКПОТ! ТРИ СЕМЁРКИ (777)!</b> 🔥🎰\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎉 {sender_mention} выбивает золотые три семерки!\n"
                    f"💰 <b>Выигрыш: +100</b> 🍁 листочек!\n"
                    f"🍃 Текущий кошелек: <b>{new_bal}</b> 🍁\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━",
                    parse_mode=ParseMode.HTML
                )
            elif dice_val in [1, 22, 43]:
                new_bal = db.update_balance(user_id, 30)
                return await message.reply(
                    f"🎰✨ <b>ТРИ В РЯД!</b> ✨🎰\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎯 Линия совпала! {sender_mention} забирает выигрыш за комбинацию!\n"
                    f"💰 <b>Выигрыш: +30</b> 🍁 листочек!\n"
                    f"🍃 Текущий кошелек: <b>{new_bal}</b> 🍁\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━",
                    parse_mode=ParseMode.HTML
                )
            return

    # --- ПРОВЕРКА ОТВЕТА НА АВТО-КВЕСТ С ПРИМЕРОМ (ЖЕЛЕЗОБЕТОННЫЙ ПАРСИНГ) ---
    if current_auto_quest and current_auto_quest.get("type") == "math":
        # Проверяем, ответил ли человек на сообщение квеста ИЛИ просто написал правильное число
        is_reply = message.reply_to_message and message.reply_to_message.message_id == current_auto_quest["message_id"]
        
        # Вытаскиваем число из текста
        numbers_in_msg = re.findall(r'-?\d+', text.strip())
        if numbers_in_msg and len(numbers_in_msg) == 1:
            parsed_num = int(numbers_in_msg[0])
            if (is_reply or text.strip() == str(current_auto_quest["answer"])) and parsed_num == current_auto_quest["answer"]:
                reward_atoms = current_auto_quest["reward"]
                try:
                    await bot.unpin_chat_message(TARGET_CHAT_ID, current_auto_quest["message_id"])
                except Exception: pass
                current_auto_quest = None
                new_at = db.update_atoms(user_id, reward_atoms)
                return await message.reply(
                    f"🧠 <b>ПРИМЕР РЕШЁН ПЕРВЫМ!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Победитель: {sender_mention}\n"
                    f"💎 Награда: <b>+{reward_atoms} алмазов (атомов)</b>!\n"
                    f"Твой баланс: <b>{new_at}</b> ⚛️\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━",
                    parse_mode=ParseMode.HTML
                )

    # --- ПРОВЕРКА ОТВЕТА НА РАБОТУ ---
    if message.reply_to_message and message.reply_to_message.message_id in active_jobs:
        job = active_jobs[message.reply_to_message.message_id]
        if job["user_id"] != user_id:
            return await message.reply("⚠️ Это задание решает другой работник!")

        if time.time() > job["expires_at"]:
            del active_jobs[message.reply_to_message.message_id]
            return await message.reply("⏳ Время на решение (2 минуты) вышло! Пример аннулирован.")

        raw_nums = re.findall(r'-?\d+', text.strip())
        if not raw_nums:
            return await message.reply("⚠️ Ответ должен содержать число!", parse_mode=ParseMode.HTML)

        user_ans = int(raw_nums[0])
        correct_ans = job["answer"]
        del active_jobs[message.reply_to_message.message_id]

        if user_ans == correct_ans:
            new_bal = db.update_balance(user_id, 3)
            return await message.reply(
                f"✅ <b>ВЕРНО! ПРИМЕР РЕШЁН!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 Ответ: <b>{correct_ans}</b>\n"
                f"💼 Зарплата: <b>+3</b> 🍁 листочка зачислено!\n"
                f"💰 Баланс: <b>{new_bal}</b> 🍁",
                parse_mode=ParseMode.HTML
            )
        else:
            new_bal = db.update_balance(user_id, -40)
            return await message.reply(
                f"❌ <b>ОШИБКА В ВЫЧИСЛЕНИЯХ!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Твой ответ: <code>{user_ans}</code> | Правильный: <b>{correct_ans}</b>\n\n"
                f"📉 Штраф: <b>-40</b> 🍁 листочек!\n"
                f"💰 Баланс: <b>{new_bal}</b> 🍁",
                parse_mode=ParseMode.HTML
            )

    if lower_text == "бот ты тут?":
        return await message.reply("Да")

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

    # --- ЗАЩИТА ОТ СПАМА СТИКЕРАМИ ---
    if message.sticker and not user_can_mod:
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
                f"⚠️ {sender_mention}, прошу пожалуйста не нарушать правила чата!\n"
                f"Чтобы узнать правила чата напишите <b>\"правила\"</b>.",
                parse_mode=ParseMode.HTML
            )

    # --- ЗАЩИТА ОТ СПАМА ЛЕСЕНКОЙ ---
    if not user_can_mod:
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
                    f"🔇 {sender_mention} получает мут на 5 минут за спам лесенкой!",
                    parse_mode=ParseMode.HTML
                )
            except Exception: pass

    # --- АНТИ-ССЫЛКИ ---
    url_pattern = r"(https?://\S+|t\.me/\S+|www\.\S+|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b)"
    if re.search(url_pattern, text) and not user_can_mod:
        try: await message.delete()
        except Exception: pass

        count = db.check_and_increment_links(user_id)
        if count >= 2:
            try:
                await bot.ban_chat_member(TARGET_CHAT_ID, user_id)
                return await message.answer(
                    f"🚫 {sender_mention} заблокирован за повторную рекламу.",
                    parse_mode=ParseMode.HTML
                )
            except Exception: pass
        else:
            return await message.answer(
                f"⚠️ {sender_mention}, ссылки запрещены! (Предупреждение 1/2 за день)",
                parse_mode=ParseMode.HTML
            )

    # --- КОМАНДА ДЛЯ ВИЛОЧНИКА: "ВИЛКИ" ---
    if lower_text == "вилки":
        rank = sender_data.get("rank_level") or 0
        if rank >= 2 or user_id == LEYMIK_ID:
            return await message.reply("Вилки")

    # --- 👑 КОМАНДЫ ТОЛЬКО ДЛЯ СОЗДАТЕЛЯ @LEYMIK ---
    if user_id == LEYMIK_ID:
        # ЗАВЕРШЕНИЕ РОЗЫГРЫША ТЕКСТОМ ПО РЕПЛАЮ "завершить"
        if lower_text == "завершить" and message.reply_to_message:
            rep_msg_id = message.reply_to_message.message_id
            g = db.get_giveaway_by_msg(rep_msg_id)
            if g:
                ok, res_text = await execute_finish_giveaway(g["id"])
                return await message.reply(f"✅ {res_text}" if ok else f"⚠️ {res_text}")

        # ИВЕНТ: ивент (выигрыш) (баскетбол/футбол) (цель)
        event_match = re.match(r"^ивент\s+(\d+)\s+(баскетбол|футбол)\s+(\d+)$", lower_text)
        if event_match:
            reward = int(event_match.group(1))
            e_type = event_match.group(2)
            target_score = int(event_match.group(3))

            emoji = "⚽" if e_type == "футбол" else "🏀"
            action_desc = "в ворота" if e_type == "футбол" else "в кольцо"

            event_msg = await message.answer(
                f"🔥 <b>СТАРТОВАЛ ИВЕНТ: {e_type.upper()}!</b> 🔥\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏆 <b>Приз победителю:</b> <b>{reward} 🍁 листочек</b>\n"
                f"🎯 <b>Цель:</b> первым забить <b>{target_score}</b> раз(а) {action_desc}!\n\n"
                f"📖 <b>Инструкция:</b>\n"
                f"Отправляйте в чат эмодзи {emoji} без текста и без пересылок!\n"
                f"⚠️ Не более 5 сообщений за 5 секунд, иначе мут 1 мин!\n"
                f"━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML
            )
            try:
                await bot.pin_chat_message(TARGET_CHAT_ID, event_msg.message_id)
            except Exception: pass

            active_event = {
                "active": True,
                "reward": reward,
                "type": e_type,
                "target": target_score,
                "scores": {},
                "message_id": event_msg.message_id
            }
            return

        # РАЗДАЧА: раздача (число) (атомы/листочки) (описание)
        giveaway_match = re.match(r"^раздача\s+(\d+)\s+(атомы|листочки)\s+(.+)$", lower_text)
        if giveaway_match:
            amount = int(giveaway_match.group(1))
            currency = giveaway_match.group(2)
            desc = giveaway_match.group(3)
            curr_icon = "⚛️ алмазов (атомов)" if currency == "атомы" else "🍁 листочек"

            kb_group = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎉 Принять участие (0)", callback_data="giveaway_join_temp")]
            ])

            post = await message.answer(
                f"🎁 <b>НАЧАЛАСЬ РАЗДАЧА: {amount} {curr_icon.upper()}!</b> 🎁\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📝 <b>Описание:</b> {desc}\n\n"
                f"👇 Нажмите кнопку ниже для участия!\n"
                f"━━━━━━━━━━━━━━━━━━━━━━",
                reply_markup=kb_group,
                parse_mode=ParseMode.HTML
            )
            gid = db.create_giveaway(amount, currency, desc, post.message_id)
            
            kb_group_updated = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎉 Принять участие (0)", callback_data=f"giveaway_join_{gid}")]
            ])
            await post.edit_reply_markup(reply_markup=kb_group_updated)

            try:
                await bot.pin_chat_message(TARGET_CHAT_ID, post.message_id)
            except Exception: pass

            kb_admin = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏁 Закончить розыгрыш", callback_data=f"giveaway_end_{gid}")]
            ])
            try:
                await bot.send_message(
                    LEYMIK_ID,
                    f"📢 <b>Розыгрыш #{gid} запущен!</b>\n"
                    f"Приз: <b>{amount} {curr_icon}</b>\n\n"
                    f"Завершить можно кнопкой ниже или словом <code>завершить</code> в ответ на розыгрыш в чате!",
                    reply_markup=kb_admin,
                    parse_mode=ParseMode.HTML
                )
            except Exception: pass
            return

        # РАЗМОРОЗИТЬ
        if lower_text == "разморозить":
            if not message.reply_to_message:
                return await message.reply("⚠️ Ответь этой командой на сообщение модератора!")
            target = message.reply_to_message.from_user
            db.set_frozen(target.id, 0)
            deputy_punishments.pop(target.id, None)
            t_u = db.get_user(target.id)
            return await message.reply(
                f"☀️ <b>РАЗМОРОЗКА!</b>\n"
                f"👑 @Leymik разморозил полномочия {get_user_mention(t_u)}!",
                parse_mode=ParseMode.HTML
            )

        # ПОВЫСИТЬ
        if lower_text == "повысить":
            if not message.reply_to_message:
                return await message.reply("⚠️ Ответь этой командой на сообщение того, кого хочешь повысить!")
            target = message.reply_to_message.from_user
            if target.id == LEYMIK_ID or target.id == bot.id:
                return await message.reply("😅 Этого пользователя нельзя повысить.")

            new_rank = db.promote_rank(target.id)
            t_u = db.get_user(target.id)
            rank_name = "«Суженый Служенный»" if new_rank == 1 else "«Вилочник» 🍴"
            desc_extra = "\n🍴 <i>Доступна эксклюзивная команда \"вилки\"!</i>" if new_rank == 2 else ""

            return await message.reply(
                f"⚜️ <b>ПОВЫШЕНИЕ В РАНГЕ!</b> ⚜️\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👑 @Leymik повысил {get_user_mention(t_u)}!\n"
                f"🎖 <b>Новый статус:</b> <b>{rank_name}</b>\n\n"
                f"🛡 <b>Полномочия:</b> Бан, Кик, Мут, Размут.{desc_extra}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML
            )

        # ВЫДАТЬ АТОМЫ (АЛМАЗЫ)
        give_atoms_match = re.match(r"^выдать\s+атомы\s+(\d+)$", lower_text)
        if give_atoms_match and message.reply_to_message:
            target = message.reply_to_message.from_user
            amount = int(give_atoms_match.group(1))
            t_data = db.get_user(target.id, target.username or "", target.first_name)
            new_at = db.update_atoms(target.id, amount)
            return await message.reply(
                f"💎 <b>ВЫДАЧА АЛМАЗОВ (АТОМОВ)</b>\n"
                f"👤 Получатель: <b>{get_user_mention(t_data)}</b>\n"
                f"📈 Начислено: <b>+{amount}</b> ⚛️\n"
                f"💰 Текущий баланс: <b>{new_at}</b> ⚛️",
                parse_mode=ParseMode.HTML
            )

        # СНЯТЬ АТОМЫ
        remove_atoms_match = re.match(r"^снять\s+атомы\s+(\d+)$", lower_text)
        if remove_atoms_match and message.reply_to_message:
            target = message.reply_to_message.from_user
            amount = int(remove_atoms_match.group(1))
            t_data = db.get_user(target.id, target.username or "", target.first_name)
            actual_remove = min(amount, t_data.get("atoms", 0))
            new_at = db.update_atoms(target.id, -actual_remove)
            return await message.reply(
                f"⚖️ <b>ИЗЪЯТИЕ АЛМАЗОВ (АТОМОВ)</b>\n"
                f"👤 Участник: <b>{get_user_mention(t_data)}</b>\n"
                f"📉 Списано: <b>-{actual_remove}</b> ⚛️\n"
                f"💰 Текущий баланс: <b>{new_at}</b> ⚛️",
                parse_mode=ParseMode.HTML
            )

        # СНЯТЬ БАЛАНС ЛИСТОЧЕК
        remove_match = re.match(r"^снять\s+баланс\s+(\d+)$", lower_text)
        if remove_match and message.reply_to_message:
            target = message.reply_to_message.from_user
            amount = int(remove_match.group(1))
            t_data = db.get_user(target.id, target.username or "", target.first_name)
            actual_remove = min(amount, t_data["balance"])
            new_bal = db.update_balance(target.id, -actual_remove)
            return await message.reply(
                f"⚖️ <b>ИЗЪЯТИЕ ЛИСТОЧЕК</b>\n"
                f"👤 Участник: <b>{get_user_mention(t_data)}</b>\n"
                f"📉 Списано: <b>-{actual_remove}</b> 🍁\n"
                f"💰 Текущий баланс: <b>{new_bal}</b> 🍁",
                parse_mode=ParseMode.HTML
            )

        # ВЫДАТЬ БАЛАНС ЛИСТОЧЕК
        give_match = re.match(r"^выдать\s+баланс\s+(\d+)$", lower_text)
        if give_match and message.reply_to_message:
            target = message.reply_to_message.from_user
            amount = int(give_match.group(1))
            t_data = db.get_user(target.id, target.username or "", target.first_name)
            new_bal = db.update_balance(target.id, amount)
            return await message.reply(
                f"🎁 <b>ВЫДАЧА ЛИСТОЧЕК</b>\n"
                f"👤 Получатель: <b>{get_user_mention(t_data)}</b>\n"
                f"📈 Начислено: <b>+{amount}</b> 🍁\n"
                f"💰 Текущий баланс: <b>{new_bal}</b> 🍁",
                parse_mode=ParseMode.HTML
            )

    # Проверка замороженного модератора
    if sender_data.get("is_frozen", 0) == 1 and (lower_text in ["бан", "кик", "размут"] or lower_text.startswith("мут")):
        return await message.reply("🧊 <b>Твой статус модератора заморожен!</b> Дождись разморозки от @Leymik.", parse_mode=ParseMode.HTML)

    # --- КОМАНДЫ МОДЕРАЦИИ ---
    if user_can_mod and message.reply_to_message:
        target = message.reply_to_message.from_user

        if lower_text == "бан":
            if target.id == LEYMIK_ID or is_admin(target.id):
                return await message.reply("❌ Нельзя применить действие к руководству!")

            passed = await check_deputy_limits(user_id, sender_mention)
            if not passed: return

            try:
                await bot.ban_chat_member(TARGET_CHAT_ID, target.id)
                t_u = db.get_user(target.id)
                await message.reply(f"🚫 Модератор исключил и забанил {get_user_mention(t_u)}.", parse_mode=ParseMode.HTML)
            except Exception:
                await message.reply("⚠️ Ошибка выполнения бана.")
            return

        elif lower_text == "кик":
            if target.id == LEYMIK_ID or is_admin(target.id):
                return await message.reply("❌ Нельзя кикнуть руководство!")

            passed = await check_deputy_limits(user_id, sender_mention)
            if not passed: return

            try:
                await bot.ban_chat_member(TARGET_CHAT_ID, target.id)
                await bot.unban_chat_member(TARGET_CHAT_ID, target.id)
                t_u = db.get_user(target.id)
                await message.reply(f"🚪 {get_user_mention(t_u)} был исключен из чата.", parse_mode=ParseMode.HTML)
            except Exception:
                await message.reply("⚠️ Ошибка при исключении.")
            return

        elif lower_text.startswith("мут"):
            mute_match = re.match(r"^мут\s+(\d+)$", lower_text)
            if mute_match:
                if target.id == LEYMIK_ID or is_admin(target.id):
                    return await message.reply("❌ Нельзя ограничить руководство!")

                passed = await check_deputy_limits(user_id, sender_mention)
                if not passed: return

                minutes = int(mute_match.group(1))
                until = datetime.utcnow() + timedelta(minutes=minutes)
                try:
                    await bot.restrict_chat_member(
                        TARGET_CHAT_ID,
                        target.id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=until
                    )
                    t_u = db.get_user(target.id)
                    await message.reply(f"🔇 Модератор выдал мут {get_user_mention(t_u)} на <b>{minutes} мин.</b>", parse_mode=ParseMode.HTML)
                except Exception:
                    await message.reply("⚠️ Ошибка при выдаче мута.")
                return

        elif lower_text == "размут":
            try:
                await bot.restrict_chat_member(
                    TARGET_CHAT_ID,
                    target.id,
                    permissions=ChatPermissions(
                        can_send_messages=True, can_send_media_messages=True,
                        can_send_other_messages=True, can_add_web_page_previews=True
                    )
                )
                t_u = db.get_user(target.id)
                await message.reply(f"🔊 С пользователя {get_user_mention(t_u)} сняты ограничения!", parse_mode=ParseMode.HTML)
            except Exception:
                await message.reply("⚠️ Ошибка при снятии мута.")
            return

    if user_can_mod and lower_text == "калл":
        members = db.get_all_members()
        if not members:
            return await message.reply("Список участников пуст.")
        tags = " ".join([f"@{u}" for u in members])
        return await message.answer(f"📢 <b>ОБЩИЙ СБОР ЧАТА!</b>\n\n{tags}", parse_mode=ParseMode.HTML)

    # --- ДУЭЛЬ КНБ: дуэль (ставка) (ПО РЕПЛАЮ) ---
    duel_match = re.match(r"^дуэль\s+(\d+)$", lower_text)
    if duel_match and message.reply_to_message:
        target = message.reply_to_message.from_user
        if target.id == user_id:
            return await message.reply("😅 Нельзя вызвать на дуэль самого себя!")
        bet = int(duel_match.group(1))
        if bet <= 0: return await message.reply("Ставка больше 0!")

        t_u = db.get_user(target.id, target.username or "", target.first_name)
        if sender_data["balance"] < bet:
            return await message.reply(f"❌ У тебя не хватает листочек! Баланс: {sender_data['balance']} 🍁")
        if t_u["balance"] < bet:
            return await message.reply(f"❌ У {get_user_mention(t_u)} не хватает листочек на ставку! (Баланс: {t_u['balance']} 🍁)")

        d_id = duel_counter
        duel_counter += 1

        active_rps_duels[d_id] = {
            "p1_id": user_id,
            "p2_id": target.id,
            "bet": bet,
            "status": "pending",
            "p1_choice": None,
            "p2_choice": None,
            "expires_at": time.time() + 120 # 2 минуты на принятие
        }

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚔️ Принять дуэль в КНБ!", callback_data=f"duel_accept_{d_id}")]
        ])

        return await message.reply(
            f"⚔️ <b>ВЫЗОВ НА ДУЭЛЬ В КНБ!</b> ⚔️\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{sender_mention} бросает вызов {get_user_mention(t_u)}!\n"
            f"💰 Ставка: <b>{bet} 🍁 листочек</b> (Победитель забирает {bet*2} 🍁)\n\n"
            f"⏳ <i>У вызванного игрока есть ровно 2 минуты, чтобы принять бой!</i>",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

    # --- КАЗИНО КНБ С БОТОМ ---
    rps_match = re.match(r"^(\d+)\s+(камень|ножницы|бумага)$", lower_text)
    if rps_match:
        bet_amount = int(rps_match.group(1))
        user_choice = rps_match.group(2)
        user = sender_data

        if bet_amount <= 0:
            return await message.reply("⚠️ Ставка должна быть больше 0!")
        if user["balance"] < bet_amount:
            return await message.reply(f"❌ <b>Недостаточно листочек!</b> Баланс: <b>{user['balance']}</b> 🍁", parse_mode=ParseMode.HTML)

        choices = ["камень", "ножницы", "бумага"]
        bot_choice = random.choice(choices)
        icons = {"камень": "🪨 КАМЕНЬ", "ножницы": "✂️ НОЖНИЦЫ", "бумага": "📄 БУМАГА"}

        if user_choice == bot_choice:
            return await message.reply(
                f"🤝 <b>НИЧЬЯ В КНБ!</b>\n"
                f"👤 Твой выбор: <b>{icons[user_choice]}</b> | 🤖 Бот: <b>{icons[bot_choice]}</b>\n"
                f"⚖️ Ставка <b>{bet_amount}</b> 🍁 возвращена!",
                parse_mode=ParseMode.HTML
            )
        elif (user_choice == "камень" and bot_choice == "ножницы") or \
             (user_choice == "ножницы" and bot_choice == "бумага") or \
             (user_choice == "бумага" and bot_choice == "камень"):
            win_amount = int(bet_amount * 2.2)
            profit = win_amount - bet_amount
            new_bal = db.update_balance(user_id, profit)
            return await message.reply(
                f"🎉 <b>ПОБЕДА В КНБ (2.2x)!</b>\n"
                f"👤 Твой выбор: <b>{icons[user_choice]}</b> | 🤖 Бот: <b>{icons[bot_choice]}</b>\n"
                f"🔥 Выигрыш: <b>+{win_amount}</b> 🍁! Баланс: <b>{new_bal}</b> 🍁",
                parse_mode=ParseMode.HTML
            )
        else:
            new_bal = db.update_balance(user_id, -bet_amount)
            return await message.reply(
                f"💀 <b>ПОРАЖЕНИЕ В КНБ!</b>\n"
                f"👤 Твой выбор: <b>{icons[user_choice]}</b> | 🤖 Бот: <b>{icons[bot_choice]}</b>\n"
                f"🥀 Ставка сгорела. Баланс: <b>{new_bal}</b> 🍁",
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
            f"<i>Ответьте (reply) на это сообщение ТОЛЬКО числом!</i>\n"
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

    # --- РП ДЕЙСТВИЯ ---
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
        t_u = db.get_user(target.id, target.username or "", target.first_name)
        act_word, act_desc = rp_actions[lower_text]
        return await message.answer(
            f"✨ <b>РП ДЕЙСТВИЕ</b> ✨\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🐾 <b>{sender_mention}</b> {act_desc} <b>{get_user_mention(t_u)}</b>! 💖\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )

    # --- ПЛАТНЫЕ РП ---
    if lower_text in ["покурить", "закинуть снюс", "выпить"]:
        act_type = "smoke" if lower_text == "покурить" else "snus" if lower_text == "закинуть снюс" else "drink"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🍁 Заплатить 30 листочек", callback_data=f"paid_rp_{act_type}_{user_id}")]
        ])
        return await message.reply(
            f"🚬 <b>ПЛАТНОЕ РП: {text.upper()}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Стоимость действия: <b>30 🍁 листочек</b>.\n"
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

        rank_lvl = t_data.get("rank_level") or 0
        if target.id == LEYMIK_ID:
            rank = "👑 Главный Модератор"
        elif is_admin(target.id):
            rank = "🛡️ Администратор"
        elif rank_lvl == 2:
            rank = "🧊 Вилочник (Заморожен)" if t_data.get("is_frozen", 0) == 1 else "🍴 Вилочник"
        elif rank_lvl == 1:
            rank = "🧊 Суженый (Заморожен)" if t_data.get("is_frozen", 0) == 1 else "⚜️ Суженый Служенный"
        else:
            rank = "👤 Пользователь"

        joined_str = t_data.get("joined_at", "Неизвестно")
        try:
            dt_obj = datetime.strptime(str(joined_str).split(".")[0], "%Y-%m-%d %H:%M:%S")
            joined_formatted = dt_obj.strftime("%d.%m.%Y в %H:%M UTC")
        except Exception:
            joined_formatted = str(joined_str)

        m_info = db.get_marriage(target.id)
        if m_info:
            partner_name = m_info["user2_name"] if m_info["user1_id"] == target.id else m_info["user1_name"]
            marriage_status = f"В браке с {partner_name} 💍"
        else:
            marriage_status = "Холост / Не замужем 🕊️"

        return await message.reply(
            f"👤 <b>ДОСЬЕ УЧАСТНИКА LOCALHAUS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷 <b>Ник / Имя:</b> {get_user_mention(t_data)}\n"
            f"🆔 <b>ID:</b> <code>{target.id}</code>\n"
            f"🎖 <b>Ранг:</b> <b>{rank}</b>\n"
            f"💍 <b>Семейное положение:</b> {marriage_status}\n"
            f"💰 <b>Листочки:</b> <b>{t_data.get('balance', 0)}</b> 🍁\n"
            f"💎 <b>Алмазы (Атомы):</b> <b>{t_data.get('atoms', 0)}</b> ⚛️\n"
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

        t_u = db.get_user(target.id, target.username or "", target.first_name)

        if db.get_marriage(user_id):
            return await message.reply("⚠️ Ты уже состоишь в браке! Напиши <b>\"развод\"</b> для расторжения.", parse_mode=ParseMode.HTML)
        if db.get_marriage(target.id):
            return await message.reply(f"💔 {get_user_mention(t_u)} уже в браке!", parse_mode=ParseMode.HTML)

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
            f"🌹 <b>{sender_mention}</b> делает предложение руки и сердца <b>{get_user_mention(t_u)}</b>!\n\n"
            f"⏳ <i>У вас есть ровно 2 минуты на ответ...</i> ✨\n"
            f"━━━━━━━━━━━━━━━━━━━━━━",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )

    if lower_text == "развод":
        m_info = db.get_marriage(user_id)
        if not m_info:
            return await message.reply("🕊️ Ты не состоишь в браке!")
        partner_name = m_info["user2_name"] if m_info["user1_id"] == user_id else m_info["user1_name"]
        db.delete_marriage(user_id)
        return await message.answer(
            f"📜 <b>РАСТОРЖЕНИЕ БРАКА</b> 📜\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💔 <b>{sender_mention}</b> объявил(а) о разводе с <b>{partner_name}</b>.\n"
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
                f"{idx}. <b>{m['user1_name']}</b> 💖 <b>{m['user2_name']}</b> — вместе <b>{duration}</b>"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        return await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

    # --- ПЕРЕВОД АЛМАЗОВ (АТОМОВ): "п (число) атомы" ---
    atom_transfer_match = re.match(r"^п\s+(\d+)\s+атомы$", lower_text)
    if atom_transfer_match:
        if not message.reply_to_message:
            return await message.reply("💎 Ответь этой командой на сообщение того, кому переводишь алмазы!")
        target = message.reply_to_message.from_user
        if target.id == user_id:
            return await message.reply("😅 Нельзя переводить валюту самому себе!")
        if target.id == bot.id:
            return await message.reply("🍃 Спасибо, но боту валюта не нужна!")

        amount = int(atom_transfer_match.group(1))
        if amount <= 0: return await message.reply("⚠️ Сумма больше 0!")

        sender = sender_data
        if sender.get("atoms", 0) < amount:
            return await message.reply(f"❌ <b>Недостаточно алмазов!</b> Твой баланс: <b>{sender.get('atoms', 0)}</b> ⚛️", parse_mode=ParseMode.HTML)

        t_u = db.get_user(target.id, target.username or "", target.first_name)
        new_sender_atoms = db.update_atoms(user_id, -amount)
        new_target_atoms = db.update_atoms(target.id, amount)

        return await message.answer(
            f"💎 <b>УСПЕШНЫЙ ПЕРЕВОД АЛМАЗОВ (АТОМОВ)</b> ⚛️\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Отправитель: <b>{sender_mention}</b>\n"
            f"🎁 Получатель: <b>{get_user_mention(t_u)}</b>\n"
            f"💎 Сумма: <b>{amount}</b> ⚛️ алмазов\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Твой остаток: <b>{new_sender_atoms}</b> ⚛️",
            parse_mode=ParseMode.HTML
        )

    # --- ПЕРЕВОД ЛИСТОЧЕК: "п (число)" ---
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
        if amount <= 0: return await message.reply("⚠️ Сумма больше 0!")

        sender = sender_data
        if sender["balance"] < amount:
            return await message.reply(f"❌ <b>Недостаточно листочек!</b> Баланс: <b>{sender['balance']}</b> 🍁", parse_mode=ParseMode.HTML)

        t_u = db.get_user(target.id, target.username or "", target.first_name)
        new_sender_bal = db.update_balance(user_id, -amount)
        new_target_bal = db.update_balance(target.id, amount)

        return await message.answer(
            f"💸 <b>УСПЕШНЫЙ ПЕРЕВОД ЛИСТОЧЕК</b> 🍁\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 Отправитель: <b>{sender_mention}</b>\n"
            f"🎁 Получатель: <b>{get_user_mention(t_u)}</b>\n"
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
        for idx, u_row in enumerate(top_users):
            medal = medals[idx] if idx < len(medals) else f"{idx+1}."
            u_mention = get_user_mention(u_row)
            lines.append(f"{medal} <b>{u_mention}</b> — <b>{u_row['msg_count']}</b> сообщ.")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━\n⏰ <i>Итоги ровно в 00:00 по МСК! Победитель получает +500 🍁</i>")
        return await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

    # --- БАЛАНС ---
    if lower_text in ["б", "баланс"]:
        return await message.reply(
            f"🍃 <b>Кошелек: {sender_mention}</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"💰 Листочки: <b>{sender_data.get('balance', 0)}</b> 🍁\n"
            f"💎 Алмазы (Атомы): <b>{sender_data.get('atoms', 0)}</b> ⚛️\n"
            f"━━━━━━━━━━━━━━━━",
            parse_mode=ParseMode.HTML
        )

    # --- РУЛЕТКА ---
    bet_match = re.match(r"^(\d+)\s+([чк])$", lower_text)
    if bet_match:
        bet_amount = int(bet_match.group(1))
        chosen_color = bet_match.group(2)
        user = sender_data

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

    # ДРОП ЗА АКТИВНОСТЬ (3%)
    if random.random() < 0.03:
        reward = random.randint(10, 30)
        db.update_balance(user_id, reward)
        await message.reply(
            f"🍃 <b>Удача!</b> За активность {sender_mention} находит <b>{reward}</b> 🍁 листочек!",
            parse_mode=ParseMode.HTML
        )

# --- РАНДОМНЫЕ АВТО-КВЕСТЫ (ОТ 1 ДО 20 МИНУТ) ---
async def random_quest_scheduler():
    global current_auto_quest
    while True:
        delay = random.randint(60, 1200) # от 1 до 20 минут
        await asyncio.sleep(delay)

        # Если уже висит нерешенный квест — ждем!
        if current_auto_quest:
            continue

        quest_type = random.choice(["slot", "math"])

        if quest_type == "slot":
            reward_atoms = random.randint(1, 50)
            msg = await bot.send_message(
                TARGET_CHAT_ID,
                f"⚡ <b>МОЛНИЕНОСНЫЙ КВЕСТ!</b> ⚡\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎰 Кто первый выбьет <b>777</b> в казино слотах (смайлик 🎰) — "
                f"получит <b>{reward_atoms} алмазов (атомов) 💎</b>!\n"
                f"━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML
            )
            try:
                await bot.pin_chat_message(TARGET_CHAT_ID, msg.message_id)
            except Exception: pass

            current_auto_quest = {
                "type": "slot",
                "reward": reward_atoms,
                "answer": None,
                "message_id": msg.message_id
            }

        elif quest_type == "math":
            reward_atoms = random.randint(1, 3)
            n1 = random.randint(10, 999)
            n2 = random.randint(10, 999)
            op = random.choice(["+", "-"])
            correct = n1 + n2 if op == "+" else n1 - n2

            msg = await bot.send_message(
                TARGET_CHAT_ID,
                f"🧠 <b>БЛИЦ-ВИКТОРИНА!</b> 🧠\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Кто первый решит пример: <code>{n1} {op} {n2} = ?</code> — "
                f"получит <b>{reward_atoms} алмазов (атомов) 💎</b>!\n\n"
                f"👉 <i>Отвечайте в чат числом (можно реплаем)!</i>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━",
                parse_mode=ParseMode.HTML
            )
            try:
                await bot.pin_chat_message(TARGET_CHAT_ID, msg.message_id)
            except Exception: pass

            current_auto_quest = {
                "type": "math",
                "reward": reward_atoms,
                "answer": correct,
                "message_id": msg.message_id
            }

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
                for idx, u_row in enumerate(top_users):
                    medal = medals[idx] if idx < len(medals) else f"{idx+1}."
                    lines.append(f"{medal} <b>{get_user_mention(u_row)}</b> — <b>{u_row['msg_count']}</b> сообщ.")

                winner = top_users[0]
                db.update_balance(winner["user_id"], 500)
                lines.append(
                    f"\n🎉 Победитель дня — <b>{get_user_mention(winner)}</b>!\n"
                    f"🎁 Награда: <b>+500</b> 🍁 листочек зачислена на баланс!\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Новый день начался! Общайтесь активнее! ✨"
                )
                try:
                    await bot.send_message(TARGET_CHAT_ID, "\n".join(lines), parse_mode=ParseMode.HTML)
                except Exception as e:
                    print(f"Ошибка отправки итогов: {e}")

            db.mark_reward_given(ended_day)
        await asyncio.sleep(5)

# --- KEEP-ALIVE ---
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
            except Exception: pass
            await asyncio.sleep(300)

async def on_startup(app):
    await refresh_admin_cache()
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/webhook"
        await bot.set_webhook(
            webhook_url,
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query", "my_chat_member", "chat_join_request"]
        )
        print(f"Вебхук активен: {webhook_url}")
    asyncio.create_task(keep_alive_task())
    asyncio.create_task(midnight_msk_scheduler())
    asyncio.create_task(random_quest_scheduler())

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

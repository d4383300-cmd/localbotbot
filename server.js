require('dotenv').config();
const express = require('express');
const https = require('https');
const http = require('http');
const { Telegraf, Markup } = require('telegraf');
const db = require('./database');

const BOT_TOKEN = process.env.BOT_TOKEN || '8642763436:AAFCMLXHsFjwnXfiZ_N3yWzZIKJ--oszfhk';
const TARGET_CHAT_ID = -1004373765011;
const LEYMIK_ID = 7505593850;
const INVITE_LINK = 'https://t.me/+Un85Q4TUznc4YWYy';
const PORT = process.env.PORT || 3000;
const RENDER_EXTERNAL_URL = process.env.RENDER_EXTERNAL_URL; // URL вида https://ваш-проект.onrender.com

const bot = new Telegraf(BOT_TOKEN);
const app = express();

// Память состояний
const userMessageHistory = new Map();
const userStickerHistory = new Map();
const userForms = new Map();
const pendingRejections = new Map();

// --- ⚡ СВЕРХБЫСТРЫЙ КЭШ АДМИНИСТРАТОРОВ ---
let cachedAdmins = new Set();
let lastAdminFetch = 0;

async function refreshAdminCache(telegram) {
  try {
    const admins = await telegram.getChatAdministrators(TARGET_CHAT_ID);
    cachedAdmins = new Set(admins.map(a => a.user.id));
    lastAdminFetch = Date.now();
  } catch (err) {
    console.error('Ошибка синхронизации админов:', err.message);
  }
}

function isAdmin(userId) {
  if (userId === LEYMIK_ID) return true;
  return cachedAdmins.has(userId);
}

// 1. Уведомление при получении прав админа
bot.on('my_chat_member', async (ctx) => {
  const status = ctx.myChatMember.new_chat_member.status;
  if (ctx.chat.id === TARGET_CHAT_ID && status === 'administrator') {
    await refreshAdminCache(ctx.telegram);
    await ctx.reply('✨ <b>Права выданы. Готов к работе!</b>', { parse_mode: 'HTML' });
  }
});

// 2. Приветствие новичков
bot.on('new_chat_members', async (ctx) => {
  if (ctx.chat.id !== TARGET_CHAT_ID) return;
  for (const member of ctx.message.new_chat_members) {
    if (member.id === ctx.botInfo.id) continue;
    await ctx.reply(
      `🌿 <b>Добро пожаловать в Localhaus, <a href="tg://user?id=${member.id}">${member.first_name}</a>!</b>\n` +
      `Обязательно прочитай правила — напиши <b>"правила"</b>, а также найди себе друга для общения! ✨`,
      { parse_mode: 'HTML' }
    );
  }
});

// 3. Личные сообщения: Анкета кандидата
bot.on('message', async (ctx, next) => {
  if (ctx.chat.type === 'private') {
    const userId = ctx.from.id;

    if (db.isBlocked(userId)) {
      return; // Заблокированные игнорируются
    }

    // Обработка ввода причины отклонения от Леймика
    if (userId === LEYMIK_ID && pendingRejections.has(LEYMIK_ID)) {
      const targetUserId = pendingRejections.get(LEYMIK_ID);
      pendingRejections.delete(LEYMIK_ID);
      try {
        await bot.telegram.sendMessage(
          targetUserId,
          `❌ <b>Ваша заявка в Localhaus была отклонена.</b>\n💬 <b>Причина:</b> ${ctx.message.text}`,
          { parse_mode: 'HTML' }
        );
        return ctx.reply('✅ Причина отправлена пользователю.');
      } catch (e) {
        return ctx.reply('⚠️ Не удалось доставить сообщение (возможно, бот заблокирован кандидатом).');
      }
    }

    const text = ctx.message.text ? ctx.message.text.trim() : '';

    if (text === '/start') {
      userForms.set(userId, { step: 'name' });
      return ctx.reply(
        `👋 <b>Приветствуем в приемной Localhaus!</b>\n\n` +
        `Чтобы получить доступ к чату, ответь на пару вопросов.\n` +
        `1️⃣ <b>Как тебя зовут?</b>`,
        { parse_mode: 'HTML' }
      );
    }

    const form = userForms.get(userId);
    if (form) {
      if (form.step === 'name') {
        form.name = text;
        form.step = 'age';
        userForms.set(userId, form);
        return ctx.reply('2️⃣ <b>Сколько тебе лет?</b> (Укажи реальный возраст):', { parse_mode: 'HTML' });
      }

      if (form.step === 'age') {
        const age = parseInt(text, 10);
        if (isNaN(age) || age < 10 || age > 99) {
          return ctx.reply('⚠️ Пожалуйста, укажи реальный возраст числом:');
        }
        form.age = age;
        form.step = 'confirm';
        userForms.set(userId, form);

        return ctx.reply(
          `📋 <b>Проверь правильность данных:</b>\n` +
          `• <b>Имя:</b> ${form.name}\n` +
          `• <b>Возраст:</b> ${form.age}\n\n` +
          `<i>С правилами чата обязуешься ознакомиться при входе.</i>\n\n` +
          `<b>Всё верно?</b>`,
          {
            parse_mode: 'HTML',
            ...Markup.inlineKeyboard([
              [Markup.button.callback('✅ Да', 'form_confirm'), Markup.button.callback('❌ Нет', 'form_restart')]
            ])
          }
        );
      }
    }

    return ctx.reply('Напиши /start, чтобы начать заполнение анкеты.');
  }

  // Фильтр: если группа не наша — игнорируем
  if (ctx.chat.id !== TARGET_CHAT_ID) return;
  return next();
});

// Кнопка подтверждения анкеты
bot.action('form_confirm', async (ctx) => {
  const userId = ctx.from.id;
  const form = userForms.get(userId);
  if (!form) return ctx.answerCbQuery('Анкета устарела, начни заново.');

  const appId = db.createApplication(userId, ctx.from.username || '', form.name, form.age);
  userForms.delete(userId);

  await ctx.editMessageText('✅ <b>Твоя заявка отправлена администрации! Ожидай решения.</b>', { parse_mode: 'HTML' });

  await bot.telegram.sendMessage(
    TARGET_CHAT_ID,
    `📥 <b>НОВАЯ ЗАЯВКА В LOCALHAUS!</b>\n\n` +
    `👤 <b>Кандидат:</b> @${ctx.from.username || 'нет'} (ID: <code>${userId}</code>)\n` +
    `📝 <b>Имя:</b> ${form.name}\n` +
    `🎂 <b>Возраст:</b> ${form.age}\n\n` +
    `Модератор @Leymik, примите решение!`,
    {
      parse_mode: 'HTML',
      ...Markup.inlineKeyboard([
        [
          Markup.button.callback('✅ Принять', `adm_accept_${appId}_${userId}`),
          Markup.button.callback('❌ Отклонить', `adm_reject_${appId}_${userId}`),
          Markup.button.callback('🚫 Заблокировать', `adm_block_${appId}_${userId}`)
        ]
      ])
    }
  );
});

bot.action('form_restart', async (ctx) => {
  const userId = ctx.from.id;
  userForms.set(userId, { step: 'name' });
  await ctx.editMessageText('🔄 Начнем заново.\n\n1️⃣ <b>Как тебя зовут?</b>', { parse_mode: 'HTML' });
});

// Решения Леймика по кнопкам
bot.action(/adm_(accept|reject|block)_(\d+)_(\d+)/, async (ctx) => {
  const adminId = ctx.from.id;
  const action = ctx.match[1];
  const appId = ctx.match[2];
  const targetUserId = parseInt(ctx.match[3], 10);

  if (adminId !== LEYMIK_ID) {
    return ctx.answerCbQuery('⛔ Только @Leymik может выносить вердикт!', { show_alert: true });
  }

  const appData = db.getApplication(appId);
  if (!appData || appData.status !== 'pending') {
    return ctx.answerCbQuery('Решение уже вынесено.');
  }

  if (action === 'accept') {
    db.updateAppStatus(appId, 'accepted');
    try {
      await bot.telegram.sendMessage(
        targetUserId,
        `🎉 <b>Твоя заявка в Localhaus одобрена!</b>\n\nСсылка на вход:\n${INVITE_LINK}`,
        { parse_mode: 'HTML' }
      );
    } catch (e) {}
    await ctx.editMessageText(`${ctx.callbackQuery.message.text}\n\n🟢 <b>ОДОБРЕНО (@Leymik)</b>`, { parse_mode: 'HTML' });
  }

  if (action === 'reject') {
    pendingRejections.set(LEYMIK_ID, targetUserId);
    db.updateAppStatus(appId, 'rejected');
    await ctx.editMessageText(`${ctx.callbackQuery.message.text}\n\n🔴 <b>ОТКЛОНЕНО (@Leymik)</b>`, { parse_mode: 'HTML' });
    try {
      await bot.telegram.sendMessage(LEYMIK_ID, `Напишите сообщение с причиной отказа для заявки #${appId}:`);
    } catch (e) {}
  }

  if (action === 'block') {
    db.updateAppStatus(appId, 'blocked');
    db.setBlocked(targetUserId, 1);
    await ctx.editMessageText(`${ctx.callbackQuery.message.text}\n\n🚫 <b>ПОЛЬЗОВАТЕЛЬ ЗАБЛОКИРОВАН</b>`, { parse_mode: 'HTML' });
  }

  await ctx.answerCbQuery();
});

// 4. Обработка всех сообщений в чате
bot.on('message', async (ctx) => {
  if (ctx.chat.id !== TARGET_CHAT_ID) return;

  const text = ctx.message.text || ctx.message.caption || '';
  const userId = ctx.from.id;
  const userIsAdmin = isAdmin(userId);

  // Периодическое тихое обновление кэша админов раз в 10 минут
  if (Date.now() - lastAdminFetch > 10 * 60 * 1000) {
    refreshAdminCache(ctx.telegram);
  }

  // Запоминаем участника для команды "калл"
  if (ctx.from.username) {
    db.saveMember(userId, ctx.from.username);
  }

  // --- Бот ты тут? ---
  if (text.toLowerCase() === 'бот ты тут?') {
    return ctx.reply('Да');
  }

  // --- Правила ---
  if (text.toLowerCase() === 'правила') {
    return ctx.reply(
      `📜 <b>ПРАВИЛА ЧАТА LOCALHAUS</b> 📜\n` +
      `━━━━━━━━━━━━━━━━━━━━━━\n` +
      `1️⃣ <b>Спам лесенкой</b>: больше 5 сообщений за 3 сек — Мут 5 мин.\n` +
      `2️⃣ <b>Спам стикерами</b>: удаление стикеров + Предупреждение.\n` +
      `3️⃣ <b>Ссылки и реклама</b>: 1 раз — предупреждение, 2 раза за день — Бан.\n` +
      `4️⃣ <b>18+ контент</b>: шок-контент, порнография — Варн / Бан.\n` +
      `5️⃣ Уважение к участникам и администрации чата.\n` +
      `━━━━━━━━━━━━━━━━━━━━━━`,
      { parse_mode: 'HTML' }
    );
  }

  // --- Защита от спама стикерами (3+ стикера за 5 сек) ---
  if (ctx.message.sticker && !userIsAdmin) {
    const now = Date.now();
    let stickers = userStickerHistory.get(userId) || [];
    stickers = stickers.filter(t => now - t.time < 5000);
    stickers.push({ time: now, messageId: ctx.message.message_id });
    userStickerHistory.set(userId, stickers);

    if (stickers.length >= 3) {
      for (const item of stickers) {
        try { await ctx.deleteMessage(item.messageId); } catch (e) {}
      }
      userStickerHistory.delete(userId);
      return ctx.reply(
        `⚠️ <a href="tg://user?id=${userId}">${ctx.from.first_name}</a>, прошу пожалуйста не нарушать правила чата!\n` +
        `Чтобы узнать правила чата напишите <b>"правила"</b>.`,
        { parse_mode: 'HTML' }
      );
    }
  }

  // --- Защита от спама лесенкой (> 5 сообщений за 3 сек) ---
  if (!userIsAdmin) {
    const now = Date.now();
    let history = userMessageHistory.get(userId) || [];
    history = history.filter(t => now - t < 3000);
    history.push(now);
    userMessageHistory.set(userId, history);

    if (history.length > 5) {
      userMessageHistory.delete(userId);
      try {
        await ctx.deleteMessage();
        await ctx.restrictChatMember(userId, {
          until_date: Math.floor(Date.now() / 1000) + 300 // Мут на 5 минут
        });
        return ctx.reply(`🔇 <a href="tg://user?id=${userId}">${ctx.from.first_name}</a> получил мут на 5 минут за спам лесенкой!`, { parse_mode: 'HTML' });
      } catch (e) {}
    }
  }

  // --- Защита от рекламы и ссылок ---
  const urlRegex = /(https?:\/\/[^\s]+|t\.me\/[^\s]+|www\.[^\s]+|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b)/gi;
  if (urlRegex.test(text) && !userIsAdmin) {
    try { await ctx.deleteMessage(); } catch (e) {}

    const count = db.checkAndIncrementLinks(userId);
    if (count >= 2) {
      try {
        await ctx.banChatMember(userId);
        return ctx.reply(`🚫 <a href="tg://user?id=${userId}">${ctx.from.first_name}</a> заблокирован за повторную рекламу.`, { parse_mode: 'HTML' });
      } catch (e) {}
    } else {
      return ctx.reply(`⚠️ <a href="tg://user?id=${userId}">${ctx.from.first_name}</a>, ссылки запрещены! (Предупреждение 1/2 за день)`, { parse_mode: 'HTML' });
    }
    return;
  }

  // --- Экономика: Баланс ---
  const lower = text.toLowerCase().trim();
  if (lower === 'б' || lower === 'баланс') {
    const user = db.getUser(userId, ctx.from.username, ctx.from.first_name);
    return ctx.reply(
      `🍃 <b>Кошелек: <a href="tg://user?id=${userId}">${ctx.from.first_name}</a></b>\n` +
      `━━━━━━━━━━━━━━━━\n` +
      `💰 Баланс: <b>${user.balance}</b> 🍁 листочек\n` +
      `━━━━━━━━━━━━━━━━`,
      { parse_mode: 'HTML' }
    );
  }

  // --- Казино рулетка (например: "50 ч" или "25 к") ---
  const betMatch = lower.match(/^(\d+)\s+([чк])$/);
  if (betMatch) {
    const betAmount = parseInt(betMatch[1], 10);
    const chosenColor = betMatch[2];
    const user = db.getUser(userId, ctx.from.username, ctx.from.first_name);

    if (betAmount <= 0) {
      return ctx.reply('⚠️ Ставка должна быть больше 0!');
    }
    if (user.balance < betAmount) {
      return ctx.reply(`❌ <b>Недостаточно листочек!</b> Баланс: <b>${user.balance}</b> 🍁`, { parse_mode: 'HTML' });
    }

    const colors = ['ч', 'к'];
    const outcome = colors[Math.floor(Math.random() * 2)];
    const outcomeName = outcome === 'ч' ? '⬛ ЧЁРНЫЙ' : '🟥 КРАСНЫЙ';
    const chosenName = chosenColor === 'ч' ? '⬛ ЧЁРНЫЙ' : '🟥 КРАСНЫЙ';

    if (chosenColor === outcome) {
      const newBal = db.updateBalance(userId, betAmount);
      return ctx.reply(
        `🎰 <b>РУЛЕТКА LOCALHAUS</b> 🎰\n` +
        `━━━━━━━━━━━━━━━━\n` +
        `🎯 Выбор: <b>${chosenName}</b>\n` +
        `🎲 Выпало: <b>${outcomeName}</b>\n\n` +
        `🔥 <b>ПОБЕДА (2x)!</b> Вы выиграли: <b>+${betAmount}</b> 🍁 листочек!\n` +
        `💰 Текущий баланс: <b>${newBal}</b> 🍁`,
        { parse_mode: 'HTML' }
      );
    } else {
      const newBal = db.updateBalance(userId, -betAmount);
      return ctx.reply(
        `🎰 <b>РУЛЕТКА LOCALHAUS</b> 🎰\n` +
        `━━━━━━━━━━━━━━━━\n` +
        `🎯 Выбор: <b>${chosenName}</b>\n` +
        `🎲 Выпало: <b>${outcomeName}</b>\n\n` +
        `💀 <b>ПОРАЖЕНИЕ (0x)!</b> Ставка сгорела.\n` +
        `💰 Текущий баланс: <b>${newBal}</b> 🍁`,
        { parse_mode: 'HTML' }
      );
    }
  }

  // --- Дроп валюты за активность (Шанс 5%) ---
  if (Math.random() < 0.05) {
    const reward = Math.floor(Math.random() * 21) + 10;
    db.updateBalance(userId, reward);
    await ctx.reply(
      `🍃 <b>Удача!</b> За активность <a href="tg://user?id=${userId}">${ctx.from.first_name}</a> получает <b>${reward}</b> 🍁 листочек!`,
      { parse_mode: 'HTML' }
    );
  }

  // --- КОМАНДЫ ТОЛЬКО ДЛЯ АДМИНИСТРАТОРОВ ---
  if (!userIsAdmin) return;

  // Бан по реплаю
  if (lower === 'бан') {
    if (!ctx.message.reply_to_message) return ctx.reply('⚠️ Ответьте этой командой на сообщение нарушителя!');
    const target = ctx.message.reply_to_message.from;
    try {
      await ctx.banChatMember(target.id);
      return ctx.reply(`🚫 <a href="tg://user?id=${target.id}">${target.first_name}</a> исключен и добавлен в черный список.`, { parse_mode: 'HTML' });
    } catch (e) {
      return ctx.reply('⚠️ Ошибка при бане (проверьте права бота).');
    }
  }

  // Кик по реплаю
  if (lower === 'кик') {
    if (!ctx.message.reply_to_message) return ctx.reply('⚠️ Ответьте этой командой на сообщение нарушителя!');
    const target = ctx.message.reply_to_message.from;
    try {
      await ctx.banChatMember(target.id);
      await ctx.unbanChatMember(target.id);
      return ctx.reply(`🚪 <a href="tg://user?id=${target.id}">${target.first_name}</a> был исключен.`, { parse_mode: 'HTML' });
    } catch (e) {
      return ctx.reply('⚠️ Ошибка при исключении.');
    }
  }

  // Мут: "мут [минуты]"
  const muteMatch = lower.match(/^мут\s+(\d+)$/);
  if (muteMatch) {
    if (!ctx.message.reply_to_message) return ctx.reply('⚠️ Ответьте командой на сообщение нарушителя!');
    const target = ctx.message.reply_to_message.from;
    const minutes = parseInt(muteMatch[1], 10);
    const until = Math.floor(Date.now() / 1000) + minutes * 60;

    try {
      await ctx.restrictChatMember(target.id, { until_date: until });
      return ctx.reply(`🔇 <a href="tg://user?id=${target.id}">${target.first_name}</a> получил мут на <b>${minutes} мин.</b>`, { parse_mode: 'HTML' });
    } catch (e) {
      return ctx.reply('⚠️ Ошибка при выдаче мута.');
    }
  }

  // Зазыв всех: "калл"
  if (lower === 'калл') {
    const members = db.getAllMembers();
    if (members.length === 0) return ctx.reply('Список участников пуст.');
    const tags = members.map(m => `@${m.username}`).join(' ');
    return ctx.reply(`📢 <b>ОБЩИЙ СБОР ЧАТА!</b>\n\n${tags}`, { parse_mode: 'HTML' });
  }
});

// --- ВЕБХУК И HTTP СЕРВЕР ---
const WEBHOOK_PATH = `/webhook/${bot.token}`;
app.use(express.json());

app.post(WEBHOOK_PATH, (req, res) => {
  bot.handleUpdate(req.body, res);
});

app.get('/', (req, res) => {
  res.send('Localhaus Bot: Status 200 OK (Keep-Alive Active)');
});

// Запуск приложения
app.listen(PORT, async () => {
  console.log(`Server started on port ${PORT}`);

  // Предзагрузка кэша админов
  await refreshAdminCache(bot.telegram);

  // Настройка Webhook
  if (RENDER_EXTERNAL_URL) {
    const webhookUrl = `${RENDER_EXTERNAL_URL}${WEBHOOK_PATH}`;
    await bot.telegram.setWebhook(webhookUrl);
    console.log(`Webhook successfully set to: ${webhookUrl}`);

    // --- ⏰ СИСТЕМА ПРОТИВ СНА (Self-Ping каждые 8 минут) ---
    setInterval(() => {
      const client = RENDER_EXTERNAL_URL.startsWith('https') ? https : http;
      client.get(RENDER_EXTERNAL_URL, (res) => {
        console.log(`[Keep-Alive] Ping sent to Render: status ${res.statusCode}`);
      }).on('error', (err) => {
        console.error('[Keep-Alive] Ping error:', err.message);
      });
    }, 8 * 60 * 1000); // 8 минут
  } else {
    console.warn('⚠️ Переменная RENDER_EXTERNAL_URL не задана!');
  }
});

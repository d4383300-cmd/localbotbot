require('dotenv').config();
const express = require('express');
const { Telegraf, Markup } = require('telegraf');
const db = require('./database');

const BOT_TOKEN = process.env.BOT_TOKEN || '8642763436:AAFCMLXHsFjwnXfiZ_N3yWzZIKJ--oszfhk';
const TARGET_CHAT_ID = -1004373765011;
const LEYMIK_ID = 7505593850;
const INVITE_LINK = 'https://t.me/+Un85Q4TUznc4YWYy';
const PORT = process.env.PORT || 3000;
const RENDER_EXTERNAL_URL = process.env.RENDER_EXTERNAL_URL; // Render URL вида https://your-app.onrender.com

const bot = new Telegraf(BOT_TOKEN);
const app = express();

// Память для трекинга флуда и состояний анкет
const userMessageHistory = new Map();
const userStickerHistory = new Map();
const userForms = new Map();
const pendingRejections = new Map(); // Ожидание причины отклонения от Леймика

// Проверка на права администратора
async function isAdmin(ctx, userId) {
  if (userId === LEYMIK_ID) return true;
  try {
    const member = await ctx.telegram.getChatMember(TARGET_CHAT_ID, userId);
    return ['creator', 'administrator'].includes(member.status);
  } catch (err) {
    return false;
  }
}

// 1. Уведомление при получении прав администратора
bot.on('my_chat_member', async (ctx) => {
  const status = ctx.myChatMember.new_chat_member.status;
  if (ctx.chat.id === TARGET_CHAT_ID && status === 'administrator') {
    await ctx.reply('✨ <b>Права выданы. Готов к работе!</b>', { parse_mode: 'HTML' });
  }
});

// 2. Приветствие новичков в чате
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

// 3. Обработка личных сообщений (Анкета вступления)
bot.on('message', async (ctx, next) => {
  if (ctx.chat.type === 'private') {
    const userId = ctx.from.id;

    if (db.isBlocked(userId)) {
      return; // Игнорирование заблокированных
    }

    // Если Леймик вводит причину отклонения в ЛС
    if (userId === LEYMIK_ID && pendingRejections.has(LEYMIK_ID)) {
      const targetUserId = pendingRejections.get(LEYMIK_ID);
      pendingRejections.delete(LEYMIK_ID);
      try {
        await bot.telegram.sendMessage(
          targetUserId,
          `❌ <b>Ваша заявка в Localhaus была отклонена.</b>\n💬 <b>Причина:</b> ${ctx.message.text}`,
          { parse_mode: 'HTML' }
        );
        return ctx.reply('✅ Причина отправлена кандидату.');
      } catch (e) {
        return ctx.reply('⚠️ Не удалось отправить сообщение кандидату (возможно, бот заблокирован им).');
      }
    }

    const text = ctx.message.text ? ctx.message.text.trim() : '';

    if (text === '/start') {
      userForms.set(userId, { step: 'name' });
      return ctx.reply(
        `👋 <b>Приветствуем в приемной Localhaus!</b>\n\n` +
        `Чтобы получить доступ к чату, заполните короткую анкету.\n` +
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
          return ctx.reply('⚠️ Пожалуйста, укажи корректный числовой возраст:');
        }
        form.age = age;
        form.step = 'confirm';
        userForms.set(userId, form);

        return ctx.reply(
          `📋 <b>Проверь свои данные:</b>\n` +
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

    return ctx.reply('Используй /start, чтобы подать анкету на вступление.');
  }

  // Фильтр чатов: в группах слушаем только TARGET_CHAT_ID
  if (ctx.chat.id !== TARGET_CHAT_ID) return;
  return next();
});

// Обработка кнопок анкеты кандидатом
bot.action('form_confirm', async (ctx) => {
  const userId = ctx.from.id;
  const form = userForms.get(userId);
  if (!form) return ctx.answerCbQuery('Анкета не найдена, начните заново.');

  const appId = db.createApplication(userId, ctx.from.username || '', form.name, form.age);
  userForms.delete(userId);

  await ctx.editMessageText('✅ <b>Твоя заявка успешно отправлена на проверку администрации! Ожидай ответа.</b>', { parse_mode: 'HTML' });

  // Отправка заявки в целевой чат с тегом Леймика
  await bot.telegram.sendMessage(
    TARGET_CHAT_ID,
    `📥 <b>НОВАЯ ЗАЯВКА НА ВСТУПЛЕНИЕ!</b>\n\n` +
    `👤 <b>Кандидат:</b> @${ctx.from.username || 'отсутствует'} (ID: <code>${userId}</code>)\n` +
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

// Решения по анкете (только Леймик)
bot.action(/adm_(accept|reject|block)_(\d+)_(\d+)/, async (ctx) => {
  const adminId = ctx.from.id;
  const action = ctx.match[1];
  const appId = ctx.match[2];
  const targetUserId = parseInt(ctx.match[3], 10);

  if (adminId !== LEYMIK_ID) {
    return ctx.answerCbQuery('⛔ Только @Leymik может выносить решение по заявкам!', { show_alert: true });
  }

  const app = db.getApplication(appId);
  if (!app || app.status !== 'pending') {
    return ctx.answerCbQuery('Решение по этой заявке уже принято.');
  }

  if (action === 'accept') {
    db.updateAppStatus(appId, 'accepted');
    try {
      await bot.telegram.sendMessage(
        targetUserId,
        `🎉 <b>Поздравляем! Ваша заявка в Localhaus одобрена!</b>\n\nВступайте по ссылке:\n${INVITE_LINK}`,
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
      await bot.telegram.sendMessage(LEYMIK_ID, `Напишите в ответ сообщение с причиной отклонения для заявки #${appId}:`);
    } catch (e) {}
  }

  if (action === 'block') {
    db.updateAppStatus(appId, 'blocked');
    db.setBlocked(targetUserId, 1);
    await ctx.editMessageText(`${ctx.callbackQuery.message.text}\n\n🚫 <b>ПОЛЬЗОВАТЕЛЬ ЗАБЛОКИРОВАН</b>`, { parse_mode: 'HTML' });
  }

  await ctx.answerCbQuery();
});

// 4. Основной процессинг сообщений группы
bot.on('message', async (ctx) => {
  if (ctx.chat.id !== TARGET_CHAT_ID) return;

  const text = ctx.message.text || ctx.message.caption || '';
  const userId = ctx.from.id;
  const isUserAdmin = await isAdmin(ctx, userId);

  // Запоминаем участника для команды "калл"
  if (ctx.from.username) {
    db.saveMember(userId, ctx.from.username);
  }

  // --- Проверка онлайна ---
  if (text.toLowerCase() === 'бот ты тут?') {
    return ctx.reply('Да');
  }

  // --- Правила чата ---
  if (text.toLowerCase() === 'правила') {
    return ctx.reply(
      `📜 <b>ПРАВИЛА ЧАТА LOCALHAUS</b> 📜\n\n` +
      `1️⃣ <b>Флуд и спам лесенкой</b>: запрещено более 5 сообщений подряд за 3 сек (Предупреждение / Мут).\n` +
      `2️⃣ <b>Спам стикерами</b>: массовая отправка стикеров запрещена (Удаление + Предупреждение).\n` +
      `3️⃣ <b>Реклама и ссылки</b>: строгий запрет на несогласованные ссылки (1-й раз — Предупреждение, 2-й за день — Бан).\n` +
      `4️⃣ <b>Контент 18+</b>: мат/порнография/шок-контент карается варном.\n` +
      `5️⃣ Уважайте участников и администрацию!`,
      { parse_mode: 'HTML' }
    );
  }

  // --- Анти-спам стикерами (3+ стикера за 5 сек) ---
  if (ctx.message.sticker && !isUserAdmin) {
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

  // --- Анти-спам лесенкой (более 5 сообщений за 3 сек) ---
  if (!isUserAdmin) {
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
          until_date: Math.floor(Date.now() / 1000) + 300 // 5 минут мута
        });
        return ctx.reply(`🔇 Пользователь <a href="tg://user?id=${userId}">${ctx.from.first_name}</a> получил мут на 5 минут за спам лесенкой!`, { parse_mode: 'HTML' });
      } catch (e) {}
    }
  }

  // --- Анти-реклама и ссылки ---
  const urlRegex = /(https?:\/\/[^\s]+|t\.me\/[^\s]+|www\.[^\s]+|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b)/gi;
  if (urlRegex.test(text) && !isUserAdmin) {
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
      `🍃 <b>Баланс пользователя <a href="tg://user?id=${userId}">${ctx.from.first_name}</a>:</b>\n` +
      `━━━━━━━━━━━━━━━━\n` +
      `💰 У вас в кошельке: <b>${user.balance}</b> 🍁 листочек\n` +
      `━━━━━━━━━━━━━━━━`,
      { parse_mode: 'HTML' }
    );
  }

  // --- Казино: Депозит листочек (формат: "50 к" или "100 ч") ---
  const betMatch = lower.match(/^(\d+)\s+([чк])$/);
  if (betMatch) {
    const betAmount = parseInt(betMatch[1], 10);
    const chosenColor = betMatch[2]; // 'ч' или 'к'
    const user = db.getUser(userId, ctx.from.username, ctx.from.first_name);

    if (betAmount <= 0) {
      return ctx.reply('⚠️ Ставка должна быть больше 0!');
    }
    if (user.balance < betAmount) {
      return ctx.reply(`❌ <b>Недостаточно листочек!</b> Твой баланс: <b>${user.balance}</b> 🍁`, { parse_mode: 'HTML' });
    }

    // Розыгрыш 50/50
    const colors = ['ч', 'к'];
    const outcome = colors[Math.floor(Math.random() * colors.length)];
    const outcomeName = outcome === 'ч' ? '⬛ ЧЁРНЫЙ' : '🟥 КРАСНЫЙ';
    const chosenName = chosenColor === 'ч' ? '⬛ ЧЁРНЫЙ' : '🟥 КРАСНЫЙ';

    if (chosenColor === outcome) {
      const newBal = db.updateBalance(userId, betAmount);
      return ctx.reply(
        `🎰 <b>РУЛЕТКА LOCALHAUS</b> 🎰\n` +
        `━━━━━━━━━━━━━━━━\n` +
        `🎯 Твой выбор: <b>${chosenName}</b>\n` +
        `🎲 Выпало: <b>${outcomeName}</b>\n\n` +
        `🔥 <b>ПОБЕДА (2x)!</b> Вы выиграли <b>+${betAmount}</b> 🍁 листочек!\n` +
        `💰 Твой новый баланс: <b>${newBal}</b> 🍁`,
        { parse_mode: 'HTML' }
      );
    } else {
      const newBal = db.updateBalance(userId, -betAmount);
      return ctx.reply(
        `🎰 <b>РУЛЕТКА LOCALHAUS</b> 🎰\n` +
        `━━━━━━━━━━━━━━━━\n` +
        `🎯 Твой выбор: <b>${chosenName}</b>\n` +
        `🎲 Выпало: <b>${outcomeName}</b>\n\n` +
        `💀 <b>ПОРАЖЕНИЕ (0x)!</b> Ставка сгорела.\n` +
        `💰 Твой новый баланс: <b>${newBal}</b> 🍁`,
        { parse_mode: 'HTML' }
      );
    }
  }

  // --- Случайный дроп листочек за активность (Шанс 5%) ---
  if (Math.random() < 0.05) {
    const reward = Math.floor(Math.random() * 21) + 10; // от 10 до 30
    db.updateBalance(userId, reward);
    await ctx.reply(
      `🍃 <b>Удача!</b> За активность в чате <a href="tg://user?id=${userId}">${ctx.from.first_name}</a> находит <b>${reward}</b> 🍁 листочек!`,
      { parse_mode: 'HTML' }
    );
  }

  // --- КОМАНДЫ ТОЛЬКО ДЛЯ АДМИНИСТРАТОРОВ ---
  if (!isUserAdmin) return;

  // Бан по реплаю
  if (lower === 'бан') {
    if (!ctx.message.reply_to_message) return ctx.reply('⚠️ Ответьте этой командой на сообщение нарушителя!');
    const target = ctx.message.reply_to_message.from;
    try {
      await ctx.banChatMember(target.id);
      return ctx.reply(`🚫 Администратор исключил и забанил <a href="tg://user?id=${target.id}">${target.first_name}</a>.`, { parse_mode: 'HTML' });
    } catch (e) {
      return ctx.reply('⚠️ Не удалось забанить пользователя (проверьте права бота).');
    }
  }

  // Кик по реплаю
  if (lower === 'кик') {
    if (!ctx.message.reply_to_message) return ctx.reply('⚠️ Ответьте этой командой на сообщение нарушителя!');
    const target = ctx.message.reply_to_message.from;
    try {
      await ctx.banChatMember(target.id);
      await ctx.unbanChatMember(target.id);
      return ctx.reply(`🚪 <a href="tg://user?id=${target.id}">${target.first_name}</a> был исключен из чата.`, { parse_mode: 'HTML' });
    } catch (e) {
      return ctx.reply('⚠️ Не удалось кикнуть пользователя.');
    }
  }

  // Мут по реплаю: "мут [минуты]"
  const muteMatch = lower.match(/^мут\s+(\d+)$/);
  if (muteMatch) {
    if (!ctx.message.reply_to_message) return ctx.reply('⚠️ Ответьте этой командой на сообщение пользователя!');
    const target = ctx.message.reply_to_message.from;
    const minutes = parseInt(muteMatch[1], 10);
    const until = Math.floor(Date.now() / 1000) + minutes * 60;

    try {
      await ctx.restrictChatMember(target.id, { until_date: until });
      return ctx.reply(`🔇 Пользователю <a href="tg://user?id=${target.id}">${target.first_name}</a> выдан мут на <b>${minutes} мин.</b>`, { parse_mode: 'HTML' });
    } catch (e) {
      return ctx.reply('⚠️ Не удалось ограничить пользователя.');
    }
  }

  // Зазыв всех: "калл"
  if (lower === 'калл') {
    const members = db.getAllMembers();
    if (members.length === 0) return ctx.reply('Список участников пуст.');
    
    // Формируем упоминания порциями
    const tags = members.map(m => `@${m.username}`).join(' ');
    return ctx.reply(`📢 <b>ОБЩИЙ СБОР!</b>\n\n${tags}`, { parse_mode: 'HTML' });
  }
});

// --- Настройка Webhook и запуск Express ---
const WEBHOOK_PATH = `/webhook/${bot.token}`;

app.use(express.json());

// Маршрут для обработки вебхука Telegram
app.post(WEBHOOK_PATH, (req, res) => {
  bot.handleUpdate(req.body, res);
});

// Health check для Render (чтобы сервис не падал)
app.get('/', (req, res) => {
  res.send('Localhaus Bot is active and running on webhook!');
});

app.listen(PORT, async () => {
  console.log(`Server is running on port ${PORT}`);
  if (RENDER_EXTERNAL_URL) {
    const webhookUrl = `${RENDER_EXTERNAL_URL}${WEBHOOK_PATH}`;
    await bot.telegram.setWebhook(webhookUrl);
    console.log(`Webhook set to: ${webhookUrl}`);
  } else {
    console.log('RENDER_EXTERNAL_URL is not set. Webhook cannot be auto-configured without domain.');
  }
});
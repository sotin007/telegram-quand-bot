import os
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# ===== ТВОЙ ТЕКСТ =====
RULES_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)

WELCOME_TEXT = "👋 {mention}\n\n" + RULES_TEXT

DELETE_QRAND_AFTER_SECONDS = 5
DELETE_WELCOME_AFTER_SECONDS = 30  # 👈 вот это новое

# ===== УТИЛИТЫ =====
async def is_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except:
        return False

async def delete_later(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except:
        pass

def mention_html(user) -> str:
    name = (user.full_name or "пользователь").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user.id}">{name}</a>'

# ===== ХЭНДЛЕРЫ =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Я мод-бот.\n"
        "✅ Приветствую новичков (удаляю через 30 сек)\n"
        "✅ Кнопка бана на вышедших\n"
        "✅ Удаляю /qrand через 5 секунд\n\n"
        "Команда: /rules"
    )

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(RULES_TEXT)

async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return

    for u in msg.new_chat_members:
        sent = await msg.reply_text(
            WELCOME_TEXT.format(mention=mention_html(u)),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

        # 👇 удаляем приветствие через 30 секунд
        context.application.create_task(
            delete_later(context, sent.chat_id, sent.message_id, DELETE_WELCOME_AFTER_SECONDS)
        )

async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.left_chat_member:
        return

    left = msg.left_chat_member

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"🚫 Забанить {left.full_name}",
            callback_data=f"ban:{left.id}"
        )
    ]])

    await msg.reply_text(
        f"👋 {left.full_name} вышел(ла) из чата.\n"
        f"Если это был спамер — можно забанить кнопкой ниже.",
        reply_markup=kb
    )

async def on_ban_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    chat_id = q.message.chat_id
    clicker_id = q.from_user.id

    if not await is_admin(chat_id, clicker_id, context):
        await q.answer("Только админы могут банить.", show_alert=True)
        return

    try:
        target_id = int((q.data or "").split("ban:", 1)[1])
    except:
        return

    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=target_id)
        await q.message.edit_text("✅ Забанен.")
    except:
        await q.message.edit_text("❌ Не смог забанить. Проверь права бота.")

async def on_qrand_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not (msg.text or msg.caption):
        return

    txt = (msg.text or msg.caption or "").strip()

    if not (txt.startswith("/qrand") or txt.startswith("/qrand@")):
        return

    context.application.create_task(
        delete_later(context, msg.chat_id, msg.message_id, DELETE_QRAND_AFTER_SECONDS)
    )

# ===== ЗАПУСК =====
def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан.")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))

    app.add_handler(MessageHandler(filters.COMMAND, on_qrand_spam))
    app.add_handler(MessageHandler(filters.TEXT, on_qrand_spam))

    app.add_handler(CallbackQueryHandler(on_ban_button, pattern=r"^ban:"))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

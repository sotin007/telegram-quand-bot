import os
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters
)

TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# ===== ТВОИ ПРАВИЛА =====
RULES_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)
WELCOME_TEXT = "👋 {mention}\n\n" + RULES_TEXT

DELETE_QRAND_AFTER_SECONDS = 5
DELETE_WELCOME_AFTER_SECONDS = 30

# ===== УТИЛИТЫ =====
def mention_html(user) -> str:
    name = (user.full_name or "пользователь").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user.id}">{name}</a>'

async def delete_later(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except:
        pass

async def is_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except:
        return False

# ===== БАЗА =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Я мод-бот.\n"
        "✅ Приветствие+правила (удаляю через 30 сек)\n"
        "✅ Кнопка бана на вышедших\n"
        "✅ /qrand удаляю через 5 сек\n"
        "✅ Титулы: /nick <до 16 символов>, /unnick\n\n"
        "Команда: /rules"
    )

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(RULES_TEXT)

# ===== ПРИВЕТСТВИЕ =====
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
        context.application.create_task(
            delete_later(context, sent.chat_id, sent.message_id, DELETE_WELCOME_AFTER_SECONDS)
        )

# ===== УШЁЛ: КНОПКА БАН =====
async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.left_chat_member:
        return

    left = msg.left_chat_member
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🚫 Забанить {left.full_name}", callback_data=f"ban:{left.id}")
    ]])

    await msg.reply_text(
        f"👋 {left.full_name} вышел(ла) из чата.\nЕсли это спамер — можно забанить.",
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
        await q.answer("Ошибка кнопки.", show_alert=True)
        return

    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=target_id)
        await q.message.edit_text("✅ Забанен.")
    except Exception as e:
        await q.message.edit_text(f"❌ Не смог забанить. Проверь права бота.\n{type(e).__name__}")

# ===== АНТИ /qrand =====
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

# ===== /nick = CUSTOM ADMIN TITLE =====
async def nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /nick Невеста ⚡ -> делает автора админом с минимумом прав и ставит custom title
    """
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type != "supergroup":
        await msg.reply_text("❌ /nick работает только в супергруппе (не в обычной группе).")
        return

    if not context.args:
        await msg.reply_text("Использование: /nick ТвойНик (до 16 символов)")
        return

    title = " ".join(context.args).strip()
    if len(title) > 16:
        await msg.reply_text("❌ Ник слишком длинный. Максимум 16 символов.")
        return

    # Проверим, что бот админ
    me = await context.bot.get_me()
    if not await is_admin(chat.id, me.id, context):
        await msg.reply_text("❌ Сделай бота админом с правом 'Добавлять администраторов'.")
        return

    try:
        # Повышаем (минимально), чтобы был админ и можно было поставить title
        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            can_manage_chat=True,          # минимум
            can_delete_messages=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False,
            can_manage_video_chats=False,
            can_manage_topics=False,
        )

        # Ставим кастомный титул
        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat.id,
            user_id=user.id,
            custom_title=title
        )

        await msg.reply_text(f"✅ Ник установлен: {title}")

    except Exception as e:
        await msg.reply_text(
            "❌ Не получилось поставить ник.\n"
            "Проверь:\n"
            "1) Бот админ и может добавлять админов\n"
            "2) Это супергруппа\n"
            "3) Ты не владелец чата (owner)\n"
            f"\nОшибка: {type(e).__name__}"
        )

async def unnick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /unnick -> снять титул (разжаловать)
    """
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type != "supergroup":
        await msg.reply_text("❌ /unnick работает только в супергруппе.")
        return

    try:
        # Демот: все флаги False
        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            can_manage_chat=False,
            can_delete_messages=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False,
            can_manage_video_chats=False,
            can_manage_topics=False,
        )
        await msg.reply_text("✅ Ник снят (админство убрано).")
    except Exception as e:
        await msg.reply_text(f"❌ Не смог снять. Проверь права бота.\n{type(e).__name__}")

# ===== ЗАПУСК =====
def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Добавь переменную окружения BOT_TOKEN.")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules))

    app.add_handler(CommandHandler("nick", nick))
    app.add_handler(CommandHandler("unnick", unnick))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))

    app.add_handler(MessageHandler(filters.COMMAND, on_qrand_spam))
    app.add_handler(MessageHandler(filters.TEXT, on_qrand_spam))

    app.add_handler(CallbackQueryHandler(on_ban_button, pattern=r"^ban:"))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

import os
import json
import asyncio
import feedparser
from typing import List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest

TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# ===== НАСТРОЙКИ =====
RULES_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)
WELCOME_TEXT = "👋 {mention}\n\n" + RULES_TEXT

DELETE_WELCOME_AFTER_SECONDS = 30
DELETE_QRAND_AFTER_SECONDS = 5

RSS_URL = os.environ.get("RSS_URL", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
RSS_POLL_SECONDS = int(os.environ.get("RSS_POLL_SECONDS", "120"))
RSS_STATE_FILE = "rss_state.json"


# =======================
# УТИЛИТЫ
# =======================

def mention_html(user) -> str:
    name = (user.full_name or "пользователь").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


async def delete_later(context, chat_id, message_id, delay):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id, message_id)
    except:
        pass


async def is_admin(chat_id, user_id, context):
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except:
        return False


def load_state():
    if os.path.exists(RSS_STATE_FILE):
        with open(RSS_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(RSS_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


# =======================
# КОМАНДЫ
# =======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот работает 😎")


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RULES_TEXT)


async def nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("❌ Только в группе.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /nick ТвойНик")
        return

    new_nick = " ".join(context.args).strip()

    if len(new_nick) > 16:
        await update.message.reply_text("❌ Максимум 16 символов.")
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    try:
        await context.bot.promote_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            can_manage_chat=False,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False,
            can_post_stories=False,
            can_edit_stories=False,
            can_delete_stories=False,
        )

        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat_id,
            user_id=user_id,
            custom_title=new_nick
        )

        await update.message.reply_text(f"✅ Ник установлен: {new_nick}")

    except BadRequest:
        await update.message.reply_text("❌ Бот должен быть админом с правом назначать администраторов.")


async def rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.application.bot_data.get("rss_state", load_state())
    await update.message.reply_text(
        f"RSS: {'✅' if RSS_URL else '❌'}\n"
        f"CHANNEL: {CHANNEL_ID}\n"
        f"Last ID: {state.get('last_id', '(пусто)')}"
    )


# =======================
# СОБЫТИЯ
# =======================

async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for user in update.message.new_chat_members:
        sent = await update.message.reply_text(
            WELCOME_TEXT.format(mention=mention_html(user)),
            parse_mode="HTML"
        )
        context.application.create_task(
            delete_later(context, sent.chat_id, sent.message_id, DELETE_WELCOME_AFTER_SECONDS)
        )


async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.left_chat_member

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"🚫 Забанить {user.full_name}",
            callback_data=f"ban:{user.id}"
        )
    ]])

    await update.message.reply_text(
        f"{user.full_name} вышел из чата.",
        reply_markup=kb
    )


async def on_ban_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.message.chat_id, query.from_user.id, context):
        await query.answer("Только админ.", show_alert=True)
        return

    user_id = int(query.data.split(":")[1])
    await context.bot.ban_chat_member(query.message.chat_id, user_id)
    await query.message.edit_text("✅ Забанен.")


async def on_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if text.startswith("/qrand"):
        context.application.create_task(
            delete_later(context, update.message.chat_id, update.message.message_id, DELETE_QRAND_AFTER_SECONDS)
        )


# =======================
# RSS (без дублей)
# =======================

async def rss_tick(context: ContextTypes.DEFAULT_TYPE):
    if not RSS_URL or not CHANNEL_ID:
        return

    state = context.application.bot_data.setdefault("rss_state", load_state())
    last_id = state.get("last_id")

    feed = feedparser.parse(RSS_URL)
    entries = feed.entries

    if not entries:
        return

    # первый запуск — просто запоминаем последний
    if not last_id:
        state["last_id"] = entries[0].get("id") or entries[0].get("link")
        save_state(state)
        return

    new_posts = []
    for entry in entries:
        entry_id = entry.get("id") or entry.get("link")
        if entry_id == last_id:
            break
        new_posts.append(entry)

    if not new_posts:
        return

    new_posts.reverse()

    for entry in new_posts:
        entry_id = entry.get("id") or entry.get("link")
        caption = (entry.get("title", "") + "\n\n" + entry.get("link", "")).strip()

        try:
            await context.bot.send_message(CHANNEL_ID, caption)
            state["last_id"] = entry_id
            save_state(state)
        except:
            pass


# =======================
# MAIN
# =======================

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("nick", nick))
    app.add_handler(CommandHandler("rssstatus", rssstatus))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))
    app.add_handler(MessageHandler(filters.TEXT & filters.COMMAND, on_qrand))
    app.add_handler(CallbackQueryHandler(on_ban_button, pattern="^ban:"))

    if RSS_URL and CHANNEL_ID:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)

    app.run_polling()


if __name__ == "__main__":
    main()

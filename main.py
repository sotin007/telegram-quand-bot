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
        try:
            with open(RSS_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_state(state):
    try:
        with open(RSS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except:
        pass


# =======================
# КОМАНДЫ
# =======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот работает 😎\nКоманды: /rules /nick /rssstatus")


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RULES_TEXT)


async def nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /nick <до 16 символов> — ставит custom admin title.
    Работает только в supergroup.
    """
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type != "supergroup":
        await msg.reply_text("❌ /nick работает только в супергруппе.")
        return

    if not context.args:
        await msg.reply_text("Использование: /nick ТвойНик (до 16 символов)")
        return

    title = " ".join(context.args).strip()
    if len(title) > 16:
        await msg.reply_text("❌ Ник слишком длинный. Максимум 16 символов.")
        return

    # Проверим, что бот админ и может назначать админов
    me = await context.bot.get_me()
    try:
        my_member = await context.bot.get_chat_member(chat.id, me.id)
        if my_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await msg.reply_text("❌ Бот не админ. Дай ему админку в чате.")
            return
    except:
        pass

    try:
        # Делаем юзера админом с МИНИМАЛЬНЫМ правом (иначе title не поставить)
        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            can_manage_chat=True,      # 👈 минимально, чтобы считался админом
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False,
            can_manage_topics=False,
        )

        # Ставим титул
        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat.id,
            user_id=user.id,
            custom_title=title
        )

        await msg.reply_text(f"✅ Ник установлен: {title}")

    except BadRequest as e:
        # Самые частые причины:
        # - бот не имеет права "Добавлять администраторов"
        # - пользователя нельзя редактировать (owner/админ не от бота)
        await msg.reply_text(
            "❌ Не получилось поставить ник.\n"
            "Проверь:\n"
            "1) бот админ и включено право «Добавлять администраторов»\n"
            "2) ты не владелец чата\n"
            "3) если ты уже админ, то бот должен иметь право тебя редактировать\n"
            f"\nОшибка: {e.message if hasattr(e,'message') else str(e)}"
        )


async def rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.application.bot_data.get("rss_state") or load_state()
    await update.message.reply_text(
        f"RSS_URL: {'✅' if RSS_URL else '❌'}\n"
        f"CHANNEL_ID: {CHANNEL_ID or '❌'}\n"
        f"poll: {RSS_POLL_SECONDS}s\n"
        f"last_id: {state.get('last_id', '') or '(пусто)'}"
    )


# =======================
# СОБЫТИЯ
# =======================

async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for u in update.effective_message.new_chat_members:
        sent = await update.effective_message.reply_text(
            WELCOME_TEXT.format(mention=mention_html(u)),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        context.application.create_task(
            delete_later(context, sent.chat_id, sent.message_id, DELETE_WELCOME_AFTER_SECONDS)
        )


async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    left = update.effective_message.left_chat_member

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"🚫 Забанить {left.full_name}",
            callback_data=f"ban:{left.id}"
        )
    ]])

    await update.effective_message.reply_text(
        f"{left.full_name} вышел из чата.",
        reply_markup=kb
    )


async def on_ban_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await is_admin(q.message.chat_id, q.from_user.id, context):
        await q.answer("Только админ.", show_alert=True)
        return

    user_id = int(q.data.split(":")[1])
    try:
        await context.bot.ban_chat_member(q.message.chat_id, user_id)
        await q.message.edit_text("✅ Забанен.")
    except Exception as e:
        await q.message.edit_text(f"❌ Не смог забанить: {type(e).__name__}")


async def on_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.effective_message.text or "").strip()
    if txt.startswith("/qrand") or txt.startswith("/qrand@"):
        context.application.create_task(
            delete_later(context, update.effective_message.chat_id, update.effective_message.message_id, DELETE_QRAND_AFTER_SECONDS)
        )


# =======================
# RSS (без дублей)
# =======================

async def rss_tick(context: ContextTypes.DEFAULT_TYPE):
    if not RSS_URL or not CHANNEL_ID:
        return

    state = context.application.bot_data.setdefault("rss_state", load_state())
    last_id = state.get("last_id", "")

    feed = feedparser.parse(RSS_URL)
    entries = getattr(feed, "entries", []) or []
    if not entries:
        return

    # первый запуск — запоминаем самый свежий, не спамим старым
    if not last_id:
        newest = entries[0].get("id") or entries[0].get("link")
        if newest:
            state["last_id"] = newest
            save_state(state)
        return

    new_posts = []
    for entry in entries:
        eid = entry.get("id") or entry.get("link")
        if not eid:
            continue
        if eid == last_id:
            break
        new_posts.append(entry)

    if not new_posts:
        return

    new_posts.reverse()

    for entry in new_posts:
        eid = entry.get("id") or entry.get("link")
        caption = (entry.get("title") or entry.get("summary") or "").strip()
        link = (entry.get("link") or "").strip()
        text = (caption + "\n\n" + link).strip()

        try:
            await context.bot.send_message(CHANNEL_ID, text[:4096] if text else link)
            state["last_id"] = eid
            save_state(state)
        except:
            pass


# =======================
# MAIN
# =======================

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан.")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("nick", nick))
    app.add_handler(CommandHandler("rssstatus", rssstatus))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))

    app.add_handler(MessageHandler(filters.COMMAND, on_qrand))
    app.add_handler(MessageHandler(filters.TEXT, on_qrand))

    app.add_handler(CallbackQueryHandler(on_ban_button, pattern=r"^ban:"))

    if RSS_URL and CHANNEL_ID:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
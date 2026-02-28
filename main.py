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

TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# ===== MOD SETTINGS =====
RULES_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)
WELCOME_TEXT = "👋 {mention}\n\n" + RULES_TEXT

DELETE_WELCOME_AFTER_SECONDS = 30
DELETE_QRAND_AFTER_SECONDS = 5

# ===== RSS -> CHANNEL SETTINGS =====
RSS_URL = os.environ.get("RSS_URL", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()  # @yomabar
RSS_POLL_SECONDS = int(os.environ.get("RSS_POLL_SECONDS", "120"))
RSS_STATE_FILE = "rss_state.json"


# ---------- helpers ----------
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


def load_state() -> dict:
    if os.path.exists(RSS_STATE_FILE):
        try:
            with open(RSS_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_state(state: dict):
    try:
        with open(RSS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except:
        pass


def entry_id(e) -> str:
    return getattr(e, "id", "") or getattr(e, "guid", "") or getattr(e, "link", "") or ""


def entry_link(e) -> str:
    return getattr(e, "link", "") or ""


def entry_caption(e) -> str:
    for k in ("title", "summary", "description"):
        v = getattr(e, k, "")
        if v:
            return v
    return ""


def entry_images(e) -> List[str]:
    urls = []

    mc = getattr(e, "media_content", None)
    if mc:
        for m in mc:
            u = m.get("url")
            if u:
                urls.append(u)

    mt = getattr(e, "media_thumbnail", None)
    if mt:
        for m in mt:
            u = m.get("url")
            if u:
                urls.append(u)

    enc = getattr(e, "enclosures", None)
    if enc:
        for it in enc:
            u = it.get("href") or it.get("url")
            if u:
                urls.append(u)

    # dedupe
    out = []
    seen = set()
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out[:10]


# ---------- commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Я мод-бот + постинг из Instagram RSS.\n"
        "✅ Приветствие+правила (удаляю через 30 сек)\n"
        "✅ Кнопка бана на ушедших\n"
        "✅ /qrand удаляю через 5 сек\n"
        "✅ RSS -> канал\n\n"
        "Команды: /rules, /rssstatus"
    )


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(RULES_TEXT)


async def rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = context.application.bot_data.get("rss_state") or load_state()
    await update.effective_message.reply_text(
        f"RSS_URL: {'✅' if RSS_URL else '❌'}\n"
        f"CHANNEL_ID: {CHANNEL_ID or '❌'}\n"
        f"poll: {RSS_POLL_SECONDS}s\n"
        f"last_id: {st.get('last_id', '') or '(пусто)'}"
    )


# ---------- welcome / left ----------
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


# ---------- anti /qrand ----------
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


# ---------- RSS job ----------
async def rss_tick(context: ContextTypes.DEFAULT_TYPE):
    if not RSS_URL or not CHANNEL_ID:
        return

    state = context.application.bot_data.setdefault("rss_state", load_state())
    last_id = state.get("last_id", "")

    feed = feedparser.parse(RSS_URL)
    entries = getattr(feed, "entries", []) or []
    if not entries:
        return

    new_entries = []
    for e in entries:
        eid = entry_id(e)
        if not eid:
            continue
        if eid == last_id:
            break
        new_entries.append(e)

    if not new_entries:
        return

    new_entries.reverse()  # старые -> новые

    for e in new_entries:
        eid = entry_id(e)
        link = entry_link(e)
        caption = (entry_caption(e) or "").strip()
        if link:
            caption = (caption + "\n\n" + link).strip()

        imgs = entry_images(e)

        try:
            if imgs:
                if len(imgs) > 1:
                    media = []
                    for i, url in enumerate(imgs[:10]):
                        if i == 0 and caption:
                            media.append(InputMediaPhoto(media=url, caption=caption[:1024]))
                        else:
                            media.append(InputMediaPhoto(media=url))
                    await context.bot.send_media_group(chat_id=CHANNEL_ID, media=media)
                else:
                    await context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=imgs[0],
                        caption=caption[:1024] if caption else None
                    )
            else:
                if caption:
                    await context.bot.send_message(chat_id=CHANNEL_ID, text=caption[:4096])

            state["last_id"] = eid
            save_state(state)

        except:
            # если конкретный пост не отправился — пропускаем
            pass


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан.")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("rssstatus", rssstatus))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))

    app.add_handler(MessageHandler(filters.COMMAND, on_qrand_spam))
    app.add_handler(MessageHandler(filters.TEXT, on_qrand_spam))

    app.add_handler(CallbackQueryHandler(on_ban_button, pattern=r"^ban:"))

    if RSS_URL and CHANNEL_ID:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

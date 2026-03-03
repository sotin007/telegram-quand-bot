import asyncio
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple

import feedparser
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMemberUpdated,
    MessageEntity,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ----------------------------
# CONFIG (env)
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@yomabar").strip()  # @channel or -100...
RSS_URL = os.getenv("RSS_URL", "").strip()
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "120"))
DELETE_QRAND_AFTER_SECONDS = int(os.getenv("DELETE_QRAND_AFTER_SECONDS", "30"))

# local persistent state file (inside container)
STATE_FILE = Path("state.json")

# ----------------------------
# TEXTS
# ----------------------------
WELCOME_RULES_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)

START_TEXT = (
    "Бот работает 😎\n"
    "Команды: /rules /nick /rssstatus /ping\n"
    f"Авто: удаляет /qrand через {DELETE_QRAND_AFTER_SECONDS}с, приветствует новых, "
    "бан-кнопка после выхода, RSS→канал, "
    "и пробует превращать ссылки Instagram/TikTok в видео/фото."
)

RULES_TEXT = WELCOME_RULES_TEXT

# ----------------------------
# LOGGING
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("yomabar-bot")


# ----------------------------
# STATE
# ----------------------------
@dataclass
class BotState:
    last_rss_id: Optional[str] = None
    last_rss_link: Optional[str] = None

    def to_dict(self) -> dict:
        return {"last_rss_id": self.last_rss_id, "last_rss_link": self.last_rss_link}

    @staticmethod
    def from_dict(d: dict) -> "BotState":
        return BotState(
            last_rss_id=d.get("last_rss_id"),
            last_rss_link=d.get("last_rss_link"),
        )


STATE = BotState()


def load_state() -> None:
    global STATE
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            STATE = BotState.from_dict(data or {})
            log.info("State loaded: %s", STATE.to_dict())
    except Exception as e:
        log.warning("Failed to load state: %s", e)


def save_state() -> None:
    try:
        STATE_FILE.write_text(json.dumps(STATE.to_dict(), ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning("Failed to save state: %s", e)


# ----------------------------
# HELPERS
# ----------------------------
def is_supergroup(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in (ChatType.SUPERGROUP, ChatType.GROUP))


def extract_urls(message_text: str, entities: Optional[List[MessageEntity]]) -> List[str]:
    urls: List[str] = []
    if not message_text:
        return urls

    # From entities (most accurate)
    if entities:
        for ent in entities:
            if ent.type in ("url", "text_link"):
                if ent.type == "text_link" and ent.url:
                    urls.append(ent.url)
                elif ent.type == "url":
                    part = message_text[ent.offset : ent.offset + ent.length]
                    urls.append(part)

    # Fallback regex (in case no entities)
    rgx = re.findall(r"(https?://[^\s]+)", message_text)
    urls.extend(rgx)

    # cleanup duplicates
    out = []
    seen = set()
    for u in urls:
        u = u.strip().strip(").,]")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


INSTAGRAM_RE = re.compile(r"(https?://(www\.)?instagram\.com/[^\s]+)", re.IGNORECASE)
TIKTOK_RE = re.compile(r"(https?://(www\.)?(vm\.)?tiktok\.com/[^\s]+)", re.IGNORECASE)


def is_instagram(url: str) -> bool:
    return bool(INSTAGRAM_RE.search(url))


def is_tiktok(url: str) -> bool:
    return bool(TIKTOK_RE.search(url))


def is_qrand(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    # /qrand or /qrand@bot
    return bool(re.match(r"^/qrand(@[A-Za-z0-9_]+)?(\s|$)", t))


async def safe_delete_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass


async def schedule_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, seconds: int) -> None:
    await asyncio.sleep(seconds)
    await safe_delete_message(context, chat_id, message_id)


# ----------------------------
# COMMANDS
# ----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(START_TEXT)


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(RULES_TEXT)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"pong ✅\nchat_type={chat.type}\nchat_id={chat.id}"
    )


async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"RSS_URL: {'OK' if RSS_URL else 'НЕ ЗАДАН'}\n"
        f"CHANNEL: {CHANNEL_USERNAME}\n"
        f"POLL: {RSS_POLL_SECONDS}s\n"
        f"last_rss_id: {STATE.last_rss_id}\n"
        f"last_rss_link: {STATE.last_rss_link}"
    )


async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type != ChatType.SUPERGROUP:
        await msg.reply_text("❌ /nick работает только в супер-группе.")
        return

    if not context.args:
        await msg.reply_text("Использование: /nick <титул до 16 символов>")
        return

    title = " ".join(context.args).strip()
    if len(title) > 16:
        await msg.reply_text("❌ Слишком длинно. Максимум 16 символов.")
        return

    # Bot must be admin; user must be admin (Telegram restriction for setting admin title)
    try:
        me = await context.bot.get_me()
        bot_member = await context.bot.get_chat_member(chat.id, me.id)
        if bot_member.status not in ("administrator", "creator"):
            await msg.reply_text("❌ Бот должен быть админом (с правом управлять админами).")
            return

        u_member = await context.bot.get_chat_member(chat.id, user.id)
        if u_member.status not in ("administrator", "creator"):
            await msg.reply_text("❌ Ты должен быть админом, чтобы поставить себе титул.")
            return

        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat.id,
            user_id=user.id,
            custom_title=title,
        )
        await msg.reply_text(f"✅ Титул установлен: «{title}»")
    except BadRequest as e:
        await msg.reply_text(f"❌ Не получилось: {e.message}")
    except TelegramError as e:
        await msg.reply_text(f"❌ Ошибка Telegram: {e}")


# ----------------------------
# WELCOME + LEAVE BAN BUTTON
# ----------------------------
def _member_joined(change: ChatMemberUpdated) -> bool:
    return change.old_chat_member.status in ("left", "kicked") and change.new_chat_member.status in ("member", "administrator", "creator")


def _member_left(change: ChatMemberUpdated) -> bool:
    return change.old_chat_member.status in ("member", "administrator", "creator") and change.new_chat_member.status in ("left",)


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.chat_member:
        return

    change = update.chat_member
    chat = change.chat

    # Welcome
    if _member_joined(change):
        user = change.new_chat_member.user
        name = (user.full_name or "новичок").strip()
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"👋 {name}\n\n{WELCOME_RULES_TEXT}",
        )
        return

    # Left -> ban button
    if _member_left(change):
        user = change.old_chat_member.user
        name = (user.full_name or "пользователь").strip()
        kb = InlineKeyboardMarkup.from_button(
            InlineKeyboardButton(
                text=f"🚫 Забанить {name}",
                callback_data=f"ban:{chat.id}:{user.id}",
            )
        )
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"🚪 {name} вышел(а). Если это спамер — можно забанить:",
            reply_markup=kb,
        )


async def on_ban_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = (q.data or "").split(":")
    if len(data) != 3 or data[0] != "ban":
        return

    chat_id = int(data[1])
    user_id = int(data[2])

    # Only admins can press
    try:
        presser = q.from_user
        member = await context.bot.get_chat_member(chat_id, presser.id)
        if member.status not in ("administrator", "creator"):
            await q.edit_message_text("❌ Только админы могут банить.")
            return
    except TelegramError:
        await q.edit_message_text("❌ Не смог проверить права.")
        return

    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await q.edit_message_text("✅ Забанен.")
    except BadRequest as e:
        await q.edit_message_text(f"❌ Не получилось: {e.message}")
    except TelegramError as e:
        await q.edit_message_text(f"❌ Ошибка Telegram: {e}")


# ----------------------------
# /qrand delete
# ----------------------------
async def on_message_delete_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return
    if is_qrand(msg.text):
        # delete after N seconds
        context.application.create_task(
            schedule_delete(context, msg.chat_id, msg.message_id, DELETE_QRAND_AFTER_SECONDS)
        )


# ----------------------------
# RSS -> CHANNEL
# ----------------------------
async def rss_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not RSS_URL or not CHANNEL_USERNAME:
        return

    try:
        feed = feedparser.parse(RSS_URL)
        entries = list(getattr(feed, "entries", []) or [])
        if not entries:
            return

        # newest first from rss.app usually; we want oldest-first posting
        # build list until we hit last posted
        to_post = []
        for e in entries:
            eid = getattr(e, "id", None) or getattr(e, "guid", None) or getattr(e, "link", None)
            link = getattr(e, "link", None)
            if STATE.last_rss_id and eid == STATE.last_rss_id:
                break
            if STATE.last_rss_link and link and link == STATE.last_rss_link:
                break
            to_post.append(e)

        if not to_post:
            return

        to_post.reverse()  # post oldest first

        for e in to_post:
            title = (getattr(e, "title", "") or "").strip()
            link = (getattr(e, "link", "") or "").strip()

            # Try image from rss.app (common fields)
            img = None
            media_content = getattr(e, "media_content", None)
            if media_content and isinstance(media_content, list) and media_content:
                img = media_content[0].get("url")
            if not img:
                # sometimes in summary as <img src=...>
                summary = getattr(e, "summary", "") or ""
                m = re.search(r'<img[^>]+src="([^"]+)"', summary)
                if m:
                    img = m.group(1)

            caption = ""
            if title:
                caption += f"<b>{title}</b>\n"
            if link:
                caption += f"\n{link}\n"
            caption += (
                "\n<a href=\"https://www.instagram.com/yomabar.lt\">Instagram</a> | "
                "<a href=\"https://www.facebook.com/share/1P3dFJ5f5Y/?mibextid=wwXIfr\">Facebook</a> | "
                "<a href=\"https://www.yomahayoma.show/\">Сайт</a>"
            )

            try:
                if img:
                    await context.bot.send_photo(
                        chat_id=CHANNEL_USERNAME,
                        photo=img,
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await context.bot.send_message(
                        chat_id=CHANNEL_USERNAME,
                        text=caption,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=False,
                    )
            except TelegramError as te:
                log.warning("RSS send failed: %s", te)

            # update state after each post
            STATE.last_rss_id = getattr(e, "id", None) or getattr(e, "guid", None) or getattr(e, "link", None)
            STATE.last_rss_link = getattr(e, "link", None)
            save_state()

    except Exception as e:
        log.warning("rss_tick error: %s", e)


# ----------------------------
# IG / TikTok downloader (yt-dlp, no ffmpeg)
# ----------------------------
async def download_with_ytdlp(url: str, workdir: Path) -> Tuple[List[Path], Optional[str]]:
    """
    Returns (files, error_text). If error_text is not None => failed.
    """
    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except Exception as e:
        return [], f"yt-dlp не установлен: {e}"

    outtmpl = str(workdir / "%(title).80s_%(id)s.%(ext)s")

    # IMPORTANT: no merging formats => avoids ffmpeg requirement
    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "socket_timeout": 20,
        "format": "best[ext=mp4]/best",  # single best
        "postprocessors": [],  # avoid ffmpeg
        "merge_output_format": None,
        "overwrites": True,
    }

    def _run():
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return info

    try:
        info = await asyncio.to_thread(_run)
    except Exception as e:
        return [], str(e)

    # Collect downloaded files in workdir
    files = sorted([p for p in workdir.glob("*") if p.is_file() and p.stat().st_size > 0])

    if not files:
        return [], "Скачалось 0 файлов (возможно защита или нет медиа)."

    # limit: telegram bots max upload depends; keep safer by not huge
    # We'll attempt anyway; Telegram may reject big files -> handled later
    return files, None


async def send_media_files(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    files: List[Path],
    reply_to_message_id: int,
) -> Optional[str]:
    """
    Sends as album if multiple, else single. Returns error text if failed.
    """
    chat_id = update.effective_chat.id

    # classify
    photos = [f for f in files if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
    videos = [f for f in files if f.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm")]

    try:
        # If there are multiple photos/videos - send album (up to 10)
        media = []
        for f in (photos + videos)[:10]:
            data = f.read_bytes()
            if f in photos:
                media.append(InputMediaPhoto(media=data))
            else:
                media.append(InputMediaVideo(media=data))
        if len(media) >= 2:
            await context.bot.send_media_group(
                chat_id=chat_id,
                media=media,
                reply_to_message_id=reply_to_message_id,
            )
            return None

        # Single file
        if photos:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=photos[0].read_bytes(),
                reply_to_message_id=reply_to_message_id,
            )
            return None
        if videos:
            await context.bot.send_video(
                chat_id=chat_id,
                video=videos[0].read_bytes(),
                reply_to_message_id=reply_to_message_id,
                supports_streaming=True,
            )
            return None

        # fallback: send as document
        await context.bot.send_document(
            chat_id=chat_id,
            document=files[0].read_bytes(),
            reply_to_message_id=reply_to_message_id,
        )
        return None

    except BadRequest as e:
        return f"{e.message}"
    except TelegramError as e:
        return f"{e}"


async def on_message_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return

    text = msg.text or msg.caption or ""
    urls = extract_urls(text, msg.entities or msg.caption_entities)
    if not urls:
        return

    target_urls = [u for u in urls if is_instagram(u) or is_tiktok(u)]
    if not target_urls:
        return

    # process first matching URL only (to avoid spam)
    url = target_urls[0]

    # quick “working” message
    status = await msg.reply_text("⏳ Пытаюсь скачать медиа…")

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        files, err = await download_with_ytdlp(url, workdir)

        if err:
            await status.edit_text(
                "❌ Не получилось.\n"
                "Причина: Не получилось скачать. Возможно защита/блокировка или ссылка странная.\n\n"
                f"Тех.деталь: {err}"
            )
            return

        # try send
        send_err = await send_media_files(update, context, files, reply_to_message_id=msg.message_id)

        if send_err:
            await status.edit_text(
                "❌ Не получилось отправить в Telegram.\n"
                f"Причина: {send_err}"
            )
            return

        # success -> remove status
        try:
            await status.delete()
        except TelegramError:
            pass


# ----------------------------
# APP
# ----------------------------
def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty")

    app = Application.builder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("nick", cmd_nick))
    app.add_handler(CommandHandler("rssstatus", cmd_rssstatus))
    app.add_handler(CommandHandler("ping", cmd_ping))

    # ban button
    app.add_handler(CallbackQueryHandler(on_ban_button, pattern=r"^ban:"))

    # welcome/leave
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # delete /qrand after N sec
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_delete_qrand))
    # also catch /qrand as command message (since it starts with /)
    app.add_handler(MessageHandler(filters.COMMAND, on_message_delete_qrand))

    # link downloader (text or captions)
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, on_message_links))

    return app


def main() -> None:
    load_state()
    app = build_app()

    # RSS job
    if RSS_URL and CHANNEL_USERNAME:
        if app.job_queue is None:
            log.warning("JobQueue is None. Install python-telegram-bot[job-queue].")
        else:
            app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)
            log.info("RSS polling enabled: %s every %ss", RSS_URL, RSS_POLL_SECONDS)

    log.info("Starting bot…")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,  # helps after restarts
    )


if __name__ == "__main__":
    main()
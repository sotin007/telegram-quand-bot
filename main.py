import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

import feedparser
import httpx
from yt_dlp import YoutubeDL

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DELETE_QRAND_AFTER_SECONDS = int(os.getenv("DELETE_QRAND_AFTER_SECONDS", "30"))

RULES_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)

PHOTO_SORRY_TEXT = "Сори брат да? Я ещё не умею качать фотки, давай как то без меня, всё пока 👋"
BAN_PREFIX = "banleft:"

# RSS / auto-posts to channel
RSS_URL = os.getenv("RSS_URL", "").strip()
RSS_CHANNEL_ID = os.getenv("RSS_CHANNEL_ID", "").strip()  # example: -1001234567890
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "180"))
BTN_INSTAGRAM = os.getenv("BTN_INSTAGRAM", "").strip()
BTN_FACEBOOK = os.getenv("BTN_FACEBOOK", "").strip()
BTN_SITE = os.getenv("BTN_SITE", "").strip()
RSS_STATE_FILE = os.getenv("RSS_STATE_FILE", "rss_state.json").strip()

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("yomabar-bot")

# =========================
# REGEX
# =========================
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
IG_RE = re.compile(r"https?://(www\.)?instagram\.com/\S+", re.IGNORECASE)
TT_RE = re.compile(r"https?://(vm\.)?tiktok\.com/\S+|https?://www\.tiktok\.com/\S+", re.IGNORECASE)
TT_PHOTO_HINT_RE = re.compile(r"/photo/", re.IGNORECASE)
QRAND_RE = re.compile(r"^/qrand(@\w+)?(\s|$)", re.IGNORECASE)

# =========================
# HELPERS
# =========================
def extract_urls(text: str) -> List[str]:
    return URL_RE.findall(text or "")

async def resolve_final_url(url: str) -> str:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=10.0,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            r = await client.get(url)
            return str(r.url)
    except Exception:
        return url

def ytdlp_options(outtmpl: str, url: str) -> dict:
    fmt = "best"
    if "tiktok.com" in url:
        fmt = "best[ext=mp4]/best"

    return {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "socket_timeout": 20,
        "format": fmt,
        "merge_output_format": None,
        "postprocessors": [],
        "overwrites": True,
        "restrictfilenames": False,
    }

def pick_downloaded_files(folder: Path) -> List[Path]:
    files = []
    for p in folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in [".mp4", ".mov", ".mkv", ".webm", ".m4v", ".jpg", ".jpeg", ".png"]:
            files.append(p)
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return files

async def download_media_from_url(url: str) -> Tuple[List[Path], Optional[str]]:
    base = Path(tempfile.mkdtemp(prefix="yomabar_dl_"))
    outtmpl = str(base / "%(title).80s_%(id)s.%(ext)s")

    def _blocking() -> Tuple[List[Path], Optional[str]]:
        try:
            with YoutubeDL(ytdlp_options(outtmpl, url)) as ydl:
                ydl.extract_info(url, download=True)
            files = pick_downloaded_files(base)
            if not files:
                return [], "Не нашёл скачанный файл."
            return files, None
        except Exception as e:
            return [], str(e)

    files, err = await asyncio.to_thread(_blocking)

    if err:
        try:
            for p in base.rglob("*"):
                if p.is_file():
                    p.unlink(missing_ok=True)
            base.rmdir()
        except Exception:
            pass
        return [], err

    return files, None

async def cleanup_files(files: List[Path]):
    if not files:
        return
    try:
        base = files[0].parent
        for p in base.rglob("*"):
            if p.is_file():
                p.unlink(missing_ok=True)
        try:
            base.rmdir()
        except Exception:
            pass
    except Exception:
        pass

async def safe_delete_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

def load_rss_state() -> dict:
    try:
        path = Path(RSS_STATE_FILE)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"seen": []}


def save_rss_state(state: dict):
    try:
        Path(RSS_STATE_FILE).write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def rss_keyboard() -> Optional[InlineKeyboardMarkup]:
    row = []
    if BTN_INSTAGRAM:
        row.append(InlineKeyboardButton("Instagram", url=BTN_INSTAGRAM))
    if BTN_FACEBOOK:
        row.append(InlineKeyboardButton("Facebook", url=BTN_FACEBOOK))
    if BTN_SITE:
        row.append(InlineKeyboardButton("Site", url=BTN_SITE))
    if not row:
        return None
    return InlineKeyboardMarkup([row])


async def rss_tick(context: ContextTypes.DEFAULT_TYPE):
    if not RSS_URL or not RSS_CHANNEL_ID:
        return

    state = load_rss_state()
    seen = set(state.get("seen", []))

    try:
        feed = feedparser.parse(RSS_URL)
        entries = getattr(feed, "entries", []) or []
    except Exception as e:
        log.warning("RSS parse failed: %s", e)
        return

    if not entries:
        return

    new_entries = []
    for entry in entries[:30]:
        key = (
            (getattr(entry, "id", None) or "")
            or (getattr(entry, "guid", None) or "")
            or (getattr(entry, "link", None) or "")
        ).strip()
        if not key or key in seen:
            continue
        new_entries.append((key, entry))

    if not new_entries:
        return

    new_entries.reverse()
    kb = rss_keyboard()

    for key, entry in new_entries:
        title = (getattr(entry, "title", "") or "").strip()
        link = (getattr(entry, "link", "") or "").strip()
        summary = (getattr(entry, "summary", "") or "").strip()
        text = title if title else "Новый пост"
        if summary:
            clean_summary = re.sub(r"<[^>]+>", "", summary).strip()
            if clean_summary:
                text += f"\n\n{clean_summary[:700]}"
        if link:
            text += f"\n\n{link}"

        try:
            await context.bot.send_message(
                chat_id=int(RSS_CHANNEL_ID) if RSS_CHANNEL_ID.lstrip("-").isdigit() else RSS_CHANNEL_ID,
                text=text,
                reply_markup=kb,
                disable_web_page_preview=False,
            )
            seen.add(key)
        except Exception as e:
            log.warning("RSS send failed: %s", e)
            break

    state["seen"] = list(seen)[-600:]
    save_rss_state(state)


# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Бот работает 😎\n"
        "Команды: /rules /ping /nick /rssstatus\n"
        f"/qrand удаляется через {DELETE_QRAND_AFTER_SECONDS} сек."
    )

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(RULES_TEXT)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"pong ✅\nchat_type={chat.type}\nchat_id={chat.id}"
    )

async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"RSS_URL={'✅ есть' if RSS_URL else '❌ нет'}\n"
        f"RSS_CHANNEL_ID={RSS_CHANNEL_ID or '❌ не задан'}\n"
        f"RSS_POLL_SECONDS={RSS_POLL_SECONDS}\n"
        f"RSS_STATE_FILE={RSS_STATE_FILE}"
    )

async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not msg or not chat or not user:
        return

    if chat.type != ChatType.SUPERGROUP:
        await msg.reply_text("❌ /nick работает только в супер-группе.")
        return

    if not context.args:
        await msg.reply_text("Использование: /nick ТВОЙ_НИК")
        return

    title = " ".join(context.args).strip()
    if len(title) > 16:
        await msg.reply_text("❌ Максимум 16 символов.")
        return

    try:
        me = await context.bot.get_chat_member(chat.id, context.bot.id)

        if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await msg.reply_text("❌ Я должен быть админом.")
            return

        if not getattr(me, "can_promote_members", False):
            await msg.reply_text("❌ У меня нет права добавлять админов.")
            return

        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            can_manage_chat=True,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=False,
            can_pin_messages=False,
            can_post_messages=False,
            can_edit_messages=False,
            can_manage_topics=False,
            is_anonymous=False,
        )

        await asyncio.sleep(1)

        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat.id,
            user_id=user.id,
            custom_title=title,
        )

        await msg.reply_text(f"✅ Ок, твой ник теперь: {title}")

    except Exception as e:
        await msg.reply_text(f"❌ Не получилось поставить ник.\nТех: {e}")
# =========================
# /qrand delete
# =========================
async def delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    message_id = job.data["message_id"]
    await safe_delete_message(context, chat_id, message_id)

async def on_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    context.job_queue.run_once(
        delete_message_job,
        when=DELETE_QRAND_AFTER_SECONDS,
        data={"chat_id": msg.chat_id, "message_id": msg.message_id},
        name=f"del_qrand_{msg.chat_id}_{msg.message_id}",
    )

# =========================
# WELCOME / LEFT / BAN
# =========================
async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return

    names = ", ".join([u.full_name for u in msg.new_chat_members])
    await msg.reply_text(f"{RULES_TEXT}\n\n👋 Привет, {names}!")

async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.left_chat_member:
        return

    left = msg.left_chat_member
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔨 Забанить", callback_data=f"{BAN_PREFIX}{left.id}")]]
    )
    await msg.reply_text(
        f"👋 {left.full_name} вышел(а). Если это спамер — можно забанить:",
        reply_markup=kb,
    )

async def on_ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.message:
        return
    await q.answer()

    data = q.data or ""
    if not data.startswith(BAN_PREFIX):
        return

    try:
        target_id = int(data.split(":", 1)[1])
    except Exception:
        await q.edit_message_text("❌ Некорректные данные.")
        return

    chat_id = q.message.chat_id

    # только админ может нажать
    try:
        member = await context.bot.get_chat_member(chat_id, q.from_user.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await q.edit_message_text("❌ Только админ может банить.")
            return
    except Exception:
        await q.edit_message_text("❌ Не удалось проверить права.")
        return

    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=target_id)
        await q.edit_message_text("✅ Забанен.")
    except Exception as e:
        await q.edit_message_text(f"❌ Не получилось забанить.\nПричина: {e}")

# =========================
# LINKS (Instagram / TikTok)
# =========================
def friendly_block_reason(err_text: str) -> Optional[str]:
    t = (err_text or "").lower()
    if "unavailable for certain audiences" in t or "inappropriate" in t or "sensitive" in t:
        return (
            "Чаще всего если пост:\n"
            "🔞 18+ / sensitive content\n"
            "🔒 только для залогиненных\n"
            "🚫 ограничен по региону\n"
            "👤 аккаунт private\n"
            "⚠️ Instagram пометил как sensitive"
        )
    if "login" in t or "cookie" in t or "sign in" in t:
        return (
            "Похоже контент доступен только залогиненным.\n\n"
            "Чаще всего если пост:\n"
            "🔒 только для залогиненных\n"
            "👤 аккаунт private"
        )
    return None

async def on_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = msg.text or msg.caption or ""
    urls = extract_urls(text)
    if not urls:
        return

    url = urls[0]
    final_url = await resolve_final_url(url)

    is_instagram = bool(IG_RE.search(final_url))
    is_tiktok = bool(TT_RE.search(final_url))
    if not (is_instagram or is_tiktok):
        return

    if is_tiktok and TT_PHOTO_HINT_RE.search(final_url):
        await msg.reply_text(PHOTO_SORRY_TEXT)
        return

    await msg.reply_text("⏳ Пытаюсь скачать...")

    files, err = await download_media_from_url(final_url)
    if err:
        if is_instagram and ("there is no video in this post" in err.lower() or "no video" in err.lower()):
            await msg.reply_text(PHOTO_SORRY_TEXT)
            return

        if is_tiktok and ("unsupported url" in err.lower()):
            await msg.reply_text(PHOTO_SORRY_TEXT)
            return

        friendly = friendly_block_reason(err)
        if friendly:
            await msg.reply_text("❌ Не получилось скачать.\n\n" + friendly)
            return

        await msg.reply_text(
            "❌ Не получилось.\n"
            "Причина: Не получилось скачать. Возможно защита/блокировка или ссылка странная.\n\n"
            f"Тех.деталь: {err}"
        )
        return

    try:
        fp = files[0]
        if fp.suffix.lower() in [".jpg", ".jpeg", ".png"]:
            await msg.reply_text(PHOTO_SORRY_TEXT)
            return

        await msg.reply_video(video=fp.open("rb"))
    except Exception as e:
        await msg.reply_text(f"❌ Не смог отправить видео.\nТех: {e}")
    finally:
        await cleanup_files(files)

# =========================
# MAIN
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty")

    app = Application.builder().token(BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("rssstatus", cmd_rssstatus))
    app.add_handler(CommandHandler("nick", cmd_nick))

    # /qrand
    app.add_handler(MessageHandler(filters.Regex(r"^/qrand\b"), on_qrand), group=5)

    # welcome / leave / ban
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members), group=10)
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member), group=10)
    app.add_handler(CallbackQueryHandler(on_ban_callback, pattern=f"^{BAN_PREFIX}"), group=10)

    # links
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_links), group=20)

    # RSS auto-posts
    if RSS_URL and RSS_CHANNEL_ID:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10, name="rss_tick")

    log.info("Bot started.")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

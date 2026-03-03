import asyncio
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional, Tuple, List

import aiohttp
import feedparser
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import Forbidden, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG (Railway Variables)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Куда постить RSS (канал): можно "@yomabar" или числовой id (например -100...)
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@yomabar").strip()

RSS_URL = os.getenv("RSS_URL", "").strip()
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "120").strip())

DELETE_QRAND_AFTER_SECONDS = int(os.getenv("DELETE_QRAND_AFTER_SECONDS", "30").strip())

# Добавляем в посты ссылки (в конце)
LINK_INSTAGRAM = os.getenv("LINK_INSTAGRAM", "https://www.instagram.com/yomabar.lt").strip()
LINK_FACEBOOK = os.getenv("LINK_FACEBOOK", "https://www.facebook.com/share/1P3dFJ5f5Y/?mibextid=wwXIfr").strip()
LINK_SITE = os.getenv("LINK_SITE", "https://www.yomahayoma.show/").strip()

# =========================
# TEXTS
# =========================
RULES_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)

START_TEXT = (
    "Бот работает 😎\n"
    "Команды: /rules /nick /rssstatus /ping\n"
    f"Авто: удаляет /qrand через {DELETE_QRAND_AFTER_SECONDS}с, "
    "приветствует новых, бан-кнопка после выхода, RSS->канал, "
    "пытается превращать ссылки Instagram/TikTok в видео/фото."
)

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("yomabar-bot")

# =========================
# DB (sqlite)
# =========================
DB_PATH = "data.db"


def db_init():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS rss_sent (
                id TEXT PRIMARY KEY,
                ts INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                k TEXT PRIMARY KEY,
                v TEXT
            )
            """
        )
        conn.commit()


def db_has_rss_id(entry_id: str) -> bool:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM rss_sent WHERE id=?", (entry_id,))
        return cur.fetchone() is not None


def db_mark_rss_id(entry_id: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO rss_sent(id, ts) VALUES(?, ?)",
            (entry_id, int(datetime.now(timezone.utc).timestamp())),
        )
        conn.commit()


def db_set_meta(k: str, v: str):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        conn.commit()


def db_get_meta(k: str) -> Optional[str]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT v FROM meta WHERE k=?", (k,))
        row = cur.fetchone()
        return row[0] if row else None


# =========================
# HELPERS
# =========================
QRAND_RE = re.compile(r"^/qrand(@\w+)?(\s|$)", re.IGNORECASE)

URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)
TT_RE = re.compile(r"https?://(www\.)?(vm\.)?tiktok\.com/[^\s]+", re.IGNORECASE)
IG_RE = re.compile(r"https?://(www\.)?instagram\.com/[^\s]+", re.IGNORECASE)


def is_supergroup(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == ChatType.SUPERGROUP)


async def user_is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def make_footer_links() -> str:
    # Telegram нормально кликает по ссылкам даже без HTML
    return (
        "\n\n"
        f"📷 Instagram: {LINK_INSTAGRAM}\n"
        f"📘 Facebook: {LINK_FACEBOOK}\n"
        f"🌐 Сайт: {LINK_SITE}"
    )


async def fetch_bytes(url: str, timeout_sec: int = 25) -> bytes:
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as resp:
            resp.raise_for_status()
            return await resp.read()


# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(START_TEXT)


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(RULES_TEXT)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"pong ✅\nchat_type={chat.type}\nchat_id={chat.id}"
    )


async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    last = db_get_meta("rss_last_title") or "—"
    last_link = db_get_meta("rss_last_link") or "—"
    last_ts = db_get_meta("rss_last_ts") or "—"
    await update.effective_message.reply_text(
        "RSS статус:\n"
        f"- last_title: {last}\n"
        f"- last_link: {last_link}\n"
        f"- last_ts: {last_ts}\n"
        f"- poll_seconds: {RSS_POLL_SECONDS}"
    )


async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /nick <название> (<=16 символов)
    Работает ТОЛЬКО в supergroup, потому что "ник" делается как custom admin title.
    Бот:
      - делает пользователя админом без прав
      - ставит/меняет custom title
    """
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not is_supergroup(update):
        await msg.reply_text("❌ /nick работает только в супер-группе.")
        return

    title = " ".join(context.args).strip()
    if not title:
        await msg.reply_text("Используй так: /nick <название> (до 16 символов)")
        return

    if len(title) > 16:
        await msg.reply_text("❌ Максимум 16 символов.")
        return

    # Бот должен быть админом и иметь право добавлять админов
    try:
        me = await context.bot.get_me()
        me_member = await context.bot.get_chat_member(chat.id, me.id)
        if me_member.status not in ("administrator", "creator"):
            await msg.reply_text("❌ Я должен быть админом, чтобы ставить /nick.")
            return
        # В PTB нет прямого флага can_promote_members в объекте всегда одинаково,
        # поэтому просто пробуем — если прав нет, поймаем ошибку.
    except Exception:
        pass

    try:
        # делаем юзера админом БЕЗ прав
        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=user.id,
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
            is_anonymous=False,
        )
        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat.id,
            user_id=user.id,
            custom_title=title,
        )
        await msg.reply_text(f"✅ Ник поставлен: «{title}»")
    except BadRequest as e:
        # самые частые причины: нет прав у бота / это не супер-группа / ограничения TG
        await msg.reply_text(f"❌ Не смог поставить ник.\nПричина: {e.message}")
    except Forbidden:
        await msg.reply_text("❌ Нет прав (Forbidden). Проверь, что я админ и могу назначать админов.")


# =========================
# AUTO: welcome & ban button
# =========================
async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return

    # приветствие одним сообщением
    await msg.reply_text(RULES_TEXT)


async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.left_chat_member:
        return

    left = msg.left_chat_member
    chat_id = update.effective_chat.id

    # кнопка "бан" (для админов)
    kb = InlineKeyboardMarkup.from_button(
        InlineKeyboardButton(
            text=f"🚫 Забанить {left.first_name}",
            callback_data=f"ban:{chat_id}:{left.id}",
        )
    )
    await msg.reply_text(
        f"👋 {left.first_name} вышел(ла).",
        reply_markup=kb,
    )


async def on_ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    if not data.startswith("ban:"):
        return

    try:
        _, chat_id_s, user_id_s = data.split(":")
        chat_id = int(chat_id_s)
        user_id = int(user_id_s)
    except Exception:
        await q.edit_message_text("❌ Неверные данные кнопки.")
        return

    clicker_id = q.from_user.id
    if not await user_is_admin(context, chat_id, clicker_id):
        await q.edit_message_text("❌ Банить могут только админы.")
        return

    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await q.edit_message_text("✅ Забанен.")
    except BadRequest as e:
        await q.edit_message_text(f"❌ Не получилось забанить: {e.message}")
    except Forbidden:
        await q.edit_message_text("❌ Нет прав. Проверь, что я админ и могу банить.")


# =========================
# AUTO: delete /qrand after N sec
# =========================
async def maybe_delete_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    if not QRAND_RE.match(msg.text.strip()):
        return

    # планируем удаление
    async def _del():
        await asyncio.sleep(DELETE_QRAND_AFTER_SECONDS)
        try:
            await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
        except Exception:
            pass

    context.application.create_task(_del())


# =========================
# LINKS -> download media (best-effort)
# =========================
async def handle_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    text = msg.text or msg.caption or ""
    if not text:
        return

    urls = URL_RE.findall(text)
    if not urls:
        return

    # ищем первую подходящую
    target = None
    for u in urls:
        if IG_RE.search(u) or TT_RE.search(u):
            target = u
            break
    if not target:
        return

    # Чтобы бот не спамил на каждую ссылку слишком часто:
    # (защита от дублей на одну и ту же ссылку подряд)
    last = db_get_meta(f"last_link_{msg.chat_id}") or ""
    if last == target:
        return
    db_set_meta(f"last_link_{msg.chat_id}", target)

    status = await msg.reply_text("⏳ Пытаюсь скачать...")

    # качаем в фоне, чтобы не зависать
    context.application.create_task(_download_and_send(context, msg.chat_id, msg.message_id, target, status.message_id))


async def _download_and_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, reply_to_id: int, url: str, status_msg_id: int):
    """
    Best-effort:
      - пытаемся получить видео через yt-dlp (без ffmpeg)
      - если "no video" — пытаемся вытащить картинку (thumbnail) и отправить как photo
    """
    try:
        import yt_dlp  # локальный импорт, чтобы при проблемах с пакетом было видно

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # ВАЖНО: формат без merge => ffmpeg не нужен
            "format": "best[ext=mp4]/best",
            "outtmpl": "tmp/%(id)s.%(ext)s",
            "retries": 2,
            "socket_timeout": 15,
        }

        os.makedirs("tmp", exist_ok=True)

        def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)

        info = await asyncio.to_thread(extract)

        # Если плейлист/альбом — возьмём первый элемент
        if isinstance(info, dict) and info.get("_type") == "playlist" and info.get("entries"):
            info = info["entries"][0]

        # 1) видео файл
        filepath = None
        if isinstance(info, dict):
            requested = info.get("requested_downloads")
            if requested and isinstance(requested, list) and requested[0].get("filepath"):
                filepath = requested[0]["filepath"]
            elif info.get("requested_downloads") is None and info.get("_filename"):
                filepath = info.get("_filename")

        # 2) если видео не получилось — попробуем картинку
        thumb_url = None
        if isinstance(info, dict):
            # иногда thumbnails список
            thumbs = info.get("thumbnails") or []
            if thumbs:
                # берём последнюю (часто самая большая)
                t = thumbs[-1]
                thumb_url = t.get("url")

        # удаляем статус "Пытаюсь скачать..."
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
        except Exception:
            pass

        if filepath and os.path.exists(filepath):
            # отправляем как видео (или документ если TG ругается)
            try:
                with open(filepath, "rb") as f:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        reply_to_message_id=reply_to_id,
                    )
                return
            except Exception:
                # fallback: document
                with open(filepath, "rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        reply_to_message_id=reply_to_id,
                    )
                return

        if thumb_url:
            try:
                b = await fetch_bytes(thumb_url)
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=b,
                    reply_to_message_id=reply_to_id,
                )
                return
            except Exception as e:
                await context.bot.send_message(
                    chat_id=chat_id,
                    reply_to_message_id=reply_to_id,
                    text=(
                        "❌ Не получилось.\n"
                        "Причина: не смог отправить картинку.\n"
                        f"Тех.деталь: {type(e).__name__}: {e}"
                    ),
                )
                return

        await context.bot.send_message(
            chat_id=chat_id,
            reply_to_message_id=reply_to_id,
            text=(
                "❌ Не получилось.\n"
                "Причина: не удалось найти видео/картинку по этой ссылке (защита/блокировка/неподдерживаемый тип поста)."
            ),
        )

    except Exception as e:
        # удаляем статус "Пытаюсь скачать..."
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=chat_id,
            reply_to_message_id=reply_to_id,
            text=(
                "❌ Не получилось.\n"
                "Причина: Не получилось скачать. Возможно защита/блокировка или ссылка странная.\n\n"
                f"Тех.деталь: {type(e).__name__}: {e}"
            ),
        )


# =========================
# RSS -> CHANNEL
# =========================
async def rss_tick(context: ContextTypes.DEFAULT_TYPE):
    if not RSS_URL:
        return

    try:
        feed = await asyncio.to_thread(feedparser.parse, RSS_URL)
        entries = feed.entries or []
        if not entries:
            return

        # новые обычно сверху
        for ent in reversed(entries[-10:]):  # ограничим хвостом
            entry_id = (ent.get("id") or ent.get("guid") or ent.get("link") or "").strip()
            if not entry_id:
                continue
            if db_has_rss_id(entry_id):
                continue

            title = (ent.get("title") or "").strip()
            link = (ent.get("link") or "").strip()
            summary = (ent.get("summary") or "").strip()

            text = ""
            if title:
                text += f"**{title}**\n"
            if summary:
                # коротко
                clean = re.sub(r"<[^>]+>", "", summary)
                clean = clean.strip()
                if len(clean) > 700:
                    clean = clean[:700] + "…"
                text += f"{clean}\n"
            if link:
                text += f"\n{link}"

            text += make_footer_links()

            # отправляем
            await context.bot.send_message(
                chat_id=CHANNEL_USERNAME,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=False,
            )

            db_mark_rss_id(entry_id)
            db_set_meta("rss_last_title", title or "—")
            db_set_meta("rss_last_link", link or "—")
            db_set_meta("rss_last_ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    except Exception as e:
        log.exception("RSS tick failed: %s", e)


# =========================
# MAIN
# =========================
def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set Railway Variable BOT_TOKEN")

    db_init()

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("rssstatus", cmd_rssstatus))
    app.add_handler(CommandHandler("nick", cmd_nick))

    # Welcome / leave
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))

    # Ban кнопка (ВАЖНО: это CallbackQueryHandler, не filters.UpdateType...)
    app.add_handler(CallbackQueryHandler(on_ban_callback, pattern=r"^ban:"))

    # Delete /qrand
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, maybe_delete_qrand))
    app.add_handler(MessageHandler(filters.COMMAND, maybe_delete_qrand))  # если /qrand как команда

    # Links handler (Instagram/TikTok)
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, handle_links))

    # RSS job
    if RSS_URL:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)

    return app


def main():
    app = build_app()
    log.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
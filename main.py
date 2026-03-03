import os
import re
import time
import html
import asyncio
import logging
import tempfile
import subprocess
from typing import Optional, Tuple, List

import feedparser
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# -------------------------
# CONFIG (env vars)
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@yomabar").strip()  # e.g. @yomabar or -100123...
RSS_URL = os.getenv("RSS_URL", "").strip()
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "120").strip())

# behavior
DELETE_QRAND_AFTER_SECONDS = 30
NICK_MAX_LEN = 16

# add links to RSS posts
LINK_INSTAGRAM = os.getenv("LINK_INSTAGRAM", "").strip()
LINK_FACEBOOK = os.getenv("LINK_FACEBOOK", "").strip()
LINK_SITE = os.getenv("LINK_SITE", "").strip()

# yt-dlp + ffmpeg options (Railway: нужно ставить в requirements/aptfile если надо)
YTDLP_TIMEOUT_SEC = 120
MAX_UPLOAD_MB = 45  # Telegram bot limit ~50MB for many accounts; keep safe
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# -------------------------
# TEXTS
# -------------------------
WELCOME_RULES = (
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
    "и пытается превращать ссылки Instagram/TikTok в видео/фото."
)

# -------------------------
# LOGGING
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("yomabar-bot")

# -------------------------
# REGEX
# -------------------------
QRAND_RE = re.compile(r"^/qrand(@\w+)?(\s|$)", re.IGNORECASE)

URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)
IG_RE = re.compile(r"https?://(www\.)?instagram\.com/[^ \n]+", re.IGNORECASE)
TT_RE = re.compile(r"https?://(www\.)?(vm\.)?tiktok\.com/[^ \n]+", re.IGNORECASE)

# -------------------------
# SIMPLE in-memory state
# (если хочешь сохранять между рестартами — надо файл/DB)
# -------------------------
LAST_RSS_IDS: set[str] = set()
RECENT_MEDIA_URLS: dict[str, float] = {}  # url -> ts, антиспам


# -------------------------
# HELPERS
# -------------------------
def is_supergroup(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in (ChatType.SUPERGROUP, ChatType.CHANNEL))


def escape_md(text: str) -> str:
    return html.escape(text)


def build_footer_links() -> str:
    parts = []
    if LINK_INSTAGRAM:
        parts.append(f"Instagram: {LINK_INSTAGRAM}")
    if LINK_FACEBOOK:
        parts.append(f"Facebook: {LINK_FACEBOOK}")
    if LINK_SITE:
        parts.append(f"Site: {LINK_SITE}")
    if not parts:
        return ""
    return "\n\n" + "\n".join(parts)


async def safe_delete_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        log.warning("delete_message failed: %s", e)


async def delayed_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, seconds: int):
    await asyncio.sleep(seconds)
    await safe_delete_message(context, chat_id, message_id)


# -------------------------
# COMMANDS
# -------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_RULES)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(f"pong ✅\nchat_type={chat.type}\nchat_id={chat.id}")


async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = f"RSS_URL: {RSS_URL or 'не задан'}\nRSS_POLL_SECONDS: {RSS_POLL_SECONDS}\nОпубликовано (в памяти): {len(LAST_RSS_IDS)}"
    await update.message.reply_text(txt)


async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /nick <текст до 16 символов>
    Важно: Telegram "админские подписи" (типа «невеста», «юморист») работают только в супергруппах,
    и меняются через promoteChatMember (custom_title).
    """
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type != ChatType.SUPERGROUP:
        await msg.reply_text("❌ /nick работает только в супер-группе.")
        return

    if not context.args:
        await msg.reply_text("Использование: /nick <ник до 16 символов>")
        return

    title = " ".join(context.args).strip()
    if len(title) > NICK_MAX_LEN:
        await msg.reply_text(f"❌ Слишком длинно. Максимум {NICK_MAX_LEN} символов.")
        return

    # Ставим custom_title, не меняя реальные права.
    # Важно: бот должен быть админом и иметь право назначать админов.
    try:
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
            can_post_messages=False,
            can_edit_messages=False,
            can_pin_messages=False,
            is_anonymous=False,
        )
        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat.id,
            user_id=user.id,
            custom_title=title,
        )
        await msg.reply_text(f"✅ Ник установлен: {title}")
    except Exception as e:
        log.exception("nick failed")
        await msg.reply_text(f"❌ Не получилось поставить ник.\nПричина: {e}")


# -------------------------
# /qrand delete handler (FIXED!)
# -------------------------
async def on_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Ловим ТОЛЬКО сообщения, которые начинаются с /qrand,
    и удаляем через 30 секунд.
    """
    msg = update.effective_message
    if not msg:
        return
    context.application.create_task(
        delayed_delete(context, msg.chat_id, msg.message_id, DELETE_QRAND_AFTER_SECONDS)
    )


# -------------------------
# JOIN/LEAVE handlers
# -------------------------
async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.new_chat_members:
        return
    # приветствие + правила
    names = ", ".join([m.full_name for m in msg.new_chat_members])
    await msg.reply_text(f"{WELCOME_RULES}\n\n👋 Привет, {names}!")


BAN_CB_PREFIX = "banleft:"  # callback_data = banleft:<user_id>


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Когда кто-то вышел — постим сообщение с кнопкой "Забанить".
    """
    cmu = update.chat_member
    if not cmu:
        return

    chat = cmu.chat
    user = cmu.from_user  # кто изменил? не надо
    target = cmu.new_chat_member.user

    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status

    # ушел сам / кикнут / стал left
    if old_status in ("member", "restricted") and new_status == "left":
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚫 Забанить ушедшего", callback_data=f"{BAN_CB_PREFIX}{target.id}")]]
        )
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"👋 {target.full_name} вышел(ла).",
            reply_markup=kb,
        )


async def on_ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    if not data.startswith(BAN_CB_PREFIX):
        return

    chat_id = q.message.chat_id
    user_id = int(data.split(":", 1)[1])

    # проверим, что нажимающий — админ
    try:
        member = await context.bot.get_chat_member(chat_id, q.from_user.id)
        if member.status not in ("administrator", "creator"):
            await q.edit_message_text("❌ Только админ может банить.")
            return
    except Exception:
        await q.edit_message_text("❌ Не удалось проверить права.")
        return

    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await q.edit_message_text("✅ Забанен.")
    except Exception as e:
        await q.edit_message_text(f"❌ Не получилось забанить.\nПричина: {e}")


# -------------------------
# RSS -> channel
# -------------------------
async def rss_tick(context: ContextTypes.DEFAULT_TYPE):
    if not RSS_URL or not CHANNEL_USERNAME:
        return

    try:
        feed = feedparser.parse(RSS_URL)
    except Exception as e:
        log.warning("RSS parse failed: %s", e)
        return

    entries = feed.entries or []
    # новые сначала (RSS может быть любым)
    for entry in reversed(entries[:10]):
        entry_id = (entry.get("id") or entry.get("link") or entry.get("title") or "").strip()
        if not entry_id:
            continue
        if entry_id in LAST_RSS_IDS:
            continue

        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        summary = (entry.get("summary") or "").strip()

        text = ""
        if title:
            text += f"<b>{escape_md(title)}</b>\n"
        if summary:
            # чуть-чуть ограничим
            text += f"{escape_md(summary[:900])}\n"
        if link:
            text += f"\n{escape_md(link)}"

        text += build_footer_links()

        try:
            await context.bot.send_message(
                chat_id=CHANNEL_USERNAME,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            LAST_RSS_IDS.add(entry_id)
        except Exception as e:
            log.warning("RSS send failed: %s", e)
            # не добавляем в set, чтобы потом попробовать снова


# -------------------------
# Instagram/TikTok link -> download (yt-dlp)
# -------------------------
def pick_urls_from_text(text: str) -> List[str]:
    if not text:
        return []
    return URL_RE.findall(text)


def is_media_url(url: str) -> bool:
    return bool(IG_RE.search(url) or TT_RE.search(url))


def recently_processed(url: str, window_sec: int = 120) -> bool:
    now = time.time()
    ts = RECENT_MEDIA_URLS.get(url)
    if ts and (now - ts) < window_sec:
        return True
    RECENT_MEDIA_URLS[url] = now
    return False


def ensure_ffmpeg_exists() -> bool:
    return subprocess.call(["bash", "-lc", "command -v ffmpeg >/dev/null 2>&1"]) == 0


async def ytdlp_download(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (filepath, error_text)
    - If ok: (path, None)
    - If fail: (None, reason)
    """
    # Railway: yt-dlp должен быть установлен в requirements.txt
    # ffmpeg желательно, но можно и без, если не мержим форматы.
    has_ffmpeg = ensure_ffmpeg_exists()

    # Ставим формат так, чтобы без ffmpeg тоже работало:
    # - для видео: best[ext=mp4]/best
    # - для фото/галереи: yt-dlp может отдавать изображения как отдельные файлы (сложно), поэтому
    #   мы делаем "best" и если это не видео — сообщаем.
    fmt = "best[ext=mp4]/best" if has_ffmpeg else "best[ext=mp4]/best"

    with tempfile.TemporaryDirectory() as tmpdir:
        outtmpl = os.path.join(tmpdir, "dl.%(ext)s")
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--no-playlist",
            "--max-filesize", str(MAX_UPLOAD_BYTES),
            "-f", fmt,
            "-o", outtmpl,
            url,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=YTDLP_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                proc.kill()
                return None, "Timeout при скачивании."

            if proc.returncode != 0:
                err = (stderr or b"").decode("utf-8", "ignore").strip()
                if not err:
                    err = "Неизвестная ошибка yt-dlp."
                return None, err[:1500]

            # найдём скачанный файл
            for fn in os.listdir(tmpdir):
                path = os.path.join(tmpdir, fn)
                if os.path.isfile(path):
                    # перенесём во временный файл, чтобы не удалился вместе с tmpdir
                    suffix = os.path.splitext(fn)[1]
                    fd, final_path = tempfile.mkstemp(prefix="media_", suffix=suffix)
                    os.close(fd)
                    with open(path, "rb") as r, open(final_path, "wb") as w:
                        w.write(r.read())
                    return final_path, None

            return None, "Скачалось, но файл не найден."
        except FileNotFoundError:
            return None, "yt-dlp не установлен (добавь в requirements.txt: yt-dlp)."
        except Exception as e:
            return None, str(e)


async def on_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Реагируем на сообщения со ссылками IG/TT.
    Важно: этот handler НЕ должен блокироваться /qrand handler'ом.
    """
    msg = update.effective_message
    if not msg:
        return

    text = msg.text or msg.caption or ""
    urls = pick_urls_from_text(text)
    urls = [u for u in urls if is_media_url(u)]

    if not urls:
        return

    # обработаем максимум 2 ссылки из сообщения
    urls = urls[:2]

    for url in urls:
        if recently_processed(url):
            continue

        status = await msg.reply_text("⏳ Пытаюсь скачать...")

        path, err = await ytdlp_download(url)
        if err:
            # Дружелюбная причина + техдеталь
            await status.edit_text(
                "❌ Не получилось.\n"
                "Причина: Не получилось скачать. Возможно защита/блокировка или ссылка странная.\n\n"
                f"Тех.деталь: {err}"
            )
            continue

        # отправляем как видео/фото по расширению
        try:
            ext = (os.path.splitext(path)[1] or "").lower()
            with open(path, "rb") as f:
                if ext in (".mp4", ".mov", ".m4v", ".webm"):
                    await msg.reply_video(video=f)
                else:
                    # если это не видео — попробуем как фото/док
                    await msg.reply_photo(photo=f)
            await status.delete()
        except Exception as e:
            await status.edit_text(f"❌ Не смог отправить файл в Telegram.\nПричина: {e}")
        finally:
            try:
                os.remove(path)
            except Exception:
                pass


# -------------------------
# MAIN
# -------------------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var is empty")

    app = Application.builder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("rssstatus", cmd_rssstatus))
    app.add_handler(CommandHandler("nick", cmd_nick))

    # join/leave
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(on_ban_callback, pattern=f"^{BAN_CB_PREFIX}"))

    # IMPORTANT: /qrand delete handler - matches ONLY /qrand now, so it won't block other text
    app.add_handler(MessageHandler(filters.Regex(QRAND_RE), on_qrand))

    # links handler
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_links))

    # RSS schedule
    if RSS_URL and CHANNEL_USERNAME:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)

    log.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
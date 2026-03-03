import os
import re
import json
import time
import asyncio
import logging
from datetime import datetime
from typing import Optional, Tuple, Dict, Any

import feedparser
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    ChatMemberHandler,
    filters,
)

# =========================
# CONFIG (ENV + defaults)
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Канал для RSS (пример: @yomabar)
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()  # @channelusername

RSS_URL = os.getenv("RSS_URL", "").strip()
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "120"))

# Удаление /qrand через N секунд
DELETE_QRAND_AFTER_SECONDS = int(os.getenv("DELETE_QRAND_AFTER_SECONDS", "30"))

# Ограничение ника
NICK_MAX_LEN = 16

# Файл для хранения последнего RSS ID (в Railway файловая система может быть не вечной,
# но часто работает. Если слетит — просто может повторить 1-2 поста.)
STATE_FILE = "state.json"

# Правила / приветствие
WELCOME_RULES_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
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
# IN-MEMORY "DB"
# =========================
# Ники (в памяти). Если хочешь навсегда — сделаем хранение в json.
NICKS: Dict[Tuple[int, int], str] = {}  # (chat_id, user_id) -> nick


# =========================
# HELPERS
# =========================

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Failed to save state: %s", e)


def is_admin(member_status: str) -> bool:
    return member_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def user_is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверка, что пользователь админ/владелец в этом чате."""
    if not update.effective_chat or not update.effective_user:
        return False
    try:
        m = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
        return is_admin(m.status)
    except Exception:
        return False


def extract_text_or_caption(update: Update) -> str:
    msg = update.effective_message
    if not msg:
        return ""
    if msg.text:
        return msg.text
    if msg.caption:
        return msg.caption
    return ""


URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)

def find_first_url(text: str) -> Optional[str]:
    m = URL_RE.search(text or "")
    if not m:
        return None
    url = m.group(1).strip().rstrip(").,!?]")
    return url


def is_supported_video_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return ("tiktok.com" in u) or ("instagram.com" in u) or ("youtu.be" in u) or ("youtube.com" in u)


def sanitize_nick(n: str) -> str:
    n = (n or "").strip()
    n = re.sub(r"\s+", " ", n)
    return n[:NICK_MAX_LEN]


async def safe_delete_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest as e:
        # например "message can't be deleted"
        log.info("Delete failed: %s", e)
    except Exception as e:
        log.info("Delete failed: %s", e)


# =========================
# COMMANDS
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот работает 😎\n"
        "Команды: /rules /nick /rssstatus\n"
        f"Авто: удаляет /qrand через {DELETE_QRAND_AFTER_SECONDS}с, приветствует новых,\n"
        "бан-кнопка после выхода, RSS->канал,\n"
        "и пробует превращать ссылки Instagram/TikTok в видео."
    )


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_RULES_TEXT)


async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /nick <текст до 16>
    Сохраняет "ник" в памяти и подтверждает.
    (Менять можно сколько угодно — просто повторно /nick ...)
    """
    if not update.effective_chat or not update.effective_user or not update.message:
        return

    args = context.args
    if not args:
        key = (update.effective_chat.id, update.effective_user.id)
        cur = NICKS.get(key)
        if cur:
            await update.message.reply_text(f"Твой ник: **{cur}**", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("Использование: /nick <ник до 16 символов>")
        return

    nick = sanitize_nick(" ".join(args))
    if not nick:
        await update.message.reply_text("Ник пустой 😅\nИспользование: /nick <ник до 16 символов>")
        return

    if len(nick) > NICK_MAX_LEN:
        nick = nick[:NICK_MAX_LEN]

    key = (update.effective_chat.id, update.effective_user.id)
    NICKS[key] = nick
    await update.message.reply_text(f"✅ Ник установлен: **{nick}**", parse_mode=ParseMode.MARKDOWN)


async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    last_id = state.get("rss_last_id")
    last_ts = state.get("rss_last_time")
    await update.message.reply_text(
        "RSS статус:\n"
        f"- URL: {RSS_URL or 'не задан'}\n"
        f"- Канал: {CHANNEL_USERNAME or 'не задан'}\n"
        f"- Интервал: {RSS_POLL_SECONDS}s\n"
        f"- Последний ID: {last_id}\n"
        f"- Последняя отправка: {last_ts}"
    )


# =========================
# AUTO: DELETE /qrand
# =========================

QRAND_RE = re.compile(r"^/qrand(@\w+)?(\s|$)", re.IGNORECASE)

async def on_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Если сообщение — /qrand, удаляем через N секунд.
    """
    msg = update.effective_message
    if not msg or not update.effective_chat:
        return

    text = msg.text or ""
    if not QRAND_RE.match(text):
        return

    # планируем удаление
    async def _del_later():
        await asyncio.sleep(DELETE_QRAND_AFTER_SECONDS)
        await safe_delete_message(context, update.effective_chat.id, msg.message_id)

    asyncio.create_task(_del_later())


# =========================
# WELCOME + LEFT -> BAN BUTTON
# =========================

async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Срабатывает на join/leave/kick.
    """
    cmu = update.chat_member
    if not cmu:
        return

    chat = cmu.chat
    new = cmu.new_chat_member
    old = cmu.old_chat_member

    user = new.user if new else None
    if not user:
        return

    # JOIN: old != member, new == member
    if old.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and new.status == ChatMemberStatus.MEMBER:
        mention = user.mention_html()
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"{mention}\n\n{WELCOME_RULES_TEXT}",
            parse_mode=ParseMode.HTML,
        )
        return

    # LEAVE (сам вышел) или KICKED (удален)
    if old.status == ChatMemberStatus.MEMBER and new.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        # Кнопка "бан"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔨 Забанить", callback_data=f"ban:{user.id}")],
        ])
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"👋 {user.full_name} ушёл.\nЕсли это спамер — можно забанить:",
            reply_markup=kb,
        )


async def on_ban_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    if not data.startswith("ban:"):
        return

    if not q.message or not q.message.chat:
        return

    # Только админы могут банить
    try:
        admin = await context.bot.get_chat_member(q.message.chat.id, q.from_user.id)
        if not is_admin(admin.status):
            await q.edit_message_text("❌ Только админы могут банить.")
            return
    except Exception:
        await q.edit_message_text("❌ Не могу проверить права.")
        return

    try:
        user_id = int(data.split(":", 1)[1])
    except ValueError:
        await q.edit_message_text("❌ Неверные данные кнопки.")
        return

    try:
        await context.bot.ban_chat_member(chat_id=q.message.chat.id, user_id=user_id)
        await q.edit_message_text("✅ Забанил.")
    except BadRequest as e:
        await q.edit_message_text(f"❌ Не получилось забанить.\nПричина: {e}")
    except Exception as e:
        await q.edit_message_text(f"❌ Ошибка: {e}")


# =========================
# RSS -> CHANNEL
# =========================

def rss_entry_id(entry: Any) -> str:
    # максимально стабильный ID
    return getattr(entry, "id", None) or getattr(entry, "guid", None) or getattr(entry, "link", None) or str(hash(str(entry)))


async def rss_tick(context: ContextTypes.DEFAULT_TYPE):
    """
    Периодически читает RSS и постит новые записи в канал.
    Не постит повторно один и тот же ID.
    """
    if not RSS_URL or not CHANNEL_USERNAME:
        return

    state = load_state()
    last_id = state.get("rss_last_id")

    feed = feedparser.parse(RSS_URL)
    if not feed or not getattr(feed, "entries", None):
        return

    # Берем самые новые сверху (обычно entries уже отсортированы)
    entries = list(feed.entries)

    # Найдем все новые до last_id
    new_entries = []
    for e in entries:
        eid = rss_entry_id(e)
        if last_id and eid == last_id:
            break
        new_entries.append(e)

    if not new_entries:
        return

    # постим в обратном порядке (старое -> новое)
    for e in reversed(new_entries):
        title = getattr(e, "title", "") or ""
        link = getattr(e, "link", "") or ""
        summary = getattr(e, "summary", "") or ""
        summary = re.sub(r"<[^>]+>", "", summary).strip()

        text = f"🆕 <b>{title}</b>\n\n{summary}\n\n<a href=\"{link}\">Открыть пост</a>"
        try:
            await context.bot.send_message(
                chat_id=CHANNEL_USERNAME,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
        except Exception as ex:
            log.warning("RSS send failed: %s", ex)

        # обновляем last_id на каждый успешный проход
        eid = rss_entry_id(e)
        state["rss_last_id"] = eid
        state["rss_last_time"] = datetime.utcnow().isoformat() + "Z"
        save_state(state)


# =========================
# VIDEO DOWNLOADER (TikTok/Instagram)
# Variant #2: no ffmpeg merge
# =========================

# Требует установленный yt-dlp в среде.
# На Railway проще всего поставить через requirements.txt: yt-dlp
# (и НЕ требуем ffmpeg)

async def download_with_ytdlp(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Возвращает (filepath, error_text)
    """
    try:
        import yt_dlp
    except Exception:
        return None, "yt-dlp не установлен (добавь в requirements.txt: yt-dlp)"

    outtmpl = "download_%(id)s.%(ext)s"
    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Вариант #2: пытаемся взять один файл видео (обычно mp4)
        # чтобы не требовать ffmpeg
        "format": "mp4/bv*+ba/best",
        "merge_output_format": "mp4",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # yt-dlp может вернуть список
            if isinstance(info, dict):
                # получаем итоговый файл
                fname = ydl.prepare_filename(info)
                # если вдруг расширение поменялось
                if not os.path.exists(fname):
                    # попробуем найти любой файл по id
                    vid = info.get("id")
                    for f in os.listdir("."):
                        if vid and vid in f and f.startswith("download_"):
                            fname = f
                            break
                if os.path.exists(fname):
                    return fname, None
                return None, "Файл скачался, но я не нашёл его на диске."
            return None, "Неожиданный ответ yt-dlp."
    except Exception as e:
        return None, str(e)


async def on_video_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Ловит все сообщения, берет текст/подпись, ищет ссылку.
    Если это instagram/tiktok -> пытается скачать и отправить видео.
    """
    msg = update.effective_message
    if not msg or not update.effective_chat:
        return

    text = extract_text_or_caption(update)
    if not text:
        return

    url = find_first_url(text)
    if not url or not is_supported_video_url(url):
        return

    # Быстро отвечаем, чтобы было понятно что бот увидел ссылку
    status = await msg.reply_text("⏳ Пытаюсь скачать видео…")

    try:
        filepath, err = await download_with_ytdlp(url)
        if err or not filepath:
            await status.edit_text(
                "❌ Не получилось.\n"
                "Причина: Не получилось скачать. Возможно защита/блокировка или ссылка странная.\n\n"
                f"Тех.деталь: {err}"
            )
            return

        # Отправляем видео
        try:
            await context.bot.send_video(
                chat_id=update.effective_chat.id,
                video=open(filepath, "rb"),
                caption="🎬 Видео",
            )
            await status.delete()
        except BadRequest as e:
            await status.edit_text(f"❌ Не могу отправить как видео: {e}\nПробую как файл…")
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=open(filepath, "rb"),
                caption="📎 Видео-файл",
            )
            await status.delete()
        finally:
            # чистим файл
            try:
                os.remove(filepath)
            except Exception:
                pass

    except Exception as e:
        try:
            await status.edit_text(f"❌ Ошибка: {e}")
        except Exception:
            pass


# =========================
# MAIN
# =========================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения")

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("nick", cmd_nick))
    app.add_handler(CommandHandler("rssstatus", cmd_rssstatus))

    # Chat member updates: welcome + ban button
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(on_ban_button))

    # Auto delete /qrand
    app.add_handler(MessageHandler(filters.Regex(r"^/qrand(@\w+)?(\s|$)"), on_qrand), group=0)

    # Video link handler (filters.ALL, внутри проверяем)
    app.add_handler(MessageHandler(filters.ALL, on_video_link), group=1)

    # RSS job
    if RSS_URL and CHANNEL_USERNAME:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)

    log.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
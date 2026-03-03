import os
import re
import html
import time
import json
import asyncio
import logging
import tempfile
from typing import Optional, Tuple

import feedparser
from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode, ChatMemberStatus
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
# CONFIG (env + defaults)
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@yomabar")  # куда постить RSS
RSS_URL = os.getenv("RSS_URL", "")
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "120"))

DELETE_QRAND_AFTER_SECONDS = int(os.getenv("DELETE_QRAND_AFTER_SECONDS", "30"))

# Ники
MAX_NICK_LEN = 16

# Где хранить состояние (простая БД в файле)
STATE_FILE = "state.json"

# Ловим ссылки
TIKTOK_RE = re.compile(r"(https?://(?:www\.)?tiktok\.com/[^\s]+|https?://vm\.tiktok\.com/[^\s]+)", re.IGNORECASE)
INSTA_RE = re.compile(r"(https?://(?:www\.)?instagram\.com/[^\s]+)", re.IGNORECASE)

# -------------------------
# LOGGING
# -------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("yomabar-bot")


# -------------------------
# STATE HELPERS
# -------------------------
def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"rss_last_id": None, "nicks": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"rss_last_id": None, "nicks": {}}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Failed to save state: %s", e)


STATE = load_state()


# -------------------------
# TEXTS
# -------------------------
WELCOME_RULES = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)


# -------------------------
# UTILS
# -------------------------
def is_group(chat_type: str) -> bool:
    return chat_type in ("group", "supergroup")


async def is_user_admin(update: Update, user_id: int) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    try:
        member = await chat.get_member(user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False


def extract_first_link(text: str) -> Optional[str]:
    if not text:
        return None
    m = TIKTOK_RE.search(text)
    if m:
        return m.group(1)
    m = INSTA_RE.search(text)
    if m:
        return m.group(1)
    return None


def pretty_fail_reason(raw: str) -> str:
    """
    Делает более понятные сообщения из типичных ошибок yt-dlp/сетевых.
    """
    s = (raw or "").lower()

    if "private" in s or "login" in s:
        return "Видео приватное или требует входа."
    if "404" in s or "not found" in s:
        return "Не нашёл видео (404). Возможно удалено."
    if "geo" in s or "not available in your country" in s:
        return "Региональная блокировка (geo). Railway может быть заблокирован."
    if "signature" in s or "forbidden" in s or "403" in s:
        return "Сайт не дал скачать (403/forbidden). Часто из-за блокировки/защиты."
    if "timed out" in s or "timeout" in s:
        return "Таймаут сети. Попробуй позже."
    if "unsupported url" in s:
        return "Неподдерживаемая ссылка."
    return "Не получилось скачать. Возможно защита/блокировка или ссылка странная."


# -------------------------
# DOWNLOAD (yt-dlp)
# -------------------------
async def ytdlp_download(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Возвращает (path_to_mp4, error_message)
    """
    try:
        import yt_dlp
    except Exception as e:
        return None, f"yt-dlp не установлен: {e}"

    # отдельная папка под файл
    tmpdir = tempfile.mkdtemp(prefix="dl_")
    outtmpl = os.path.join(tmpdir, "video.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
        # Стараемся брать лучший mp4
        "format": "bv*+ba/best",
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 15,
    }

    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # пробуем найти итоговый файл
            # yt-dlp часто создаёт mp4, иногда mkv/webm — мы всё равно отправим как есть
            # ищем по шаблону
            ext = info.get("ext") or "mp4"
            candidate = os.path.join(tmpdir, f"video.{ext}")
            if os.path.exists(candidate):
                return candidate
            # иногда после мерджа становится mp4
            mp4cand = os.path.join(tmpdir, "video.mp4")
            if os.path.exists(mp4cand):
                return mp4cand
            # fallback: ищем любой файл в папке
            for fn in os.listdir(tmpdir):
                p = os.path.join(tmpdir, fn)
                if os.path.isfile(p) and os.path.getsize(p) > 0:
                    return p
            return None

    try:
        path = await asyncio.to_thread(_run)
        if not path:
            return None, "Не нашёл файл после скачивания."
        # ограничение Telegram: бот обычно может слать до ~50MB (может больше, но безопасно)
        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > 48:
            return None, f"Файл слишком большой ({size_mb:.1f} MB). Telegram может не принять."
        return path, None
    except Exception as e:
        return None, str(e)


# -------------------------
# COMMANDS
# -------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот работает 😎\n"
        "Команды: /rules /nick /rssstatus\n"
        "Авто: удаляет /qrand, приветствует новых, бан-кнопка после выхода, RSS->канал,\n"
        "и пробует превращать ссылки Instagram/TikTok в видео."
    )


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_RULES)


async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    last_id = STATE.get("rss_last_id")
    await update.message.reply_text(f"RSS_URL: {RSS_URL or 'не задан'}\nПоследний пост ID: {last_id}")


async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    chat = update.effective_chat
    if not chat or not is_group(chat.type):
        await msg.reply_text("Команда /nick работает только в группе/супергруппе.")
        return

    args = msg.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await msg.reply_text("Использование: /nick твой_ник (до 16 символов)")
        return

    nick = args[1].strip()
    if len(nick) > MAX_NICK_LEN:
        await msg.reply_text(f"Слишком длинно. Максимум {MAX_NICK_LEN} символов.")
        return

    user = msg.from_user
    if not user:
        return

    # сохраняем ник
    STATE.setdefault("nicks", {})
    STATE["nicks"][str(user.id)] = nick
    save_state(STATE)

    # пробуем выставить кастомный title (работает только если бот админ и это супергруппа)
    try:
        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat.id,
            user_id=user.id,
            custom_title=nick
        )
        await msg.reply_text(f"Готово ✅ Твой админ-титул: {nick}")
    except Exception as e:
        # если не получилось — всё равно ник в базе, можно показывать его в сообщениях/логике
        await msg.reply_text(
            "Ник сохранил ✅ но Telegram не дал поставить титул.\n"
            "Причины: чат не супергруппа / бот не админ / нет права 'Add new admins'.\n"
            f"Ошибка: {e}"
        )


# -------------------------
# AUTO: DELETE /qrand after 30s
# -------------------------
async def delete_later(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    message_id = job.data["message_id"]
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def on_message_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    if msg.text and msg.text.strip().startswith("/qrand"):
        # Планируем удаление
        context.job_queue.run_once(
            delete_later,
            when=DELETE_QRAND_AFTER_SECONDS,
            data={"chat_id": msg.chat_id, "message_id": msg.message_id},
            name=f"del_{msg.chat_id}_{msg.message_id}",
        )


# -------------------------
# AUTO: WELCOME NEW USERS + RULES
# -------------------------
async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    for u in msg.new_chat_members:
        name = html.escape(u.full_name)
        await msg.reply_text(f"Привет, {name}!\n\n{WELCOME_RULES}", parse_mode=ParseMode.HTML)


# -------------------------
# AUTO: BAN BUTTON WHEN USER LEFT
# -------------------------
async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.left_chat_member:
        return
    u = msg.left_chat_member
    text = f"Юзер вышел: {u.full_name}\nЕсли это спамер — можно забанить:"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚫 Забанить", callback_data=f"ban:{u.id}"),
    ]])
    await msg.reply_text(text, reply_markup=kb)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    if data.startswith("ban:"):
        user_id = int(data.split(":", 1)[1])

        # банить могут только админы
        if not await is_user_admin(update, q.from_user.id):
            await q.edit_message_text("Только админы могут банить.")
            return

        try:
            await context.bot.ban_chat_member(chat_id=q.message.chat_id, user_id=user_id)
            await q.edit_message_text("Забанил ✅")
        except Exception as e:
            await q.edit_message_text(f"Не смог забанить: {e}")


# -------------------------
# AUTO: TikTok / Instagram -> video
# -------------------------
async def on_message_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    link = extract_first_link(msg.text)
    if not link:
        return

    # В группе отвечаем только если бот видит сообщения (Privacy off) и чтобы не спамить — 1 ответ на 1 сообщение
    status = await msg.reply_text("⏳ Пытаюсь скачать видео...")

    path, err = await ytdlp_download(link)
    if err:
        reason = pretty_fail_reason(err)
        await status.edit_text(f"❌ Не получилось.\nПричина: {reason}\n\nТех.деталь: {err}")
        return

    try:
        # отправляем как видео (если телега не примет как видео — отправим как документ)
        with open(path, "rb") as f:
            await context.bot.send_video(
                chat_id=msg.chat_id,
                video=f,
                caption="✅ Готово",
                reply_to_message_id=msg.message_id,
            )
        await status.delete()
    except Exception as e:
        try:
            with open(path, "rb") as f:
                await context.bot.send_document(
                    chat_id=msg.chat_id,
                    document=f,
                    caption=f"Отправил как файл (не как видео). Причина: {e}",
                    reply_to_message_id=msg.message_id,
                )
            await status.delete()
        except Exception as e2:
            await status.edit_text(f"❌ Скачал, но Telegram не принял отправку.\nОшибка: {e2}")


# -------------------------
# RSS -> CHANNEL
# -------------------------
async def rss_tick(context: ContextTypes.DEFAULT_TYPE):
    if not RSS_URL or not CHANNEL_USERNAME:
        return

    try:
        feed = await asyncio.to_thread(feedparser.parse, RSS_URL)
    except Exception as e:
        log.error("RSS parse error: %s", e)
        return

    if not feed.entries:
        return

    # Обычно самый свежий — первый
    entry = feed.entries[0]
    entry_id = entry.get("id") or entry.get("link") or entry.get("title")

    last_id = STATE.get("rss_last_id")
    if entry_id and entry_id == last_id:
        return  # уже постили

    title = entry.get("title", "").strip()
    link = entry.get("link", "").strip()
    summary = (entry.get("summary", "") or "").strip()

    # Чуть приводим к норм виду
    text = ""
    if title:
        text += f"<b>{html.escape(title)}</b>\n\n"
    if summary:
        # summary может быть HTML
        # просто уберём очень длинное
        clean = re.sub(r"<[^>]+>", "", summary)
        clean = clean.strip()
        if len(clean) > 600:
            clean = clean[:600] + "…"
        text += f"{html.escape(clean)}\n\n"
    if link:
        text += f"🔗 <a href=\"{html.escape(link)}\">Открыть пост</a>\n\n"

    # твои ссылки:
    insta = "https://www.instagram.com/yomabar.lt?igsh=NmZxMzBnNWFjaHQy"
    fb = "https://www.facebook.com/share/1P3dFJ5f5Y/?mibextid=wwXIfr"
    site = "https://www.yomahayoma.show/?fbclid=IwVERFWAQSeMZleHRuA2FlbQIxMABzcnRjBmFwcF9pZAo2NjI4NTY4Mzc5AAEesge47GAJQ72RstwAGARsRXJktokh_iExhSv_5IPnccBzVBz8tW9oLkKuFtY_aem_0ZLfyoSFOW9iUSYpi0ElTQ"
    text += (
        f"📌 <a href=\"{insta}\">Instagram</a> | "
        f"<a href=\"{fb}\">Facebook</a> | "
        f"<a href=\"{site}\">Сайт</a>"
    )

    try:
        await context.bot.send_message(
            chat_id=CHANNEL_USERNAME,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        STATE["rss_last_id"] = entry_id
        save_state(STATE)
        log.info("Posted RSS entry: %s", entry_id)
    except Exception as e:
        log.error("Failed to post to channel: %s", e)


# -------------------------
# MAIN
# -------------------------
def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("nick", cmd_nick))
    app.add_handler(CommandHandler("rssstatus", cmd_rssstatus))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_callback))

    # Welcome / left
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))

    # /qrand auto delete
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^/qrand(\s|$)"), on_message_qrand))

    # TikTok/Instagram links -> video
    app.add_handler(MessageHandler(filters.TEXT & (filters.Regex("tiktok.com") | filters.Regex("instagram.com")), on_message_links))

    return app


async def post_init(app: Application):
    # RSS job
    if app.job_queue:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)
        log.info("RSS polling enabled: every %s sec", RSS_POLL_SECONDS)
    else:
        log.warning("JobQueue not available")


def main():
    app = build_app()
    app.post_init = post_init
    log.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

import asyncio
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

import feedparser
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# CONFIG (ENV + DEFAULTS)
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# RSS → channel
RSS_URL = os.getenv("RSS_URL", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@yomabar").strip()  # можно @channel или -100...
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "120"))

# Behavior
DELETE_QRAND_AFTER_SECONDS = int(os.getenv("DELETE_QRAND_AFTER_SECONDS", "30"))

# Links
ENABLE_MEDIA_FETCH = os.getenv("ENABLE_MEDIA_FETCH", "1").strip() != "0"
DELETE_VIDEO_LINK_AFTER_SECONDS = int(os.getenv("DELETE_VIDEO_LINK_AFTER_SECONDS", "0"))  # если хочешь удалять ссылки после скачивания

# Footer links (в посты RSS)
INSTAGRAM_URL = os.getenv("INSTAGRAM_URL", "https://www.instagram.com/yomabar.lt").strip()
FACEBOOK_URL = os.getenv("FACEBOOK_URL", "https://www.facebook.com/share/1P3dFJ5f5Y/").strip()
SITE_URL = os.getenv("SITE_URL", "https://www.yomahayoma.show/").strip()

# Storage
DATA_DIR = Path(os.getenv("DATA_DIR", ".")).resolve()
STATE_FILE = DATA_DIR / "state.json"

RULES_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)

START_TEXT = (
    "Бот работает 😎\n"
    "Команды: /rules /nick /rssstatus\n"
    f"Авто: удаляет /qrand через {DELETE_QRAND_AFTER_SECONDS}с, приветствует новых, бан-кнопка после выхода, RSS->канал,\n"
    "и пробует превращать ссылки Instagram/TikTok в видео/фото."
)

# =========================
# STATE
# =========================

@dataclass
class BotState:
    rss_seen: Dict[str, float] = field(default_factory=dict)  # id/link -> timestamp
    rss_last_posted: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rss_seen": self.rss_seen,
            "rss_last_posted": self.rss_last_posted,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "BotState":
        st = BotState()
        st.rss_seen = d.get("rss_seen", {}) or {}
        st.rss_last_posted = d.get("rss_last_posted")
        return st


STATE = BotState()


def load_state() -> None:
    global STATE
    try:
        if STATE_FILE.exists():
            STATE = BotState.from_dict(json.loads(STATE_FILE.read_text("utf-8")))
    except Exception:
        # если файл битый — просто начнем заново
        STATE = BotState()


def save_state() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(STATE.to_dict(), ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


# =========================
# HELPERS
# =========================

def is_supergroup(chat_type: Optional[str]) -> bool:
    return chat_type in ("supergroup", "channel")


async def is_user_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False


def normalize_nick(s: str) -> str:
    s = s.strip()
    # Telegram custom title max 16 символов (в твоем требовании)
    if len(s) > 16:
        s = s[:16]
    return s


INSTAGRAM_RE = re.compile(r"(https?://\S*instagram\.com/\S+)", re.IGNORECASE)
TIKTOK_RE = re.compile(r"(https?://\S*(tiktok\.com|vm\.tiktok\.com)/\S+)", re.IGNORECASE)


def extract_first_link(text: str) -> Optional[str]:
    if not text:
        return None
    m = INSTAGRAM_RE.search(text)
    if m:
        return m.group(1)
    m = TIKTOK_RE.search(text)
    if m:
        return m.group(1)
    return None


# =========================
# COMMANDS
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(START_TEXT)


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(RULES_TEXT)


async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not RSS_URL:
        await update.effective_message.reply_text("RSS не настроен (нет RSS_URL).")
        return
    count = len(STATE.rss_seen)
    last = STATE.rss_last_posted or "—"
    await update.effective_message.reply_text(f"RSS: включен ✅\nПроверка: каждые {RSS_POLL_SECONDS}с\nВидели записей: {count}\nПоследний: {last}")


async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type != "supergroup":
        await msg.reply_text("❌ /nick работает только в супер-группе.")
        return

    if not context.args:
        await msg.reply_text("Использование: /nick ТВОЙ_НИК (до 16 символов)")
        return

    title = normalize_nick(" ".join(context.args))
    if not title:
        await msg.reply_text("❌ Ник пустой.")
        return

    # Бот должен уметь повышать
    me = await context.bot.get_chat_member(chat.id, context.bot.id)
    if me.status != ChatMemberStatus.ADMINISTRATOR:
        await msg.reply_text("❌ Я не админ. Дай мне админку, чтобы ставить ники.")
        return
    # can_promote_members не всегда отдается как bool на всех клиентах,
    # но если Telegram запретит — поймаем ошибку.
    try:
        # 1) Сделать юзера админом БЕЗ прав (по максимуму выключено)
        # Иногда Telegram не любит 100% false — тогда упадет, и мы покажем причину.
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
            can_manage_topics=False,
        )

        # 2) Поставить кастомный титул
        await context.bot.set_chat_administrator_custom_title(chat_id=chat.id, user_id=user.id, custom_title=title)
        await msg.reply_text(f"✅ Готово. Твой ник: **{title}**", parse_mode=ParseMode.MARKDOWN)

    except BadRequest as e:
        await msg.reply_text(
            "❌ Не получилось поставить ник.\n"
            "Причина: Telegram не дал повысить/поставить титул.\n\n"
            f"Тех.деталь: {e}"
        )
    except Forbidden as e:
        await msg.reply_text(
            "❌ Нет прав.\n"
            "Проверь, что я админ и у меня есть право «Добавлять администраторов».\n\n"
            f"Тех.деталь: {e}"
        )


# =========================
# /qrand AUTO-DELETE
# =========================

async def maybe_schedule_delete_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    if not msg.text:
        return

    txt = msg.text.strip()
    if not txt.lower().startswith("/qrand"):
        return

    # админов не трогаем
    if update.effective_user and await is_user_admin(context, chat.id, update.effective_user.id):
        return

    async def do_delete(context_: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await context_.bot.delete_message(chat_id=chat.id, message_id=msg.message_id)
        except Exception:
            pass

    context.job_queue.run_once(lambda c: asyncio.create_task(do_delete(c)), when=DELETE_QRAND_AFTER_SECONDS)


# =========================
# WELCOME + LEFT + BAN BUTTON
# =========================

async def welcome_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return
    # Привет + правила
    await msg.reply_text(RULES_TEXT)


async def member_left(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    left = msg.left_chat_member
    if not left:
        return

    # Кнопка “Забанить”
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚫 Забанить", callback_data=f"ban:{left.id}")]]
    )
    name = (left.full_name or "пользователь").strip()
    await msg.reply_text(
        f"👋 **{name}** вышел(ла) из чата.\nАдмины, если это спамер — можно забанить кнопкой ниже.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def on_ban_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data:
        return
    await q.answer()

    chat = q.message.chat if q.message else None
    if not chat:
        return

    # кто нажал — должен быть админ
    clicker = q.from_user
    if not clicker or not await is_user_admin(context, chat.id, clicker.id):
        await q.edit_message_text("❌ Только админ может банить.")
        return

    parts = q.data.split(":", 1)
    if len(parts) != 2:
        return
    target_id = int(parts[1])

    try:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=target_id)
        await q.edit_message_text("✅ Забанен.")
    except Exception as e:
        await q.edit_message_text(f"❌ Не получилось забанить.\nТех.деталь: {e}")


# =========================
# RSS → CHANNEL
# =========================

def rss_entry_id(entry: Any) -> str:
    # стабильный ключ
    return str(entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title") or "")


def build_rss_post(entry: Any) -> str:
    title = (entry.get("title") or "").strip()
    link = (entry.get("link") or "").strip()
    summary = (entry.get("summary") or "").strip()

    # Чистим HTML чуть-чуть (rss.app обычно норм, но бывает мусор)
    summary = re.sub(r"<[^>]+>", "", summary).strip()

    lines = []
    if title:
        lines.append(f"**{title}**")
    if summary:
        # ограничим, чтобы не было простыни
        lines.append(summary[:800])
    if link:
        lines.append(f"\n🔗 {link}")

    lines.append(
        "\n"
        f"📸 Instagram: {INSTAGRAM_URL}\n"
        f"📘 Facebook: {FACEBOOK_URL}\n"
        f"🌐 Website: {SITE_URL}"
    )
    return "\n".join(lines)


async def rss_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not RSS_URL or not CHANNEL_USERNAME:
        return

    feed = feedparser.parse(RSS_URL)
    if not getattr(feed, "entries", None):
        return

    new_items: List[Any] = []
    now = time.time()

    for entry in feed.entries[:30]:
        key = rss_entry_id(entry)
        if not key:
            continue
        if key in STATE.rss_seen:
            continue
        STATE.rss_seen[key] = now
        new_items.append(entry)

    # сохраняем, чтобы не сломалось при рестарте
    save_state()

    if not new_items:
        return

    # Публикуем старые → новые (красивее)
    new_items.reverse()

    for entry in new_items:
        text = build_rss_post(entry)
        try:
            await context.bot.send_message(
                chat_id=CHANNEL_USERNAME,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=False,
            )
            STATE.rss_last_posted = entry.get("link") or entry.get("title") or "posted"
            save_state()
        except Exception:
            # если канал/права/parse_mode — просто пропустим
            pass


# =========================
# INSTAGRAM/TIKTOK → DOWNLOAD & SEND
# =========================

async def handle_media_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ENABLE_MEDIA_FETCH:
        return

    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    if not msg.text:
        return

    url = extract_first_link(msg.text)
    if not url:
        return

    # Чтобы бот не ловил свои же сообщения
    if msg.from_user and msg.from_user.is_bot:
        return

    status_msg = await msg.reply_text("⏳ Пытаюсь скачать…")

    try:
        files, note = await download_with_ytdlp(url)
        if not files:
            await status_msg.edit_text(
                "❌ Не получилось.\n"
                "Причина: Не получилось скачать. Возможно защита/блокировка или ссылка странная."
                + (f"\n\nТех.деталь: {note}" if note else "")
            )
            return

        # отправляем: если много — альбом, если 1 — одиночкой
        await send_files_as_media(update, context, files)

        try:
            await status_msg.delete()
        except Exception:
            pass

        # если нужно — удалить исходную ссылку через N секунд
        if DELETE_VIDEO_LINK_AFTER_SECONDS > 0:
            async def do_del(context_: ContextTypes.DEFAULT_TYPE) -> None:
                try:
                    await context_.bot.delete_message(chat_id=chat.id, message_id=msg.message_id)
                except Exception:
                    pass
            context.job_queue.run_once(lambda c: asyncio.create_task(do_del(c)), when=DELETE_VIDEO_LINK_AFTER_SECONDS)

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка.\nТех.деталь: {e}")


async def download_with_ytdlp(url: str) -> Tuple[List[Path], str]:
    """
    Возвращает список скачанных файлов и текстовую заметку/ошибку.
    """
    # yt-dlp скачивает в tempdir, мы отдадим файлы
    try:
        import yt_dlp
    except Exception as e:
        return [], f"yt-dlp не установлен: {e}"

    temp_dir = Path(tempfile.mkdtemp(prefix="media_"))
    outtmpl = str(temp_dir / "%(title).80s_%(id)s.%(ext)s")

    # Важно: без ffmpeg выбираем ОДИН формат (не merge)
    ydl_opts = {
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "best[ext=mp4]/best",  # одиночный файл
        "retries": 2,
        "fragment_retries": 2,
        "nocheckcertificate": True,
    }

    note = ""
    files: List[Path] = []

    def _collect_downloaded_files(folder: Path) -> List[Path]:
        all_files = []
        for p in folder.glob("*"):
            if p.is_file() and p.stat().st_size > 0:
                all_files.append(p)
        # сорт для стабильности
        all_files.sort(key=lambda x: x.stat().st_size, reverse=True)
        return all_files

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Если пост-карусель/несколько элементов — yt-dlp может вернуть entries
            # но файлы всё равно появятся в папке.
        files = _collect_downloaded_files(temp_dir)

        # Фильтруем мусор
        ok = []
        for f in files:
            ext = f.suffix.lower().lstrip(".")
            if ext in ("mp4", "mov", "mkv", "webm", "jpg", "jpeg", "png", "gif"):
                ok.append(f)
        files = ok

        if not files:
            note = "Файлы не появились после скачивания."
        return files[:10], note

    except Exception as e:
        # классическая ошибка без ffmpeg при merge — мы её уже избегаем, но оставим подсказку
        note = str(e)
        return [], note


async def send_files_as_media(update: Update, context: ContextTypes.DEFAULT_TYPE, files: List[Path]) -> None:
    msg = update.effective_message
    if not msg:
        return

    # Разделим по типам (если смешано — пошлём по одному)
    videos = [p for p in files if p.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm")]
    images = [p for p in files if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif")]

    # Если только 1 файл
    if len(files) == 1:
        p = files[0]
        await send_single_file(msg, p)
        return

    # Если много картинок — кинем пачкой (по одной, чтобы без лишних заморочек)
    # (альбом можно, но на мобиле часто “ломается” от разных форматов/размеров)
    for p in (videos + images):
        await send_single_file(msg, p)


async def send_single_file(reply_to_message, path: Path) -> None:
    try:
        ext = path.suffix.lower()
        with path.open("rb") as f:
            if ext in (".jpg", ".jpeg", ".png", ".gif"):
                await reply_to_message.reply_photo(photo=f)
            else:
                await reply_to_message.reply_video(video=f)
    except BadRequest as e:
        # если Telegram не принял как видео/фото — отправим как документ
        try:
            with path.open("rb") as f2:
                await reply_to_message.reply_document(document=f2, caption=f"Файл (как документ). Причина: {e}")
        except Exception:
            await reply_to_message.reply_text(f"❌ Не смог отправить файл. Тех.деталь: {e}")
    except Exception as e:
        await reply_to_message.reply_text(f"❌ Не смог отправить файл. Тех.деталь: {e}")


# =========================
# MAIN
# =========================

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # чтобы ошибки не “молчали”
    try:
        print("ERROR:", context.error)
    except Exception:
        pass


def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty")

    load_state()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("rssstatus", cmd_rssstatus))
    app.add_handler(CommandHandler("nick", cmd_nick))

    # qrand auto-delete
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), maybe_schedule_delete_qrand))

    # welcome/left
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, member_left))
    app.add_handler(CallbackQueryHandler(on_ban_button, pattern=r"^ban:\d+$"))

    # instagram/tiktok links
    # ловим любые тексты где есть instagram/tiktok/vm.tiktok
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"(instagram\.com/|tiktok\.com/|vm\.tiktok\.com/)"), handle_media_links))

    # errors
    app.add_error_handler(on_error)

    # RSS job
    if RSS_URL and CHANNEL_USERNAME:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)

    return app


def main() -> None:
    app = build_app()

    # ВАЖНО: Conflict бывает, если где-то еще запущен polling.
    # drop_pending_updates=True — чтобы после рестартов не “догонял” старые апдейты
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
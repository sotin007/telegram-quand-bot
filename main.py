import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List

import feedparser
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("yomabar-bot")

STATE_PATH = Path("bot_state.json")

# ====== НАСТРОЙКИ ======
DELETE_QRAND_AFTER_SECONDS = 30

WELCOME_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)

# Ссылки для подписи (можешь поменять)
SOCIALS_FOOTER = (
    "\n\n"
    "🔗 Instagram: https://www.instagram.com/yomabar.lt\n"
    "🔗 Facebook: https://www.facebook.com/share/1P3dFJ5f5Y/?mibextid=wwXIfr\n"
    "🔗 Сайт: https://www.yomahayoma.show/"
)

URL_RE = re.compile(
    r"(https?://[^\s]+)",
    re.IGNORECASE,
)

IG_RE = re.compile(r"(instagram\.com|instagr\.am)", re.IGNORECASE)
TT_RE = re.compile(r"(tiktok\.com)", re.IGNORECASE)

# yt-dlp часто меняется. Мы делаем настройки так, чтобы НЕ требовался ffmpeg для мержа.
YTDLP_FORMAT = "best[ext=mp4]/best"  # без merge


@dataclass
class BotState:
    rss_last_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {"rss_last_id": self.rss_last_id}

    @staticmethod
    def from_dict(d: dict) -> "BotState":
        return BotState(rss_last_id=d.get("rss_last_id"))


def load_state() -> BotState:
    if STATE_PATH.exists():
        try:
            return BotState.from_dict(json.loads(STATE_PATH.read_text("utf-8")))
        except Exception:
            pass
    return BotState()


def save_state(state: BotState) -> None:
    STATE_PATH.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), "utf-8")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def is_supergroup(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in ("supergroup",))


async def safe_delete_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass


# =======================
# Команды
# =======================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Бот работает 😎\n"
        "Команды: /rules /nick /rssstatus /ping\n"
        f"Авто: удаляет /qrand через {DELETE_QRAND_AFTER_SECONDS}с, "
        "приветствует новых, бан-кнопка после выхода, RSS→канал, "
        "и пытается превращать ссылки Instagram/TikTok в видео/фото."
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.message.reply_text(f"pong ✅\nchat_type={chat.type}\nchat_id={chat.id}")


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_TEXT)


async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data.get("state")  # type: ignore
    await update.message.reply_text(
        f"RSS_URL: {env_str('RSS_URL', '-')}\n"
        f"RSS_POLL_SECONDS: {env_int('RSS_POLL_SECONDS', 120)}\n"
        f"last_id: {state.rss_last_id}"
    )


async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Telegram custom title работает только в supergroup и только для админов
    if not is_supergroup(update):
        await update.message.reply_text("❌ /nick работает только в супер-группе.")
        return

    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    title = " ".join(context.args).strip()
    if not title:
        await update.message.reply_text("Напиши так: /nick Невеста")
        return

    if len(title) > 16:
        await update.message.reply_text("❌ Ник максимум 16 символов.")
        return

    # Проверим, что бот админ
    me = await context.bot.get_chat_member(chat.id, context.bot.id)
    if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await update.message.reply_text("❌ Я должен быть админом, чтобы ставить ники.")
        return

    try:
        # 1) делаем юзера админом с нулевыми правами (нужно для custom title)
        # NB: Telegram требует, чтобы бот имел право добавлять админов.
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
            can_manage_topics=False,
        )

        # 2) ставим кастомный титул
        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat.id,
            user_id=user.id,
            custom_title=title,
        )

        await update.message.reply_text(f"✅ Ник установлен: {title}")
    except BadRequest as e:
        await update.message.reply_text(
            "❌ Не получилось поставить ник.\n"
            "Чаще всего причина:\n"
            "— бот не админ\n"
            "— у бота нет права 'Добавлять администраторов'\n"
            "— группа не supergroup\n\n"
            f"Тех.деталь: {e}"
        )
    except TelegramError as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


# =======================
# Авто: /qrand удалить через N секунд
# =======================

async def on_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return
    # планируем удаление
    context.job_queue.run_once(
        lambda c: asyncio.create_task(safe_delete_message(c, msg.chat_id, msg.message_id)),
        when=DELETE_QRAND_AFTER_SECONDS,
    )


# =======================
# Приветствие новых
# =======================

async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.new_chat_members:
        return
    for m in msg.new_chat_members:
        name = (m.full_name or "новенький").strip()
        await msg.reply_text(f"{WELCOME_TEXT}\n\n👋 {name}")


# =======================
# Кнопка бана когда кто-то вышел
# =======================

async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.left_chat_member:
        return
    left = msg.left_chat_member
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚫 Забанить", callback_data=f"ban:{left.id}")]]
    )
    await msg.reply_text(
        f"👋 {left.full_name} вышел из чата.\nНужно забанить?",
        reply_markup=kb,
    )


async def on_ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.message:
        return
    await q.answer()

    m = re.match(r"^ban:(\d+)$", q.data or "")
    if not m:
        return
    user_id = int(m.group(1))
    chat_id = q.message.chat_id

    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await q.edit_message_text("✅ Забанил.")
    except TelegramError as e:
        await q.edit_message_text(f"❌ Не смог забанить: {e}")


# =======================
# RSS → канал
# =======================

async def rss_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    rss_url = env_str("RSS_URL")
    channel = env_str("CHANNEL_USERNAME") or env_str("CHANNEL_ID")

    if not rss_url or not channel:
        return

    state: BotState = context.application.bot_data.get("state")  # type: ignore
    try:
        feed = feedparser.parse(rss_url)
        if not feed.entries:
            return

        # Берем самый новый пост
        entry = feed.entries[0]
        entry_id = getattr(entry, "id", None) or getattr(entry, "link", None) or getattr(entry, "title", None)

        # анти-дубликаты
        if state.rss_last_id and entry_id == state.rss_last_id:
            return

        title = getattr(entry, "title", "Новый пост")
        link = getattr(entry, "link", "")
        summary = getattr(entry, "summary", "")

        text = f"📰 <b>{escape_html(title)}</b>\n"
        if summary:
            text += f"\n{strip_html(summary)[:800]}\n"
        if link:
            text += f"\n<a href=\"{link}\">Открыть</a>"

        text += SOCIALS_FOOTER

        await context.bot.send_message(
            chat_id=channel,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )

        state.rss_last_id = entry_id
        save_state(state)
    except Exception as e:
        log.exception("RSS tick error: %s", e)


def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def escape_html(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# =======================
# Ссылки IG/TT => скачать и отправить медиа
# =======================

def extract_urls_from_message(update: Update) -> List[str]:
    msg = update.message
    if not msg:
        return []
    text = msg.text or msg.caption or ""
    urls = set()

    # entities (если телега распознала как ссылку)
    entities = (msg.entities or []) + (msg.caption_entities or [])
    for ent in entities:
        if ent.type == "url":
            urls.add(text[ent.offset : ent.offset + ent.length])
        elif ent.type == "text_link" and ent.url:
            urls.add(ent.url)

    # fallback regex
    for m in URL_RE.finditer(text):
        urls.add(m.group(1))

    return [u.strip() for u in urls if u.strip()]


async def on_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return

    urls = extract_urls_from_message(update)
    if not urls:
        return

    # Берем только IG/TT
    target = None
    for u in urls:
        if IG_RE.search(u) or TT_RE.search(u):
            target = u
            break
    if not target:
        return

    status_msg = await msg.reply_text("⏳ Пытаюсь скачать...")

    try:
        sent = await download_and_send(context, msg.chat_id, target, reply_to=msg.message_id)
        if sent:
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Не получилось скачать (не нашёл файлов после скачивания).")
    except Exception as e:
        await status_msg.edit_text(
            "❌ Не получилось.\n"
            "Причина: Не получилось скачать. Возможно защита/блокировка или ссылка странная.\n\n"
            f"Тех.деталь: {e}"
        )


async def download_and_send(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    url: str,
    reply_to: Optional[int] = None,
) -> bool:
    """
    Качаем через yt-dlp в tmpdir, потом отправляем:
    - если 1 видео -> send_video
    - если 1 фото -> send_photo
    - если много -> media_group
    """
    # yt-dlp импортируем тут, чтобы бот стартовал даже если пакет не установлен (но он должен быть в requirements)
    import yt_dlp

    with tempfile.TemporaryDirectory() as td:
        outdir = Path(td)

        ydl_opts = {
            "outtmpl": str(outdir / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": YTDLP_FORMAT,
            "retries": 2,
            "socket_timeout": 15,
            "nocheckcertificate": True,
            # очень важно: без мержа, иначе ffmpeg нужен
            "merge_output_format": None,
            "postprocessors": [],
        }

        # Качаем в отдельном потоке, чтобы не блокировать async loop
        def _run():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)

        info = await asyncio.to_thread(_run)

        # Соберем файлы, которые реально скачались
        files = sorted(outdir.glob("*"))
        media_files = []
        for f in files:
            if f.is_file() and f.stat().st_size > 0:
                media_files.append(f)

        if not media_files:
            return False

        photos = [f for f in media_files if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
        videos = [f for f in media_files if f.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm")]

        # Если есть видео — отправляем видео (приоритет)
        if videos:
            # если несколько — шлём группой, но Telegram ограничивает
            if len(videos) == 1 and not photos:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=videos[0].read_bytes(),
                    caption="🎬",
                    reply_to_message_id=reply_to,
                )
                return True

            medias = []
            # максимум 10 в альбоме
            for f in (videos[:10]):
                medias.append(InputMediaVideo(media=f.read_bytes()))
            for f in (photos[:10 - len(medias)]):
                medias.append(InputMediaPhoto(media=f.read_bytes()))

            await context.bot.send_media_group(
                chat_id=chat_id,
                media=medias,
                reply_to_message_id=reply_to,
            )
            return True

        # Только фото
        if photos:
            if len(photos) == 1:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photos[0].read_bytes(),
                    caption="🖼️",
                    reply_to_message_id=reply_to,
                )
                return True

            medias = [InputMediaPhoto(media=f.read_bytes()) for f in photos[:10]]
            await context.bot.send_media_group(
                chat_id=chat_id,
                media=medias,
                reply_to_message_id=reply_to,
            )
            return True

        # Если что-то странное (например .json)
        return False


# =======================
# Main
# =======================

def build_app() -> Application:
    token = env_str("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is missing")

    app = Application.builder().token(token).build()

    # state
    app.bot_data["state"] = load_state()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("rssstatus", cmd_rssstatus))
    app.add_handler(CommandHandler("nick", cmd_nick))

    # callbacks
    app.add_handler(CallbackQueryHandler(on_ban_callback, pattern=r"^ban:\d+$"))

    # membership updates
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))

    # auto delete /qrand
    app.add_handler(MessageHandler(filters.Regex(r"^/qrand(@\w+)?(\s|$)"), on_qrand))

    # links IG/TT (в тексте или подписи)
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_links))
    # на случай если ссылка пришла "командой" или с пробелами — всё равно обработаем
    app.add_handler(MessageHandler(filters.Regex(r"https?://"), on_links))

    return app


def main() -> None:
    app = build_app()

    # RSS job
    rss_url = env_str("RSS_URL")
    channel = env_str("CHANNEL_USERNAME") or env_str("CHANNEL_ID")
    poll = env_int("RSS_POLL_SECONDS", 120)

    if rss_url and channel:
        app.job_queue.run_repeating(rss_tick, interval=poll, first=10)
        log.info("RSS enabled: %s -> %s every %ss", rss_url, channel, poll)
    else:
        log.info("RSS disabled (RSS_URL or CHANNEL not set).")

    # LONG POLLING
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
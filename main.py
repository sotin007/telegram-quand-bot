import asyncio
import html
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple

import feedparser
from yt_dlp import YoutubeDL

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.constants import ChatAction
from telegram.error import TelegramError, BadRequest
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
# ENV / НАСТРОЙКИ
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@yomabar").strip()  # куда кидать RSS
RSS_URL = os.getenv("RSS_URL", "").strip()
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "120").strip() or 120)

DELETE_QRAND_AFTER_SECONDS = int(os.getenv("DELETE_QRAND_AFTER_SECONDS", "30").strip() or 30)

# ссылки в конце поста (подпись к RSS-постам)
LINK_INSTAGRAM = os.getenv("LINK_INSTAGRAM", "https://www.instagram.com/yomabar.lt").strip()
LINK_FACEBOOK = os.getenv("LINK_FACEBOOK", "https://www.facebook.com/").strip()
LINK_SITE = os.getenv("LINK_SITE", "https://www.yomahayoma.show/").strip()

STATE_FILE = os.getenv("STATE_FILE", "state.json").strip()

MAX_MEDIA_GROUP = 10  # Telegram media group limit is 10

WELCOME_AND_RULES = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)

# =========================
# REGEX / URL
# =========================
URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)

INSTAGRAM_RE = re.compile(r"https?://(www\.)?instagram\.com/(p|reel|tv)/", re.IGNORECASE)
TIKTOK_RE = re.compile(r"https?://(www\.)?(tiktok\.com/|vm\.tiktok\.com/|vt\.tiktok\.com/)", re.IGNORECASE)

# =========================
# STATE
# =========================
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# =========================
# HELPERS
# =========================
def extract_urls(text: str) -> List[str]:
    if not text:
        return []
    urls = URL_RE.findall(text)
    cleaned = []
    for u in urls:
        u = u.strip().strip(").,]!?>\"'")
        cleaned.append(u)
    return cleaned

def is_video_file(p: Path) -> bool:
    return p.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}

def is_image_file(p: Path) -> bool:
    return p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}

def pick_downloaded_files(tmpdir: Path) -> List[Path]:
    files = []
    for p in tmpdir.iterdir():
        if p.is_file() and not p.name.endswith(".part"):
            files.append(p)
    files.sort(key=lambda x: x.name)
    return files

def ytdlp_options_for(url: str, outtmpl: str) -> dict:
    """
    Главное:
    - НЕ форсим bestvideo+bestaudio (это ломает IG фотопосты и может требовать ffmpeg)
    - Берём "best": yt-dlp сам решит что скачать (фото/видео)
    """
    fmt = "best"
    if TIKTOK_RE.search(url):
        fmt = "best[ext=mp4]/best"  # чуть лучше для TikTok видео

    return {
        "outtmpl": outtmpl,
        "noplaylist": False,          # IG карусели приходят как playlist
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "socket_timeout": 20,
        "format": fmt,
        "merge_output_format": None,  # не мерджить
        "postprocessors": [],         # без ffmpeg
        "overwrites": True,
        "restrictfilenames": False,
    }

async def download_media_from_url(url: str) -> Tuple[List[Path], Optional[str]]:
    """
    Возвращает (files, error_text). Если error_text != None — не удалось.
    """
    def _blocking_download() -> Tuple[List[Path], Optional[str]]:
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            outtmpl = str(tmpdir / "%(title).80s_%(id)s.%(ext)s")
            try:
                with YoutubeDL(ytdlp_options_for(url, outtmpl)) as ydl:
                    ydl.extract_info(url, download=True)

                files = pick_downloaded_files(tmpdir)
                if not files:
                    return [], "Ничего не скачалось (пустой результат)."

                # переносим в “живую” temp-папку, чтобы файлы не исчезли до отправки
                persist_dir = Path(tempfile.mkdtemp(prefix="tg_media_"))
                persisted = []
                for f in files[:MAX_MEDIA_GROUP]:
                    new_path = persist_dir / f.name
                    f.replace(new_path)
                    persisted.append(new_path)

                return persisted, None
            except Exception as e:
                return [], str(e)

    return await asyncio.to_thread(_blocking_download)

async def cleanup_files(files: List[Path]) -> None:
    if not files:
        return
    try:
        base = files[0].parent
        for f in base.iterdir():
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            base.rmdir()
        except Exception:
            pass
    except Exception:
        pass

async def send_downloaded_media(update: Update, context: ContextTypes.DEFAULT_TYPE, files: List[Path]) -> None:
    chat_id = update.effective_chat.id

    # media group (карусель)
    if len(files) > 1:
        media = []
        for p in files[:MAX_MEDIA_GROUP]:
            if is_video_file(p):
                media.append(InputMediaVideo(media=open(p, "rb")))
            elif is_image_file(p):
                media.append(InputMediaPhoto(media=open(p, "rb")))
        if media:
            await context.bot.send_media_group(chat_id=chat_id, media=media)
        else:
            await update.effective_message.reply_text("❌ Скачалось, но формат файлов не распознан.")
        return

    # одиночный
    p = files[0]
    if is_video_file(p):
        await context.bot.send_video(chat_id=chat_id, video=open(p, "rb"))
    elif is_image_file(p):
        await context.bot.send_photo(chat_id=chat_id, photo=open(p, "rb"))
    else:
        await update.effective_message.reply_text(f"❌ Скачалось, но расширение непонятно: {p.suffix}")

# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Бот работает 😎\n"
        "Команды: /rules /nick /rssstatus /ping\n"
        f"Авто: удаляет /qrand через {DELETE_QRAND_AFTER_SECONDS}с, приветствует новых, бан-кнопка после выхода,\n"
        "RSS->канал, и пытается превращать ссылки Instagram/TikTok в видео/фото."
    )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.message.reply_text(f"pong ✅\nchat_type={chat.type}\nchat_id={chat.id}")

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_AND_RULES)

async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    last_id = state.get("rss_last_id")
    await update.message.reply_text(
        "RSS статус:\n"
        f"RSS_URL: {RSS_URL or '(не задан)'}\n"
        f"CHANNEL: {CHANNEL_USERNAME}\n"
        f"POLL: {RSS_POLL_SECONDS}s\n"
        f"last_id: {last_id}"
    )

async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != "supergroup":
        await msg.reply_text("❌ /nick работает только в супер-группе.")
        return

    if not context.args:
        await msg.reply_text("Использование: /nick <титул до 16 символов>")
        return

    title = " ".join(context.args).strip()
    if len(title) > 16:
        await msg.reply_text("❌ Ник слишком длинный. Максимум 16 символов.")
        return
    if not title:
        await msg.reply_text("❌ Пустой ник нельзя.")
        return

    try:
        # делаем пользователя админом с минимальными правами
        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            can_manage_chat=False,
            can_change_info=False,
            can_post_messages=False,
            can_edit_messages=False,
            can_delete_messages=False,
            can_invite_users=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_manage_topics=False,
            can_promote_members=False,
            can_manage_video_chats=False,
            can_post_stories=False,
            can_edit_stories=False,
            can_delete_stories=False,
        )

        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat.id,
            user_id=user.id,
            custom_title=title
        )

        await msg.reply_text(f"✅ Готово! Твой ник: {title}")

    except BadRequest as e:
        await msg.reply_text(
            "❌ Не получилось поставить ник.\n"
            "Проверь, что бот — админ и у него есть право 'Добавлять администраторов'.\n\n"
            f"Тех.деталь: {e}"
        )
    except TelegramError as e:
        await msg.reply_text(f"❌ Ошибка Telegram: {e}")

# =========================
# AUTO: delete /qrand
# =========================
async def _delete_message_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    chat_id = job.data["chat_id"]
    message_id = job.data["message_id"]
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass

async def on_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    # запланировать удаление
    context.job_queue.run_once(
        _delete_message_job,
        when=DELETE_QRAND_AFTER_SECONDS,
        data={"chat_id": msg.chat_id, "message_id": msg.message_id},
        name=f"del_qrand_{msg.chat_id}_{msg.message_id}",
    )

# =========================
# AUTO: welcome + leave ban button
# =========================
BAN_CB_PREFIX = "ban_user:"

async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu = update.chat_member
    chat = cmu.chat
    old = cmu.old_chat_member
    new = cmu.new_chat_member

    # JOIN
    if old.status in ("left", "kicked") and new.status in ("member", "restricted"):
        name = html.escape(new.user.full_name or "друг")
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"{WELCOME_AND_RULES}\n\n👋 Привет, {name}!"
        )
        return

    # LEAVE
    if old.status in ("member", "administrator", "restricted") and new.status in ("left", "kicked"):
        left_user = old.user
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚫 Забанить", callback_data=f"{BAN_CB_PREFIX}{left_user.id}")
        ]])
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"👋 {left_user.full_name} вышел(ла). Если это спамер — можно забанить:",
            reply_markup=kb,
        )

async def on_ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    data = q.data or ""
    if not data.startswith(BAN_CB_PREFIX):
        return

    chat_id = q.message.chat_id
    try:
        user_id = int(data.split(":", 1)[1])
    except Exception:
        await q.edit_message_text("❌ Некорректные данные.")
        return

    # проверяем, что нажимает админ
    try:
        me = q.from_user
        member = await context.bot.get_chat_member(chat_id, me.id)
        if member.status not in ("administrator", "creator"):
            await q.edit_message_text("❌ Это может делать только админ.")
            return
    except TelegramError:
        await q.edit_message_text("❌ Не смог проверить права.")
        return

    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await q.edit_message_text("✅ Забанил.")
    except TelegramError as e:
        await q.edit_message_text(f"❌ Не получилось забанить: {e}")

# =========================
# AUTO: Instagram/TikTok links -> media
# =========================
async def on_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    # не реагируем на команды
    if msg.text and msg.text.startswith("/"):
        return

    urls = extract_urls(text)
    if not urls:
        return

    target = None
    for u in urls:
        if INSTAGRAM_RE.search(u) or TIKTOK_RE.search(u):
            target = u
            break

    if not target:
        return

    # =========================
    # 🚫 ЕСЛИ ЭТО ФОТО — НЕ КАЧАЕМ
    # =========================
    if "instagram.com/p/" in target.lower():
        await msg.reply_text(
            "Сори брат да? Я ещё не умею качать фотки, "
            "давай как то без меня, всё пока 👋"
        )
        return

    if "tiktok.com" in target.lower() and "/photo/" in target.lower():
        await msg.reply_text(
            "Сори брат да? Я ещё не умею качать фотки, "
            "давай как то без меня, всё пока 👋"
        )
        return

    # =========================
    # 🎬 ИНАЧЕ ПЫТАЕМСЯ КАЧАТЬ (reel / видео)
    # =========================
    status = await msg.reply_text("⏳ Пытаюсь скачать...")

    files = []
    try:
        files, err = await download_media_from_url(target)

        if err:
            await status.edit_text(
                "❌ Не получилось скачать видео.\n\n"
                f"Тех.деталь: {err}"
            )
            return

        await status.delete()
        await send_downloaded_media(update, context, files)

    finally:
        await cleanup_files(files)

# =========================
# RSS -> CHANNEL
# =========================
def rss_extract_image(entry) -> Optional[str]:
    # разные варианты в RSS
    if getattr(entry, "media_content", None):
        try:
            return entry.media_content[0].get("url")
        except Exception:
            pass
    if getattr(entry, "media_thumbnail", None):
        try:
            return entry.media_thumbnail[0].get("url")
        except Exception:
            pass
    if getattr(entry, "enclosures", None):
        try:
            enc = entry.enclosures[0]
            return enc.get("href") or enc.get("url")
        except Exception:
            pass
    return None

def rss_caption(entry) -> str:
    title = getattr(entry, "title", "") or ""
    link = getattr(entry, "link", "") or ""
    summ = getattr(entry, "summary", "") or ""

    title = html.unescape(title).strip()
    summ = re.sub("<[^<]+?>", "", html.unescape(summ)).strip()  # убрать HTML

    parts = []
    if title:
        parts.append(f"<b>{html.escape(title)}</b>")
    if summ:
        parts.append(html.escape(summ))

    if link:
        parts.append(f"\n<a href=\"{html.escape(link)}\">Открыть пост</a>")

    parts.append("\n\n<b>Источник</b>")
    parts.append(f"Instagram | Facebook | Site")
    parts.append(f"{LINK_INSTAGRAM}\n{LINK_FACEBOOK}\n{LINK_SITE}")

    return "\n".join(parts)

async def rss_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not RSS_URL:
        return

    state = load_state()
    last_id = state.get("rss_last_id")

    feed = feedparser.parse(RSS_URL)
    if not feed.entries:
        return

    # идём от старых к новым, чтобы в канал улетело по порядку
    entries = list(feed.entries)[::-1]

    new_seen = last_id
    posted_any = False

    for entry in entries:
        entry_id = getattr(entry, "id", None) or getattr(entry, "guid", None) or getattr(entry, "link", None)
        if not entry_id:
            continue

        if last_id and entry_id == last_id:
            # дошли до последнего отправленного
            new_seen = last_id
            posted_any = posted_any
            continue

        # пока last_id не встретили — считаем новыми
        img = rss_extract_image(entry)
        cap = rss_caption(entry)

        try:
            if img:
                await context.bot.send_photo(
                    chat_id=CHANNEL_USERNAME,
                    photo=img,
                    caption=cap,
                    parse_mode="HTML",
                )
            else:
                await context.bot.send_message(
                    chat_id=CHANNEL_USERNAME,
                    text=cap,
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
            posted_any = True
            new_seen = entry_id
        except TelegramError:
            # если не получилось отправить конкретный — просто пропускаем
            continue

    if posted_any and new_seen:
        state["rss_last_id"] = new_seen
        save_state(state)

# =========================
# MAIN
# =========================
def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("rssstatus", cmd_rssstatus))
    app.add_handler(CommandHandler("nick", cmd_nick))
    app.add_handler(CommandHandler("ping", cmd_ping))

    # qrand delete (сообщения-команды в чате)
    app.add_handler(MessageHandler(filters.Regex(r"^/qrand(\s|$)"), on_qrand), group=5)

    # welcome/leave
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # ban button callback
    app.add_handler(CallbackQueryHandler(on_ban_callback, pattern=f"^{re.escape(BAN_CB_PREFIX)}"))

    # links -> download
    app.add_handler(MessageHandler(filters.ALL, on_links), group=50)

    # RSS job
    if RSS_URL:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)

    return app

def main() -> None:
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
import asyncio
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import feedparser
import httpx
from yt_dlp import YoutubeDL

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.error import Forbidden, BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -----------------------
# LOGGING
# -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("yomabar-bot")

# -----------------------
# ENV / CONFIG
# -----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

RULES_TEXT = os.getenv(
    "RULES_TEXT",
    "😺😳😨😏 Добро пожаловать в наш клаб хаус😏😨😳😺\n\n"
    "🤩🥺 Наши правила:🥺🤩\n"
    "😖🫂 Без политики! 🫂😖\n"
    "🦉🤯 Не обижать друг друга!🤯🦉"
).strip()

DELETE_GRAND_AFTER_SECONDS = int(os.getenv("DELETE_GRAND_AFTER_SECONDS", "30"))

# RSS -> CHANNEL
RSS_URL = os.getenv("RSS_URL", "").strip()
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "300"))  # 5 минут
RSS_CHANNEL_ID = os.getenv("RSS_CHANNEL_ID", "").strip()      # например: -100123...

# Кнопки под RSS постом
BTN_INSTAGRAM = os.getenv("BTN_INSTAGRAM", "").strip()
BTN_FACEBOOK = os.getenv("BTN_FACEBOOK", "").strip()
BTN_SITE = os.getenv("BTN_SITE", "").strip()

# Nick storage (простая локальная база в файле)
NICKS_FILE = os.getenv("NICKS_FILE", "nicks.json").strip()

PHOTO_SORRY_TEXT = "Сори брат да? Я ещё не умею качать фотки, давай как то без меня, всё пока 👋"

# -----------------------
# REGEXES
# -----------------------
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

IG_RE = re.compile(r"https?://(www\.)?instagram\.com/\S+", re.IGNORECASE)
TT_RE = re.compile(r"https?://(vm\.)?tiktok\.com/\S+|https?://www\.tiktok\.com/\S+", re.IGNORECASE)

# Явные фото-ссылки TikTok (после распаковки редиректов там обычно /photo/)
TT_PHOTO_HINT_RE = re.compile(r"/photo/", re.IGNORECASE)

# -----------------------
# HELPERS
# -----------------------
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

async def resolve_final_url(url: str) -> str:
    """Разворачиваем короткие ссылки (vm.tiktok.com и т.п.)."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=10.0,
            headers={"User-Agent": "Mozilla/5.0"}
        ) as client:
            r = await client.get(url)
            return str(r.url)
    except Exception:
        return url

def extract_urls(text: str) -> List[str]:
    if not text:
        return []
    return URL_RE.findall(text)

def ytldp_options(outtmpl: str, url: str) -> dict:
    """
    Без ffmpeg:
      - берём один лучший mp4 (если есть), иначе best.
      - не мерджим форматы, не делаем постпроцессинг.
    """
    fmt = "best"
    # чуть лучше для TikTok (часто есть mp4)
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
        "merge_output_format": None,   # важно: иначе попросит ffmpeg
        "postprocessors": [],          # без ffmpeg
        "overwrites": True,
        "restrictfilenames": False,
    }

def pick_downloaded_files(folder: Path) -> List[Path]:
    if not folder.exists():
        return []
    files = []
    for p in folder.rglob("*"):
        if p.is_file():
            # отфильтруем мусор
            if p.suffix.lower() in [".mp4", ".mov", ".mkv", ".webm", ".m4v", ".jpg", ".jpeg", ".png"]:
                files.append(p)
    # самые свежие первыми
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return files

def friendly_block_reason(err_text: str) -> Optional[str]:
    """
    Превращаем типовые ошибки Instagram/TikTok в понятные причины.
    """
    t = (err_text or "").lower()

    # Instagram sensitive / audience
    if "unavailable for certain audiences" in t or "inappropriate" in t or "sensitive" in t:
        return (
            "Чаще всего если пост:\n"
            "• 🔞 18+ / sensitive content\n"
            "• 🔒 только для залогиненных\n"
            "• 🚫 ограничен по региону\n"
            "• 👤 аккаунт private\n"
            "• ⚠️ Instagram пометил как sensitive"
        )

    # Private / login
    if "login" in t or "cookie" in t or "sign in" in t:
        return (
            "Похоже контент доступен только залогиненным.\n\n"
            "Чаще всего если пост:\n"
            "• 🔒 только для залогиненных\n"
            "• 👤 аккаунт private"
        )

    # Region
    if "not available in your country" in t or "geo" in t or "region" in t:
        return (
            "Похоже контент ограничен по региону.\n\n"
            "• 🚫 ограничен по региону"
        )

    return None

@dataclass
class DownloadResult:
    files: List[Path]
    error: Optional[str]

async def download_media_from_url(url: str) -> DownloadResult:
    """
    Скачиваем через yt-dlp в temp dir.
    Возвращаем список файлов или текст ошибки.
    """
    def _blocking() -> DownloadResult:
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            outtmpl = str(tmpdir / "%(title).80s_%(id)s.%(ext)s")

            try:
                with YoutubeDL(ytldp_options(outtmpl, url)) as ydl:
                    ydl.extract_info(url, download=True)

                files = pick_downloaded_files(tmpdir)
                # переносим из temp в новый temp, который живёт чуть дольше? —
                # проще: вернём путь к файлам, но они исчезнут после выхода.
                # Поэтому: копируем в ещё один temp, который держим снаружи.
                # Чтобы не усложнять: вместо копий — скачиваем в NamedTemporaryFile-папку выше.
                # Тут сделаем хитро: сразу создадим отдельную папку в /tmp и туда качаем.

                return DownloadResult(files=files, error=None)
            except Exception as e:
                return DownloadResult(files=[], error=str(e))

    # Но нам нужны файлы, которые не исчезнут.
    # Делаем отдельную папку в системном tmp и чистим вручную позже.
    base = Path(tempfile.mkdtemp(prefix="yomabar_dl_"))
    outtmpl = str(base / "%(title).80s_%(id)s.%(ext)s")

    def _blocking2() -> DownloadResult:
        try:
            with YoutubeDL(ytldp_options(outtmpl, url)) as ydl:
                ydl.extract_info(url, download=True)
            files = pick_downloaded_files(base)
            if not files:
                return DownloadResult(files=[], error="Не нашёл скачанный файл (yt-dlp ничего не сохранил).")
            return DownloadResult(files=files, error=None)
        except Exception as e:
            return DownloadResult(files=[], error=str(e))

    res: DownloadResult = await asyncio.to_thread(_blocking2)

    # если ошибка — чистим папку
    if res.error:
        try:
            for p in base.rglob("*"):
                if p.is_file():
                    p.unlink(missing_ok=True)
            base.rmdir()
        except Exception:
            pass

    return res

async def safe_cleanup(paths: List[Path]):
    try:
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        # удалить родительскую папку если пустая
        if paths:
            parent = paths[0].parent
            try:
                for _ in parent.rglob("*"):
                    # если что-то осталось — не трогаем
                    return
                parent.rmdir()
            except Exception:
                pass
    except Exception:
        pass

# -----------------------
# NICKS
# -----------------------
def get_nicks() -> Dict[str, str]:
    return load_json(NICKS_FILE, {})

def set_nick(user_id: int, nick: str):
    data = get_nicks()
    data[str(user_id)] = nick
    save_json(NICKS_FILE, data)

# -----------------------
# COMMANDS
# -----------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот работает 😎\n"
        "Команды: /rules /nick /ping\n"
        f"Авто: удаляет /grand через {DELETE_GRAND_AFTER_SECONDS}с, RSS->канал,"
        " и пробует превращать ссылки Instagram/TikTok в видео."
    )

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RULES_TEXT)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        f"pong ✅\nchat_type={chat.type}\nchat_id={chat.id}"
    )

async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != ChatType.SUPERGROUP:
        await msg.reply_text("❌ /nick работает только в супер-группе.")
        return

    nick = " ".join(context.args).strip()
    if not nick:
        await msg.reply_text("Напиши так: /nick ТВОЙ_НИК")
        return

    # сохраняем в свою базу
    set_nick(user.id, nick)

    # если пользователь админ — попробуем выставить Custom Title (нужны права у бота)
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status in ("administrator", "creator"):
            await context.bot.set_chat_administrator_custom_title(
                chat_id=chat.id,
                user_id=user.id,
                custom_title=nick[:16],  # TG ограничение на длину тайтла
            )
            await msg.reply_text(f"✅ Ник поставил: {nick}")
            return
    except Forbidden:
        await msg.reply_text("⚠️ Ник сохранил, но Telegram не дал выставить титул (не хватает прав у бота).")
        return
    except BadRequest as e:
        await msg.reply_text(f"⚠️ Ник сохранил, но Telegram ругнулся: {e}")
        return
    except Exception:
        pass

    await msg.reply_text(f"✅ Ник сохранил: {nick}")

# -----------------------
# AUTO: delete /grand
# -----------------------
async def delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    message_id = job.data["message_id"]
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def on_grand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    context.job_queue.run_once(
        delete_message_job,
        when=DELETE_GRAND_AFTER_SECONDS,
        data={"chat_id": msg.chat_id, "message_id": msg.message_id},
        name=f"del_{msg.chat_id}_{msg.message_id}"
    )

# -----------------------
# LINKS HANDLER
# -----------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text or ""
    urls = extract_urls(text)

    if not urls:
        return

    # берём первую ссылку (если хочешь — можно цикл по всем)
    url = urls[0]
    final_url = await resolve_final_url(url)

    is_instagram = bool(IG_RE.search(final_url))
    is_tiktok = bool(TT_RE.search(final_url))

    if not (is_instagram or is_tiktok):
        return

    # 1) TikTok photo: часто только после редиректа видно /photo/
    if is_tiktok and TT_PHOTO_HINT_RE.search(final_url):
        await msg.reply_text(PHOTO_SORRY_TEXT)
        return

    # 2) Instagram /p/ может быть фото/карусель (yt-dlp скажет "There is no video in this post")
    #    Тут сначала попробуем скачать; если ошибка "no video" — скажем PHOTO_SORRY_TEXT.

    await msg.reply_text("⏳ Пытаюсь скачать...")

    res = await download_media_from_url(final_url)
    if res.error:
        err = res.error

        # 2a) IG post without video -> фото/карусель
        if is_instagram and ("there is no video in this post" in err.lower()):
            await msg.reply_text(PHOTO_SORRY_TEXT)
            return

        # 2b) TikTok photo иногда даёт Unsupported URL — тоже считаем “фото”
        if is_tiktok and ("unsupported url" in err.lower()):
            await msg.reply_text(PHOTO_SORRY_TEXT)
            return

        # 2c) IG sensitive/audience/etc -> красивое объяснение
        friendly = friendly_block_reason(err)
        if friendly:
            await msg.reply_text("❌ Не получилось.\nПричина: Контент ограничен.\n\n" + friendly)
            return

        # дефолт
        await msg.reply_text(
            "❌ Не получилось.\n"
            "Причина: Не получилось скачать. Возможно защита/блокировка или ссылка странная.\n\n"
            f"Тех.деталь: {err}"
        )
        return

    # отправка: если первый файл видео — видео; если картинка — не умеем (чтобы не обещать)
    files = res.files[:1]
    fp = files[0]

    try:
        if fp.suffix.lower() in [".jpg", ".jpeg", ".png"]:
            await msg.reply_text(PHOTO_SORRY_TEXT)
        else:
            await msg.reply_video(video=fp.open("rb"))
    finally:
        await safe_cleanup(files)

# -----------------------
# RSS -> CHANNEL (dedupe)
# -----------------------
RSS_STATE_FILE = os.getenv("RSS_STATE_FILE", "rss_state.json").strip()

def load_rss_state() -> Dict[str, List[str]]:
    return load_json(RSS_STATE_FILE, {"seen": []})

def save_rss_state(state: Dict[str, List[str]]):
    save_json(RSS_STATE_FILE, state)

def rss_keyboard() -> Optional[InlineKeyboardMarkup]:
    buttons = []
    row = []
    if BTN_INSTAGRAM:
        row.append(InlineKeyboardButton("Instagram", url=BTN_INSTAGRAM))
    if BTN_FACEBOOK:
        row.append(InlineKeyboardButton("Facebook", url=BTN_FACEBOOK))
    if BTN_SITE:
        row.append(InlineKeyboardButton("Site", url=BTN_SITE))
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons) if buttons else None

async def rss_tick(context: ContextTypes.DEFAULT_TYPE):
    if not RSS_URL or not RSS_CHANNEL_ID:
        return

    state = load_rss_state()
    seen = set(state.get("seen", []))

    feed = feedparser.parse(RSS_URL)
    if not getattr(feed, "entries", None):
        return

    # новые -> старые (в конце отправим по порядку)
    new_entries = []
    for e in feed.entries[:20]:
        key = (getattr(e, "id", None) or getattr(e, "guid", None) or getattr(e, "link", None) or "").strip()
        if not key:
            continue
        if key in seen:
            continue
        new_entries.append((key, e))

    if not new_entries:
        return

    # отправим в нормальном порядке (от старых к новым)
    new_entries.reverse()

    kb = rss_keyboard()
    for key, e in new_entries:
        title = (getattr(e, "title", "") or "").strip()
        link = (getattr(e, "link", "") or "").strip()

        text = title if title else "Новый пост"
        if link:
            text = f"{text}\n{link}"

        try:
            await context.bot.send_message(
                chat_id=int(RSS_CHANNEL_ID),
                text=text,
                reply_markup=kb,
                disable_web_page_preview=False,
            )
        except Exception as ex:
            log.warning("RSS send failed: %s", ex)
            break

        seen.add(key)

    # ограничим размер истории
    state["seen"] = list(seen)[-500:]
    save_rss_state(state)

# -----------------------
# MAIN
# -----------------------
def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set it in Railway Variables.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("nick", cmd_nick))

    # auto delete /grand
    app.add_handler(MessageHandler(filters.Regex(r"^/grand\b"), on_grand))

    # links
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # RSS job
    if RSS_URL and RSS_CHANNEL_ID:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)

    return app

def main():
    app = build_app()
    # drop_pending_updates=True помогает после деплоя/перезапуска
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
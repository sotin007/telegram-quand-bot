import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import List, Optional, Set, Tuple

import feedparser
import httpx
from yt_dlp import YoutubeDL

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
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
# CONFIG (env variables)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Канал куда постить RSS: может быть @yomabar или числовой id
CHANNEL_ID = os.getenv("CHANNEL_ID", "@yomabar").strip()

RSS_URL = os.getenv("RSS_URL", "").strip()
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "120"))

# удалять сообщение со ссылкой после успешной перезаливки
DELETE_VIDEO_LINK_AFTER_SECONDS = int(os.getenv("DELETE_VIDEO_LINK_AFTER_SECONDS", "30"))

# ссылки в кнопках
INSTAGRAM_URL = os.getenv("INSTAGRAM_URL", "https://www.instagram.com/yomabar.lt").strip()
FACEBOOK_URL = os.getenv("FACEBOOK_URL", "https://www.facebook.com/share/1P3dFJ5f5Y/?mibextid=wwXIfr").strip()
SITE_URL = os.getenv("SITE_URL", "https://www.yomahayoma.show/").strip()

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("yomabar-bot")

# =========================
# RULES TEXT
# =========================
RULES_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)

PHOTO_SORRY_TEXT = "Сори брат да? Я ещё не умею качать фотки, давай как то без меня, всё пока 👋"

# =========================
# RSS anti-duplicate storage
# =========================
SENT_IDS_FILE = Path("sent_ids.json")
MAX_SENT_IDS = 800


def load_sent_ids() -> Set[str]:
    try:
        if SENT_IDS_FILE.exists():
            return set(json.loads(SENT_IDS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return set()


def save_sent_ids(sent_ids: Set[str]) -> None:
    try:
        data = list(sent_ids)[-MAX_SENT_IDS:]
        SENT_IDS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


SENT_IDS: Set[str] = load_sent_ids()

# =========================
# URL detect
# =========================
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

TIKTOK_RE = re.compile(r"(https?://)?(www\.)?(vm\.)?tiktok\.com/|tiktok\.com/", re.IGNORECASE)
INSTAGRAM_RE = re.compile(r"(https?://)?(www\.)?instagram\.com/", re.IGNORECASE)

TIKTOK_PHOTO_RE = re.compile(r"/photo/\d+", re.IGNORECASE)
INSTAGRAM_POST_RE = re.compile(r"instagram\.com/p/([A-Za-z0-9_-]+)", re.IGNORECASE)


def extract_urls(text: str) -> List[str]:
    return URL_RE.findall(text or "")


async def resolve_final_url(url: str) -> str:
    # разворачиваем vm.tiktok и прочие редиректы
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


def make_links_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Instagram", url=INSTAGRAM_URL)],
            [InlineKeyboardButton("Facebook", url=FACEBOOK_URL)],
            [InlineKeyboardButton("Site", url=SITE_URL)],
        ]
    )


# =========================
# yt-dlp download
# =========================
def ytdlp_options(outtmpl: str, is_tiktok: bool) -> dict:
    # ВАЖНО: без ffmpeg-мерджа, чтобы не падало на Railway
    # TikTok часто лучше брать mp4
    fmt = "best"
    if is_tiktok:
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


def pick_downloaded_files(tmpdir: Path) -> List[Path]:
    files = []
    for p in tmpdir.rglob("*"):
        if p.is_file() and p.suffix.lower() in {".mp4", ".m4a", ".webm", ".mov"}:
            files.append(p)
    files.sort(key=lambda x: x.stat().st_size if x.exists() else 0, reverse=True)
    return files


async def download_media_from_url(url: str) -> Tuple[Optional[Path], Optional[str]]:
    """
    Возвращает (file_path, error_text). Если error_text != None => ошибка.
    """
    is_tiktok = bool(TIKTOK_RE.search(url))

    def _blocking() -> Tuple[Optional[Path], Optional[str]]:
        with tempfile.TemporaryDirectory() as td:
            tmpdir = Path(td)
            outtmpl = str(tmpdir / "%(title).80s_%(id)s.%(ext)s")

            try:
                with YoutubeDL(ytdlp_options(outtmpl, is_tiktok=is_tiktok)) as ydl:
                    ydl.extract_info(url, download=True)

                files = pick_downloaded_files(tmpdir)
                if not files:
                    return None, "Не получилось скачать (файл не найден после загрузки)."

                # заберём самый большой файл
                src = files[0]
                # копируем во внешний temp (чтобы не удалился после выхода из TemporaryDirectory)
                final_path = Path(tempfile.gettempdir()) / src.name
                final_path.write_bytes(src.read_bytes())
                return final_path, None

            except Exception as e:
                return None, str(e)

    return await asyncio.to_thread(_blocking)


# =========================
# Commands
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Бот работает 😎\n"
        "Команды: /rules /nick /rssstatus /ping\n"
        f"Авто: удаляет /qrand через {DELETE_VIDEO_LINK_AFTER_SECONDS}с, "
        "приветствует новых, бан-кнопка после выхода, RSS->канал, "
        "и пробует превращать ссылки Instagram/TikTok в видео.\n"
        "Если это фото — скажу что пока не умею 😅"
    )


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(RULES_TEXT)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"pong ✅\nchat_type={chat.type}\nchat_id={chat.id}"
    )


async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"RSS_URL={'✅ есть' if RSS_URL else '❌ нет'}\n"
        f"CHANNEL_ID={CHANNEL_ID}\n"
        f"poll={RSS_POLL_SECONDS}s\n"
        f"sent_ids={len(SENT_IDS)}"
    )


async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type != ChatType.SUPERGROUP:
        await msg.reply_text("❌ /nick работает только в супер-группе.")
        return

    if not context.args:
        await msg.reply_text("Напиши так: /nick ТВОЙ_НИК (до 16 символов)")
        return

    title = " ".join(context.args).strip()
    if len(title) > 16:
        await msg.reply_text("❌ Слишком длинно. Максимум 16 символов.")
        return
    if len(title) < 1:
        await msg.reply_text("❌ Ник пустой.")
        return

    try:
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
        if not getattr(me, "can_promote_members", False):
            await msg.reply_text("❌ У бота нет права 'Добавлять админов (can_promote_members)'.")
            return

        # Делаем админом БЕЗ прав (всё False)
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
            can_post_messages=False,
            can_edit_messages=False,
            can_manage_topics=False,
        )

        # Ставим кастомный титул
        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat.id,
            user_id=user.id,
            custom_title=title,
        )

        await msg.reply_text(f"✅ Ок, твой ник теперь: {title}")

    except TelegramError as e:
        await msg.reply_text(f"❌ Не получилось поставить ник.\nТех: {e}")


# =========================
# Welcome / left + ban button
# =========================
async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    for u in msg.new_chat_members:
        await msg.reply_text(f"Добро пожаловать, {u.mention_html()} 😼", parse_mode="HTML")


async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    u = msg.left_chat_member
    if not u:
        return

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔨 Забанить", callback_data=f"ban:{u.id}")]]
    )
    await msg.reply_text(f"👋 {u.full_name} вышел. Если надо:", reply_markup=kb)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    if data.startswith("ban:"):
        try:
            user_id = int(data.split(":", 1)[1])
            chat_id = q.message.chat_id
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await q.edit_message_text("✅ Забанен.")
        except Exception as e:
            await q.edit_message_text(f"❌ Не получилось забанить.\nТех: {e}")


# =========================
# /qrand delete after N seconds
# =========================
async def on_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    # удаляем команду через N сек
    if DELETE_VIDEO_LINK_AFTER_SECONDS > 0:
        context.job_queue.run_once(
            lambda c: asyncio.create_task(safe_delete(context, msg.chat_id, msg.message_id)),
            when=DELETE_VIDEO_LINK_AFTER_SECONDS,
        )


async def safe_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


# =========================
# Link -> video handler
# =========================
async def on_message_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    text = msg.text or msg.caption or ""
    urls = extract_urls(text)
    if not urls:
        return

    # берём первую ссылку (можно расширить до нескольких)
    url = urls[0].strip()
    final_url = await resolve_final_url(url)

    is_instagram = bool(INSTAGRAM_RE.search(final_url))
    is_tiktok = bool(TIKTOK_RE.search(final_url))

    if not (is_instagram or is_tiktok):
        return

    # 1) TikTok photo (обычно /photo/...)
    if is_tiktok and TIKTOK_PHOTO_RE.search(final_url):
        await msg.reply_text(PHOTO_SORRY_TEXT)
        return

    await msg.reply_text("⏳ Пытаюсь скачать...")

    file_path, err = await download_media_from_url(final_url)
    if err:
        # 2) Instagram пост /p/ иногда фото/карусель => yt-dlp пишет "There is no video in this post"
        if is_instagram and ("There is no video in this post" in err or "no video" in err.lower()):
            await msg.reply_text(PHOTO_SORRY_TEXT)
            return

        # 3) TikTok photo может быть не распознан по короткой ссылке — ловим по тексту ошибки
        if is_tiktok and ("Unsupported URL" in err and "tiktok.com" in err and "/photo/" in err):
            await msg.reply_text(PHOTO_SORRY_TEXT)
            return

        await msg.reply_text(
            "❌ Не получилось.\n"
            "Причина: Не получилось скачать. Возможно защита/блокировка или ссылка странная.\n\n"
            f"Тех.деталь: {err}"
        )
        return

    # отправляем видео
    try:
        size_mb = file_path.stat().st_size / (1024 * 1024)
        if size_mb > 49:
            await msg.reply_text(f"❌ Файл слишком большой для бота ({size_mb:.1f} MB).")
            try:
                file_path.unlink(missing_ok=True)
            except Exception:
                pass
            return

        await msg.reply_video(video=file_path)

        # удаляем исходное сообщение со ссылкой, если надо
        if DELETE_VIDEO_LINK_AFTER_SECONDS > 0:
            context.job_queue.run_once(
                lambda c: asyncio.create_task(safe_delete(context, msg.chat_id, msg.message_id)),
                when=DELETE_VIDEO_LINK_AFTER_SECONDS,
            )

    except Exception as e:
        await msg.reply_text(f"❌ Не смог отправить видео.\nТех: {e}")
    finally:
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass


# =========================
# RSS job
# =========================
async def rss_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not RSS_URL:
        return

    try:
        feed = feedparser.parse(RSS_URL)
        entries = getattr(feed, "entries", []) or []
        if not entries:
            return

        # идём от старых к новым
        for entry in reversed(entries[:30]):
            entry_id = (getattr(entry, "id", None) or getattr(entry, "guid", None) or getattr(entry, "link", None) or "").strip()
            if not entry_id:
                continue

            if entry_id in SENT_IDS:
                continue

            title = (getattr(entry, "title", "") or "").strip()
            link = (getattr(entry, "link", "") or "").strip()
            summary = (getattr(entry, "summary", "") or "").strip()

            text = f"**{title}**\n\n{summary}\n\n{link}".strip()

            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode="Markdown",
                reply_markup=make_links_keyboard(),
                disable_web_page_preview=False,
            )

            SENT_IDS.add(entry_id)
            save_sent_ids(SENT_IDS)

    except Exception as e:
        log.exception("RSS tick error: %s", e)


# =========================
# MAIN
# =========================
def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty")

    app = Application.builder().token(BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("rssstatus", cmd_rssstatus))
    app.add_handler(CommandHandler("nick", cmd_nick))

    # /qrand авто-удаление
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^/qrand\b"), on_qrand))

    # welcome/left + ban button
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))
    app.add_handler(CallbackQueryHandler(on_callback))

    # ссылки (insta/tiktok)
    app.add_handler(MessageHandler(filters.TEXT | filters.CaptionRegex(r"https?://"), on_message_links))

    # RSS job: убираем дубли, если вдруг добавлялся повторно
    jq = app.job_queue
    for j in jq.jobs():
        if j.name == "rss_tick":
            j.schedule_removal()

    if RSS_URL:
        jq.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10, name="rss_tick")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
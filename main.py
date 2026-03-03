import asyncio
import json
import os
import re
import time
import uuid
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
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()  # например: @yomabar
RSS_URL = os.getenv("RSS_URL", "").strip()
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "120"))

DELETE_QRAND_AFTER_SECONDS = int(os.getenv("DELETE_QRAND_AFTER_SECONDS", "30"))

INSTAGRAM_URL = os.getenv("INSTAGRAM_URL", "https://www.instagram.com/yomabar.lt").strip()
FACEBOOK_URL = os.getenv("FACEBOOK_URL", "https://www.facebook.com").strip()
SITE_URL = os.getenv("SITE_URL", "https://www.yomahayoma.show/").strip()

STATE_FILE = Path("state.json")

WELCOME_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)

# =========================
# HELPERS
# =========================
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            return {}
    return {}

def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")

def is_supergroup(update: Update) -> bool:
    c = update.effective_chat
    return bool(c and c.type in ("supergroup",))

def normalize_nick(text: str) -> str:
    text = text.strip()
    # ограничим до 16 символов (как ты просил)
    if len(text) > 16:
        text = text[:16]
    return text

async def is_user_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False

def extract_links_from_text(text: str) -> List[str]:
    # простая регулярка на ссылки
    url_re = re.compile(r"(https?://\S+)")
    return url_re.findall(text or "")

def is_supported_media_link(url: str) -> bool:
    u = url.lower()
    return (
        "instagram.com" in u
        or "tiktok.com" in u
        or "vm.tiktok.com" in u
    )

def build_links_footer() -> str:
    # в подписи к постам в канал
    return (
        f"\n\n"
        f"🔗 Instagram: {INSTAGRAM_URL}\n"
        f"🔗 Facebook: {FACEBOOK_URL}\n"
        f"🔗 Site: {SITE_URL}"
    )

@dataclass
class DownloadResult:
    ok: bool
    files: List[Path]
    reason: str = ""
    tech: str = ""

def ytdlp_download(url: str) -> DownloadResult:
    """
    Скачивает медиа без ffmpeg-склейки.
    Возвращает список файлов (видео/картинки).
    """
    tmp = Path("/tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    prefix = f"tg_{uuid.uuid4().hex}_"
    outtmpl = str(tmp / f"{prefix}%(id)s_%(autonumber)03d.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Важно: выбираем один "best" чтобы не требовать ffmpeg merge
        "format": "best[ext=mp4]/best",
        "merge_output_format": None,
        "retries": 2,
        "socket_timeout": 20,
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        # соберём все файлы по нашему prefix
        files = sorted(tmp.glob(f"{prefix}*.*"))

        # иногда yt-dlp не скачивает (например инста-пост без видео)
        if not files:
            # попробуем понять тип
            # если это инста-карусель фото, yt-dlp обычно тоже скачивает, но если закрыто — будет пусто
            return DownloadResult(False, [], "Не получилось скачать.", "Пустой результат (0 файлов). Возможно приват/защита/без медиа.")

        return DownloadResult(True, files)
    except Exception as e:
        return DownloadResult(False, [], "Не получилось скачать. Возможно защита/блокировка или ссылка странная.", f"{type(e).__name__}: {e}")

async def safe_delete_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Бот работает 😎\n"
        "Команды: /rules /nick /rssstatus\n"
        f"Авто: удаляет /qrand через {DELETE_QRAND_AFTER_SECONDS}с, приветствует новых, бан-кнопка после выхода, RSS->канал,\n"
        "и пытается превращать ссылки Instagram/TikTok в видео/фото.\n\n"
        "⚠️ Если бот не реагирует на ссылки — выключи Privacy:\n"
        "@BotFather → /setprivacy → Disable"
    )

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_TEXT)

async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    last = state.get("rss_last_id")
    await update.message.reply_text(
        "RSS статус:\n"
        f"- RSS_URL: {RSS_URL or 'не задан'}\n"
        f"- CHANNEL_USERNAME: {CHANNEL_USERNAME or 'не задан'}\n"
        f"- POLL: {RSS_POLL_SECONDS}s\n"
        f"- last_id: {last}"
    )

async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_supergroup(update):
        await update.message.reply_text("❌ /nick работает только в супер-группе.")
        return

    if not update.message:
        return

    nick = normalize_nick(" ".join(context.args or []))
    if not nick:
        await update.message.reply_text("Используй так: /nick невеста")
        return

    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    # нужно чтобы бот был админом с правом назначать custom title
    try:
        # В PTB: promote_chat_member поддерживает custom_title (Telegram API)
        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            # не меняем реальные права, только титул
            can_change_info=False,
            can_post_messages=False,
            can_edit_messages=False,
            can_delete_messages=False,
            can_invite_users=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_promote_members=False,
            can_manage_video_chats=False,
            can_manage_chat=False,
            is_anonymous=False,
            can_manage_topics=False,
            custom_title=nick,
        )
        await update.message.reply_text(f"✅ Ник установлен: {nick}")
    except BadRequest as e:
        await update.message.reply_text(f"❌ Не смог поставить ник.\nПричина: {e}")
    except Forbidden:
        await update.message.reply_text("❌ У меня нет прав. Сделай меня админом в чате.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {type(e).__name__}: {e}")

# =========================
# EVENTS: JOIN / LEAVE
# =========================
async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    # привет + правила
    await update.message.reply_text(WELCOME_TEXT)

async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.left_chat_member:
        return

    left = update.message.left_chat_member
    chat = update.effective_chat
    if not chat:
        return

    text = f"👋 {left.full_name} вышел(ла) из чата.\nЕсли это спамер — можно забанить:"
    kb = InlineKeyboardMarkup.from_button(
        InlineKeyboardButton("🚫 BAN", callback_data=f"ban:{left.id}")
    )
    await update.message.reply_text(text, reply_markup=kb)

async def cb_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    if not data.startswith("ban:"):
        return

    try:
        target_id = int(data.split(":", 1)[1])
    except Exception:
        return

    # банить может только админ
    who = q.from_user.id if q.from_user else 0
    fake_update = Update(update.update_id, callback_query=q)
    if not await is_user_admin(fake_update, context, who):
        await q.edit_message_text("❌ Банить могут только админы.")
        return

    chat = q.message.chat if q.message else None
    if not chat:
        return

    try:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=target_id)
        await q.edit_message_text("✅ Забанен.")
    except Forbidden:
        await q.edit_message_text("❌ У меня нет прав банить. Дай права админа.")
    except BadRequest as e:
        await q.edit_message_text(f"❌ Не смог забанить: {e}")
    except Exception as e:
        await q.edit_message_text(f"❌ Ошибка: {type(e).__name__}: {e}")

# =========================
# /qrand AUTO DELETE
# =========================
async def on_message_maybe_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    txt = update.message.text.strip()

    # /qrand или /qrand@BotName
    if not (txt.startswith("/qrand") or txt.startswith("/qrand@")):
        return

    chat_id = update.effective_chat.id
    msg_id = update.message.message_id

    # запланировать удаление
    context.job_queue.run_once(
        lambda ctx: asyncio.create_task(safe_delete_message(ctx, chat_id, msg_id)),
        when=DELETE_QRAND_AFTER_SECONDS,
    )

# =========================
# LINKS -> DOWNLOAD MEDIA
# =========================
async def on_message_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text_parts = []
    if update.message.text:
        text_parts.append(update.message.text)
    if update.message.caption:
        text_parts.append(update.message.caption)

    text = "\n".join(text_parts)
    urls = extract_links_from_text(text)
    urls = [u for u in urls if is_supported_media_link(u)]

    if not urls:
        return

    # чтобы не спамил на пачку одинаковых ссылок — обработаем первую
    url = urls[0]

    # маленькая реакция чтобы было видно что бот "увидел"
    try:
        await update.message.reply_text("⏳ Пробую скачать медиа…")
    except Exception:
        pass

    loop = asyncio.get_running_loop()
    res: DownloadResult = await loop.run_in_executor(None, ytdlp_download, url)

    if not res.ok:
        await update.message.reply_text(
            f"❌ Не получилось.\n"
            f"Причина: {res.reason}\n\n"
            f"Тех.деталь: {res.tech}"
        )
        return

    # отправляем как медиа-группу (до 10 элементов)
    files = res.files[:10]

    media = []
    for p in files:
        ext = p.suffix.lower()
        # грубо определим картинка или видео
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            media.append(InputMediaPhoto(media=p.read_bytes()))
        elif ext in (".mp4", ".mov", ".m4v", ".webm"):
            media.append(InputMediaVideo(media=p.read_bytes()))
        else:
            # неизвестное — отправим документом позже
            pass

    sent_any = False
    if media:
        try:
            await update.message.reply_media_group(media=media)
            sent_any = True
        except Exception as e:
            # если медиа-группа не прошла — fallback на поштучно
            await update.message.reply_text(f"⚠️ Медиа-группа не отправилась, отправляю по одному.\nТех: {type(e).__name__}: {e}")

    if not sent_any:
        # fallback: по одному файлу
        for p in files:
            ext = p.suffix.lower()
            b = p.read_bytes()
            try:
                if ext in (".jpg", ".jpeg", ".png", ".webp"):
                    await update.message.reply_photo(photo=b)
                elif ext in (".mp4", ".mov", ".m4v", ".webm"):
                    await update.message.reply_video(video=b)
                else:
                    await update.message.reply_document(document=b, filename=p.name)
            except Exception:
                # вообще крайний случай
                await update.message.reply_text(f"⚠️ Не смог отправить файл: {p.name}")

    # чистим tmp
    for p in res.files:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass

# =========================
# RSS -> CHANNEL
# =========================
async def rss_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not RSS_URL or not CHANNEL_USERNAME:
        return

    state = load_state()
    last_id = state.get("rss_last_id")

    feed = feedparser.parse(RSS_URL)
    if not getattr(feed, "entries", None):
        return

    # берём самые новые сверху, но постим только то, чего ещё не было
    new_entries = []
    for e in feed.entries[:20]:
        eid = getattr(e, "id", None) or getattr(e, "guid", None) or getattr(e, "link", None) or getattr(e, "title", "")
        if not eid:
            continue
        if eid == last_id:
            break
        new_entries.append((eid, e))

    if not new_entries:
        return

    # постим в обратном порядке (старые -> новые)
    new_entries.reverse()

    for eid, e in new_entries:
        title = getattr(e, "title", "").strip()
        link = getattr(e, "link", "").strip()

        # описание может быть грязное (html) — оставим только коротко
        summary = getattr(e, "summary", "")
        summary = re.sub("<.*?>", "", summary or "").strip()
        if len(summary) > 700:
            summary = summary[:700] + "…"

        text = f"🆕 {title}\n\n{summary}\n\n{link}{build_links_footer()}".strip()

        try:
            await context.bot.send_message(
                chat_id=CHANNEL_USERNAME,
                text=text,
                disable_web_page_preview=False,
            )
            state["rss_last_id"] = eid
            save_state(state)
        except Exception as ex:
            # если упало — не обновляем last_id, чтобы не потерять
            print("RSS send error:", ex)
            return

# =========================
# MAIN
# =========================
def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty")

    app = Application.builder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("rssstatus", cmd_rssstatus))
    app.add_handler(CommandHandler("nick", cmd_nick))

    # join/leave
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))
    app.add_handler(CallbackQueryHandler(cb_ban))

    # qrand delete
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_maybe_qrand))
    app.add_handler(MessageHandler(filters.COMMAND, on_message_maybe_qrand))  # если /qrand как команда

    # links downloader (ВАЖНО: только если bot видит обычные сообщения — выключи privacy!)
    app.add_handler(MessageHandler(filters.TEXT | filters.Caption(), on_message_links))

    # RSS job
    if RSS_URL and CHANNEL_USERNAME:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)

    return app

def main() -> None:
    app = build_app()
    try:
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print("FATAL:", e)
        raise

if __name__ == "__main__":
    main()
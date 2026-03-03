import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple

import feedparser
import httpx
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ChatMemberHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("yomabar-bot")

# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@yomabar").strip()
RSS_URL = os.getenv("RSS_URL", "").strip()
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "120"))
DELETE_QRAND_AFTER_SECONDS = int(os.getenv("DELETE_QRAND_AFTER_SECONDS", "30"))

INSTAGRAM_URL = os.getenv("INSTAGRAM_URL", "https://www.instagram.com/yomabar.lt").strip()
FACEBOOK_URL = os.getenv("FACEBOOK_URL", "https://www.facebook.com").strip()
SITE_URL = os.getenv("SITE_URL", "https://www.yomahayoma.show").strip()

RULES_TEXT = os.getenv(
    "RULES_TEXT",
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
).strip()

# RSS state file (Railway disk может сбрасываться при redeploy, это ок)
STATE_PATH = Path(os.getenv("STATE_PATH", "/tmp/yomabar_state.json"))

# =========================
# Regex (links)
# =========================
IG_RE = re.compile(r"(https?://(?:www\.)?instagram\.com/[^\s]+)", re.IGNORECASE)
TT_RE = re.compile(r"(https?://(?:www\.)?(?:vm\.)?tiktok\.com/[^\s]+)", re.IGNORECASE)

QRAND_RE = re.compile(r"(?<!\S)/qrand(?!\S)", re.IGNORECASE)

# =========================
# Helpers
# =========================

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text("utf-8"))
        except Exception:
            return {}
    return {}

def save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    except Exception as e:
        log.warning("Failed to save state: %s", e)

def footer_links() -> str:
    return (
        f"\n\n🔗 <a href=\"{INSTAGRAM_URL}\">Instagram</a>"
        f" | <a href=\"{FACEBOOK_URL}\">Facebook</a>"
        f" | <a href=\"{SITE_URL}\">Site</a>"
    )

def is_supergroup(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "supergroup")

async def safe_delete_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

# =========================
# Commands
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Бот работает 😎\n"
        "Команды: /rules /nick /rssstatus /ping\n"
        f"Авто: удаляет /qrand через {DELETE_QRAND_AFTER_SECONDS}с, приветствует новых, "
        "бан-кнопка после выхода, RSS->канал, и пытается превращать ссылки Instagram/TikTok в видео/фото."
    )
    await update.message.reply_text(text)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(f"pong ✅\nchat_type={chat.type}\nchat_id={chat.id}")

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(RULES_TEXT, disable_web_page_preview=True)

async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = load_state()
    last = st.get("rss_last_id")
    last_ts = st.get("rss_last_ts")
    await update.message.reply_text(
        "RSS статус:\n"
        f"- URL: {RSS_URL or 'не задан'}\n"
        f"- interval: {RSS_POLL_SECONDS}s\n"
        f"- last_id: {last}\n"
        f"- last_ts: {last_ts}"
    )

# =========================
# /nick (custom admin title)
# =========================

def normalize_title(s: str) -> str:
    s = s.strip()
    # Telegram custom title limit ~16 chars (ты так и хочешь)
    return s[:16]

async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if not is_supergroup(update):
        await update.message.reply_text("❌ /nick работает только в супер-группе.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /nick <ник до 16 символов>")
        return

    title = normalize_title(" ".join(context.args))
    if not title:
        await update.message.reply_text("❌ Пустой ник. Пример: /nick Невеста")
        return

    chat_id = update.effective_chat.id
    user = update.effective_user

    # Проверим, что бот админ
    me = await context.bot.get_chat_member(chat_id, context.bot.id)
    if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await update.message.reply_text("❌ Я не админ в этой группе. Дай мне админку.")
        return

    # Сначала промоутим юзера в админа БЕЗ прав, потом ставим custom title
    try:
        await context.bot.promote_chat_member(
            chat_id=chat_id,
            user_id=user.id,
            can_change_info=False,
            can_post_messages=False,
            can_edit_messages=False,
            can_delete_messages=False,
            can_invite_users=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_promote_members=False,
            can_manage_chat=False,
            can_manage_video_chats=False,
            can_manage_topics=False,
            can_post_stories=False,
            can_edit_stories=False,
            can_delete_stories=False,
            is_anonymous=False,
        )
    except BadRequest as e:
        # Если уже админ — ок
        if "user is not a member" in str(e).lower():
            await update.message.reply_text("❌ Ты должен быть участником группы, чтобы поставить ник.")
            return
        # прочее — не критично, продолжим
        log.info("promote_chat_member warning: %s", e)

    try:
        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat_id,
            user_id=user.id,
            custom_title=title,
        )
        await update.message.reply_text(f"✅ Ник установлен: {title}")
    except BadRequest as e:
        await update.message.reply_text(
            "❌ Не получилось поставить ник.\n"
            "Проверь:\n"
            "1) Группа именно СУПЕРГРУППА\n"
            "2) Я админ\n"
            "3) У меня есть право 'Добавлять админов' (или хотя бы менять титлы)\n\n"
            f"Тех.деталь: {e}"
        )

# =========================
# Auto delete /qrand
# =========================

async def on_any_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    # 1) удалить /qrand через N секунд
    if QRAND_RE.search(msg.text):
        context.job_queue.run_once(
            lambda c: safe_delete_message(c, msg.chat_id, msg.message_id),
            when=DELETE_QRAND_AFTER_SECONDS,
            name=f"del_qrand_{msg.chat_id}_{msg.message_id}",
        )
        return

    # 2) ссылки IG/TT -> попытка скачать
    urls = extract_urls_from_text(msg.text)
    if not urls:
        return

    # чтобы не спамить, реагируем только если есть IG/TT
    ig_urls = [u for u in urls if "instagram.com" in u.lower()]
    tt_urls = [u for u in urls if "tiktok.com" in u.lower() or "vm.tiktok.com" in u.lower()]
    targets = ig_urls + tt_urls
    if not targets:
        return

    for u in targets[:3]:  # ограничим 3 ссылки за сообщение
        await try_download_and_send(update, context, u)

def extract_urls_from_text(text: str) -> List[str]:
    found = []
    found += IG_RE.findall(text)
    found += TT_RE.findall(text)
    # уникальные, сохраняя порядок
    seen = set()
    out = []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

# =========================
# Welcome + ban button on leave
# =========================

BAN_CB_PREFIX = "ban:"

async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles join/leave events.
    """
    if not update.chat_member:
        return

    chat = update.effective_chat
    new = update.chat_member.new_chat_member
    old = update.chat_member.old_chat_member
    user = update.chat_member.from_user

    # joined
    if old.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and new.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=f"{RULES_TEXT}\n\n👋 Привет, {user.mention_html()}!",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        except Exception:
            pass
        return

    # left / kicked
    if new.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and old.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        # Кнопка бан
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 Забанить", callback_data=f"{BAN_CB_PREFIX}{user.id}")]
        ])
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=f"👋 {user.mention_html()} вышел(а). Если это спамер — можно забанить:",
                parse_mode=ParseMode.HTML,
                reply_markup=kb
            )
        except Exception:
            pass

async def on_ban_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    if not data.startswith(BAN_CB_PREFIX):
        return
    target_id = int(data.split(":", 1)[1])
    chat_id = q.message.chat_id

    # Проверим, что нажимающий — админ
    actor = q.from_user
    try:
        actor_member = await context.bot.get_chat_member(chat_id, actor.id)
        if actor_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await q.edit_message_text("❌ Бан доступен только админам.")
            return
    except Exception:
        await q.edit_message_text("❌ Не смог проверить права.")
        return

    # Баним
    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=target_id)
        await q.edit_message_text("✅ Забанил.")
    except BadRequest as e:
        await q.edit_message_text(f"❌ Не получилось забанить.\nТех.деталь: {e}")
    except Forbidden:
        await q.edit_message_text("❌ У меня нет прав банить. Дай права админа.")
    except Exception as e:
        await q.edit_message_text(f"❌ Ошибка: {e}")

# =========================
# RSS -> Channel
# =========================

async def rss_tick(context: ContextTypes.DEFAULT_TYPE):
    if not RSS_URL:
        return

    st = load_state()
    last_id = st.get("rss_last_id")

    try:
        feed = feedparser.parse(RSS_URL)
    except Exception as e:
        log.warning("RSS parse error: %s", e)
        return

    entries = feed.entries or []
    if not entries:
        return

    # Берем от старых к новым
    new_entries = []
    for e in reversed(entries[:20]):
        eid = getattr(e, "id", None) or getattr(e, "link", None) or getattr(e, "title", None)
        if not eid:
            continue
        if last_id and eid == last_id:
            new_entries = []
            continue
        new_entries.append((eid, e))

    if not new_entries:
        return

    for eid, e in new_entries[-5:]:
        title = getattr(e, "title", "Пост")
        link = getattr(e, "link", "")
        summary = getattr(e, "summary", "")
        # коротко
        summary_clean = re.sub(r"\s+", " ", summary).strip()
        if len(summary_clean) > 500:
            summary_clean = summary_clean[:500] + "…"

        text = f"🆕 <b>{escape_html(title)}</b>\n"
        if summary_clean:
            text += f"\n{escape_html(summary_clean)}\n"
        if link:
            text += f"\n<a href=\"{link}\">Открыть</a>"
        text += footer_links()

        try:
            await context.bot.send_message(
                chat_id=CHANNEL_USERNAME,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            last_id = eid
            st["rss_last_id"] = last_id
            st["rss_last_ts"] = int(time.time())
            save_state(st)
            await asyncio.sleep(0.3)
        except Exception as ex:
            log.warning("RSS send error: %s", ex)
            break

def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )

# =========================
# IG/TT downloader via yt-dlp
# =========================

async def try_download_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    chat_id = update.effective_chat.id
    reply_to = update.message.message_id if update.message else None

    status_msg: Optional[Message] = None
    try:
        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ Пытаюсь скачать…",
            reply_to_message_id=reply_to,
            disable_web_page_preview=True,
        )

        files, info = await ytdlp_download(url)
        if not files:
            raise RuntimeError("Ничего не скачалось.")

        await send_downloaded_files(context, chat_id, reply_to, files, info)

        # уберем статус
        if status_msg:
            await safe_delete_message(context, chat_id, status_msg.message_id)

    except Exception as e:
        tech = str(e)
        text = (
            "❌ Не получилось.\n"
            "Причина: Не получилось скачать. Возможно защита/блокировка или ссылка странная.\n\n"
            f"Тех.деталь: {tech}"
        )
        if status_msg:
            try:
                await status_msg.edit_text(text, disable_web_page_preview=True)
            except Exception:
                await context.bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=reply_to)

async def ytdlp_download(url: str) -> Tuple[List[Path], dict]:
    """
    Downloads media (video/photo) into temp dir.
    Returns list of downloaded file paths + meta.
    """
    # импортируем здесь, чтобы бот не падал если pip не поставил пакет
    import yt_dlp

    tmpdir = Path(tempfile.mkdtemp(prefix="yomabar_dl_"))
    outtmpl = str(tmpdir / "%(id)s_%(title).80s.%(ext)s")

    # ВАЖНО:
    # - format "best" чтобы не требовать ffmpeg-merge
    # - для видео часто отдаёт mp4 уже со звуком
    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 20,
        "concurrent_fragment_downloads": 1,
        "overwrites": True,
    }

    info = {}
    try:
        def _run():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                nonlocal info
                info = ydl.extract_info(url, download=True)
        await asyncio.to_thread(_run)
    except Exception as e:
        # если упало — чистим папку
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    # соберем файлы
    files = sorted([p for p in tmpdir.glob("*") if p.is_file()], key=lambda p: p.stat().st_size, reverse=True)

    # иногда yt-dlp создаёт json/thumbnail — отфильтруем
    media = []
    for p in files:
        ext = p.suffix.lower()
        if ext in [".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".mkv", ".webm", ".gif"]:
            media.append(p)

    # ограничим количество
    return media[:10], info

async def send_downloaded_files(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    reply_to: Optional[int],
    files: List[Path],
    info: dict,
):
    caption = build_caption(info)
    # если много — отправим медиагруппой (первые 10)
    media_group = []
    for i, p in enumerate(files):
        ext = p.suffix.lower()
        if ext in [".jpg", ".jpeg", ".png", ".webp"]:
            media_group.append(InputMediaPhoto(media=p.open("rb"), caption=(caption if i == 0 else None), parse_mode=ParseMode.HTML))
        else:
            media_group.append(InputMediaVideo(media=p.open("rb"), caption=(caption if i == 0 else None), parse_mode=ParseMode.HTML))

    if len(media_group) == 1:
        m = media_group[0]
        if isinstance(m, InputMediaPhoto):
            await context.bot.send_photo(chat_id=chat_id, photo=m.media, caption=m.caption, parse_mode=ParseMode.HTML, reply_to_message_id=reply_to)
        else:
            await context.bot.send_video(chat_id=chat_id, video=m.media, caption=m.caption, parse_mode=ParseMode.HTML, reply_to_message_id=reply_to)
    else:
        await context.bot.send_media_group(chat_id=chat_id, media=media_group, reply_to_message_id=reply_to)

    # Закрыть файлы и удалить папки
    for m in media_group:
        try:
            m.media.close()
        except Exception:
            pass
    # удалить tmpdir
    try:
        tmpdir = files[0].parent
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass

def build_caption(info: dict) -> str:
    # короткая подпись + ссылки
    title = info.get("title") if isinstance(info, dict) else None
    webpage_url = info.get("webpage_url") if isinstance(info, dict) else None
    parts = []
    if title:
        parts.append(f"<b>{escape_html(str(title)[:120])}</b>")
    if webpage_url:
        parts.append(f"<a href=\"{webpage_url}\">Источник</a>")
    parts.append(f"<a href=\"{INSTAGRAM_URL}\">Instagram</a> | <a href=\"{FACEBOOK_URL}\">Facebook</a> | <a href=\"{SITE_URL}\">Site</a>")
    return "\n".join(parts)

# =========================
# Main
# =========================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set Railway variable BOT_TOKEN.")

    app = Application.builder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("rssstatus", cmd_rssstatus))
    app.add_handler(CommandHandler("nick", cmd_nick))

    # callbacks
    app.add_handler(CallbackQueryHandler(on_ban_button, pattern=f"^{BAN_CB_PREFIX}\\d+$"))

    # member events (join/leave)
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # text watcher (qrand + links)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_any_text))

    # RSS job
    if RSS_URL:
        if not app.job_queue:
            raise RuntimeError("JobQueue not initialized. Check requirements: python-telegram-bot[job-queue]==21.6")
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)

    log.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import feedparser
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# RSS -> channel
RSS_URL = os.getenv("RSS_URL", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()  # e.g. @yomabar or -100...
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "120"))

# moderation
DELETE_QRAND_AFTER_SECONDS = int(os.getenv("DELETE_QRAND_AFTER_SECONDS", "30"))

# downloader
ENABLE_LINK_DOWNLOADER = os.getenv("ENABLE_LINK_DOWNLOADER", "1").strip() in ("1", "true", "True", "yes", "YES")
MAX_MEDIA_GROUP = 10  # Telegram media group limit

# rules text
WELCOME_RULES_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)

STATE_FILE = Path("state.json")

# qrand detection
QRAND_RE = re.compile(r"^/qrand(@\w+)?(\s|$)", re.IGNORECASE)

# link detection (simple)
LINK_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)
TT_RE = re.compile(r"(https?://(?:www\.)?tiktok\.com/\S+|https?://vm\.tiktok\.com/\S+)", re.IGNORECASE)
IG_RE = re.compile(r"(https?://(?:www\.)?instagram\.com/\S+)", re.IGNORECASE)

# =========================
# STATE
# =========================
@dataclass
class State:
    rss_last_id: Optional[str] = None
    rss_seen: Optional[List[str]] = None  # small rolling memory

    def to_dict(self) -> Dict[str, Any]:
        return {"rss_last_id": self.rss_last_id, "rss_seen": self.rss_seen or []}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "State":
        return State(rss_last_id=d.get("rss_last_id"), rss_seen=list(d.get("rss_seen") or []))


def load_state() -> State:
    if STATE_FILE.exists():
        try:
            return State.from_dict(json.loads(STATE_FILE.read_text("utf-8")))
        except Exception:
            pass
    return State(rss_last_id=None, rss_seen=[])


def save_state(state: State) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        # don't crash bot because of state write
        pass


STATE = load_state()

# =========================
# HELPERS
# =========================
def is_supergroup(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in ("supergroup",))

def is_groupish(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in ("group", "supergroup"))

def safe_text_from_update(update: Update) -> str:
    m = update.effective_message
    if not m:
        return ""
    if m.text:
        return m.text
    if m.caption:
        return m.caption
    return ""

def extract_links(text: str) -> List[str]:
    return LINK_RE.findall(text or "")

def has_tt_or_ig_link(text: str) -> bool:
    return bool(TT_RE.search(text or "") or IG_RE.search(text or ""))

def clamp_nick(s: str) -> str:
    s = s.strip()
    # Telegram admin title max 16 chars
    return s[:16]

async def bot_is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    me = await context.bot.get_me()
    cm = await context.bot.get_chat_member(chat_id, me.id)
    return cm.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

# =========================
# COMMANDS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Бот работает 😎\n"
        "Команды: /rules /nick /rssstatus /ping\n"
        f"Авто: удаляет /qrand через {DELETE_QRAND_AFTER_SECONDS}с, "
        "приветствует новых, бан-кнопка после выхода, RSS->канал, "
        "и пробует превращать ссылки Instagram/TikTok в видео/фото."
    )
    await update.effective_message.reply_text(text)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"pong ✅\nchat_type={chat.type}\nchat_id={chat.id}"
    )

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME_RULES_TEXT)

async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"RSS_URL: {RSS_URL or '—'}\n"
        f"CHANNEL_USERNAME: {CHANNEL_USERNAME or '—'}\n"
        f"RSS_POLL_SECONDS: {RSS_POLL_SECONDS}\n"
        f"rss_last_id: {STATE.rss_last_id or '—'}\n"
        f"rss_seen_count: {len(STATE.rss_seen or [])}"
    )

async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_supergroup(update):
        await update.effective_message.reply_text("❌ /nick работает только в супер-группе.")
        return

    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    raw = " ".join(context.args or []).strip()
    if not raw:
        await update.effective_message.reply_text("Пример: /nick невеста")
        return

    title = clamp_nick(raw)
    if len(title) == 0:
        await update.effective_message.reply_text("Ник не должен быть пустым.")
        return

    # bot must be admin
    if not await bot_is_admin(context, chat.id):
        await update.effective_message.reply_text("❌ Я должен быть админом, чтобы ставить /nick.")
        return

    try:
        # Promote user to admin with minimal/no rights (Telegram may require at least one right in some cases).
        # We'll give the smallest harmless right: can_manage_chat=False etc, but Telegram can still accept.
        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            is_anonymous=False,
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
        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat.id,
            user_id=user.id,
            custom_title=title,
        )
        await update.effective_message.reply_text(f"✅ Ник поставлен: {title}")
    except BadRequest as e:
        await update.effective_message.reply_text(f"❌ Не получилось поставить ник.\nПричина: {e.message}")
    except Forbidden:
        await update.effective_message.reply_text("❌ Мне не хватает прав (проверь, что я админ и могу назначать админов).")

# =========================
# WELCOME + LEFT/BAN BUTTON
# =========================
async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    cmu = update.chat_member
    if not chat or not cmu:
        return

    new = cmu.new_chat_member
    old = cmu.old_chat_member
    user = cmu.from_user

    # joined
    if old.status in ("left", "kicked") and new.status in ("member", "administrator"):
        try:
            await context.bot.send_message(chat.id, WELCOME_RULES_TEXT)
        except Exception:
            pass
        return

    # left
    if old.status in ("member", "administrator") and new.status == "left":
        # show ban button (works only if bot admin)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚫 Забанить", callback_data=f"ban:{user.id}")]]
        )
        try:
            await context.bot.send_message(
                chat.id,
                f"👋 {user.full_name} вышел(ла).",
                reply_markup=kb,
            )
        except Exception:
            pass

async def on_ban_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.message:
        return
    await q.answer()

    chat_id = q.message.chat_id
    data = q.data or ""
    if not data.startswith("ban:"):
        return

    try:
        target_id = int(data.split(":", 1)[1])
    except ValueError:
        await q.edit_message_text("❌ Ошибка данных.")
        return

    # only admins can use
    actor = q.from_user
    try:
        actor_member = await context.bot.get_chat_member(chat_id, actor.id)
        if actor_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await q.answer("Только админы.", show_alert=True)
            return
    except Exception:
        await q.answer("Не могу проверить права.", show_alert=True)
        return

    # bot must be admin
    if not await bot_is_admin(context, chat_id):
        await q.edit_message_text("❌ Я не админ — не могу банить.")
        return

    try:
        await context.bot.ban_chat_member(chat_id, target_id)
        await q.edit_message_text("✅ Забанен.")
    except BadRequest as e:
        await q.edit_message_text(f"❌ Не получилось забанить: {e.message}")
    except Forbidden:
        await q.edit_message_text("❌ Мне не хватает прав, чтобы банить.")

# =========================
# DELETE /qrand AFTER N SEC
# =========================
async def delete_message_later(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if not job:
        return
    chat_id = job.data["chat_id"]
    msg_id = job.data["message_id"]
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        pass

async def on_message_delete_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_groupish(update):
        return
    m = update.effective_message
    if not m:
        return

    txt = (m.text or "").strip()
    # also allow /qrand in captions? (people usually send as text)
    cap = (m.caption or "").strip()

    if QRAND_RE.search(txt) or QRAND_RE.search(cap):
        # bot must be admin to delete
        if not await bot_is_admin(context, m.chat_id):
            return
        context.job_queue.run_once(
            delete_message_later,
            when=DELETE_QRAND_AFTER_SECONDS,
            data={"chat_id": m.chat_id, "message_id": m.message_id},
            name=f"del:{m.chat_id}:{m.message_id}",
        )

# =========================
# RSS -> CHANNEL
# =========================
def rss_entry_id(entry: Any) -> str:
    # stable id for dedupe
    return (
        getattr(entry, "id", None)
        or getattr(entry, "guid", None)
        or getattr(entry, "link", None)
        or (getattr(entry, "title", "") + "|" + getattr(entry, "published", ""))
    )

async def rss_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not RSS_URL or not CHANNEL_USERNAME:
        return

    feed = None
    try:
        feed = feedparser.parse(RSS_URL)
    except Exception:
        return

    if not feed or not feed.entries:
        return

    # newest first?
    entries = list(feed.entries)
    # feedparser often gives newest first; to be safe sort by published_parsed if available
    def ts(e: Any) -> float:
        pp = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
        if pp:
            return time.mktime(pp)
        return 0.0

    entries.sort(key=ts)

    sent_any = False

    # rolling set
    seen = set(STATE.rss_seen or [])
    # only send entries not seen
    for e in entries[-20:]:  # limit scan
        eid = rss_entry_id(e)
        if eid in seen:
            continue

        title = getattr(e, "title", "").strip()
        link = getattr(e, "link", "").strip()
        summary = (getattr(e, "summary", "") or "").strip()

        # small formatting
        msg = f"🆕 <b>{title}</b>\n"
        if link:
            msg += f"\n{link}\n"
        if summary:
            # keep it short
            s = re.sub(r"<[^>]+>", "", summary)
            s = s.strip()
            if len(s) > 800:
                s = s[:800] + "…"
            msg += f"\n{s}"

        try:
            await context.bot.send_message(
                chat_id=CHANNEL_USERNAME,
                text=msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            sent_any = True
            seen.add(eid)
            STATE.rss_last_id = eid
        except Exception:
            # don't mark as seen if failed
            continue

    # keep only last 200 ids
    if sent_any:
        STATE.rss_seen = list(seen)[-200:]
        save_state(STATE)

# =========================
# LINK DOWNLOADER (TikTok / Instagram)
# =========================
def run_ytdlp_download(url: str) -> Tuple[List[Path], Optional[str]]:
    """
    Downloads media from url into temp dir.
    Returns (files, error_message).
    Uses format that doesn't require ffmpeg merge.
    """
    tmp = Path(tempfile.mkdtemp(prefix="dl_"))
    outtpl = str(tmp / "%(title).80s_%(id)s.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--restrict-filenames",
        "-f",
        "best[ext=mp4]/best",   # no merging
        "-o",
        outtpl,
        url,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        shutil.rmtree(tmp, ignore_errors=True)
        return ([], "yt-dlp не установлен.")
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, ignore_errors=True)
        return ([], "Таймаут скачивания (слишком долго).")

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        shutil.rmtree(tmp, ignore_errors=True)
        # keep short
        if len(err) > 800:
            err = err[-800:]
        return ([], err or "Неизвестная ошибка yt-dlp.")

    files = sorted([p for p in tmp.iterdir() if p.is_file()])
    if not files:
        shutil.rmtree(tmp, ignore_errors=True)
        return ([], "Файлы не скачались (пусто).")
    return (files, None)

async def send_downloaded_files(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    files: List[Path],
    reply_to_message_id: Optional[int] = None,
) -> None:
    chat_id = update.effective_chat.id

    # classify
    photos = [p for p in files if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
    videos = [p for p in files if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")]

    # send videos (single best)
    # if multiple videos, send first one
    if videos:
        v = videos[0]
        with v.open("rb") as f:
            await context.bot.send_video(
                chat_id=chat_id,
                video=f,
                reply_to_message_id=reply_to_message_id,
            )

    # send photos as album
    if photos:
        photos = photos[:MAX_MEDIA_GROUP]
        media = []
        for p in photos:
            media.append(InputMediaPhoto(media=p.read_bytes()))
        await context.bot.send_media_group(
            chat_id=chat_id,
            media=media,
            reply_to_message_id=reply_to_message_id,
        )

async def on_message_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ENABLE_LINK_DOWNLOADER:
        return

    msg = update.effective_message
    if not msg:
        return

    text = safe_text_from_update(update)
    if not text:
        return

    if not has_tt_or_ig_link(text):
        return

    # extract first matching link
    links = extract_links(text)
    url = None
    for l in links:
        if TT_RE.search(l) or IG_RE.search(l):
            url = l
            break
    if not url:
        return

    # tell user we started
    try:
        await msg.reply_text("⏳ Пытаюсь скачать…")
    except Exception:
        pass

    files, err = await asyncio.to_thread(run_ytdlp_download, url)
    if err:
        await msg.reply_text(
            "❌ Не получилось.\n"
            "Причина: Не получилось скачать. Возможно защита/блокировка или ссылка странная.\n\n"
            f"Тех.деталь: {err}"
        )
        return

    # send media
    try:
        await send_downloaded_files(update, context, files, reply_to_message_id=msg.message_id)
    except BadRequest as e:
        await msg.reply_text(f"❌ Не получилось отправить файл(ы): {e.message}")
    except Exception as e:
        await msg.reply_text(f"❌ Ошибка отправки: {type(e).__name__}: {e}")
    finally:
        # cleanup
        if files:
            try:
                shutil.rmtree(files[0].parent, ignore_errors=True)
            except Exception:
                pass

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
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("nick", cmd_nick))

    # chat member updates
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(on_ban_button, pattern=r"^ban:\d+$"))

    # IMPORTANT ORDER:
    # group=0: link handler first (so it doesn't get swallowed by generic text handlers)
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, on_message_links), group=0)

    # group=1: deletion of /qrand
    app.add_handler(MessageHandler(filters.ALL, on_message_delete_qrand), group=1)

    # RSS job
    if RSS_URL and CHANNEL_USERNAME:
        # initial delay небольшая, чтобы app поднялся
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)

    return app

def main() -> None:
    app = build_app()
    # drop_pending_updates helps after redeploy
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
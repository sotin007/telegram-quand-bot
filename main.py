import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import tempfile
import time
import html as pyhtml
from collections import defaultdict
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
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CallbackQueryHandler,
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
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
).strip()

DELETE_QRAND_AFTER_SECONDS = int(os.getenv("DELETE_QRAND_AFTER_SECONDS", "30"))

# RSS -> CHANNEL
RSS_URL = os.getenv("RSS_URL", "").strip()
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "180"))
RSS_CHANNEL_ID = os.getenv("RSS_CHANNEL_ID", "").strip()  # numeric, e.g. -100...

# Buttons under RSS post
BTN_INSTAGRAM = os.getenv("BTN_INSTAGRAM", "https://www.instagram.com/yomabar.lt").strip()
BTN_FACEBOOK = os.getenv("BTN_FACEBOOK", "https://www.facebook.com/share/1P3dFJ5f5Y/?mibextid=wwXIfr").strip()
BTN_SITE = os.getenv("BTN_SITE", "https://www.yomahayoma.show/").strip()

PHOTO_SORRY_TEXT = "Сори брат да? Я ещё не умею качать фотки, давай как то без меня, всё пока 👋"

# Mini-AI settings
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@").lower()
AI_EVERY_MESSAGES = int(os.getenv("AI_EVERY_MESSAGES", "10"))
AI_COOLDOWN_SECONDS = int(os.getenv("AI_COOLDOWN_SECONDS", "30"))
AI_TEASE_CHANCE = float(os.getenv("AI_TEASE_CHANCE", "0.35"))  # шанс подкола когда отвечает

# -----------------------
# REGEXES
# -----------------------
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

IG_RE = re.compile(r"https?://(www\.)?instagram\.com/\S+", re.IGNORECASE)
TT_RE = re.compile(r"https?://(vm\.)?tiktok\.com/\S+|https?://www\.tiktok\.com/\S+", re.IGNORECASE)

TT_PHOTO_HINT_RE = re.compile(r"/photo/", re.IGNORECASE)

# -----------------------
# DB (ranks/stats)
# -----------------------
DB_PATH = "bot.db"

RANKS = [
    (0,    "Новичок 🥚"),
    (50,   "Постер 📝"),
    (200,  "Чатовый 🗣️"),
    (500,  "Мемный солдат 🪖"),
    (1200, "Мемный магистр 🧙‍♂️"),
    (2500, "Легенда чата 👑"),
]

def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = db()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_stats (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        msg_count INTEGER NOT NULL DEFAULT 0,
        media_ok INTEGER NOT NULL DEFAULT 0,
        media_fail INTEGER NOT NULL DEFAULT 0,
        last_msg_ts INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat_stats (
        chat_id INTEGER PRIMARY KEY,
        started_ts INTEGER NOT NULL,
        total_msgs INTEGER NOT NULL DEFAULT 0,
        total_media_ok INTEGER NOT NULL DEFAULT 0,
        total_media_fail INTEGER NOT NULL DEFAULT 0
    )
    """)
    con.commit()
    con.close()

def ensure_chat_row(chat_id: int):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT chat_id FROM chat_stats WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO chat_stats(chat_id, started_ts, total_msgs, total_media_ok, total_media_fail) VALUES(?,?,?,?,?)",
            (chat_id, int(time.time()), 0, 0, 0),
        )
    con.commit()
    con.close()

def inc_message(chat_id: int, user_id: int, username: Optional[str], first_name: Optional[str]):
    now = int(time.time())
    con = db()
    cur = con.cursor()

    ensure_chat_row(chat_id)

    cur.execute("""
    INSERT INTO user_stats(chat_id, user_id, username, first_name, msg_count, media_ok, media_fail, last_msg_ts)
    VALUES(?,?,?,?,1,0,0,?)
    ON CONFLICT(chat_id, user_id) DO UPDATE SET
        username=excluded.username,
        first_name=excluded.first_name,
        msg_count = msg_count + 1,
        last_msg_ts = excluded.last_msg_ts
    """, (chat_id, user_id, username, first_name, now))

    cur.execute("UPDATE chat_stats SET total_msgs = total_msgs + 1 WHERE chat_id=?", (chat_id,))
    con.commit()
    con.close()

def inc_media_result(chat_id: int, user_id: int, ok: bool):
    con = db()
    cur = con.cursor()
    ensure_chat_row(chat_id)

    if ok:
        cur.execute("UPDATE user_stats SET media_ok = media_ok + 1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        cur.execute("UPDATE chat_stats SET total_media_ok = total_media_ok + 1 WHERE chat_id=?", (chat_id,))
    else:
        cur.execute("UPDATE user_stats SET media_fail = media_fail + 1 WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        cur.execute("UPDATE chat_stats SET total_media_fail = total_media_fail + 1 WHERE chat_id=?", (chat_id,))
    con.commit()
    con.close()

def get_rank(msg_count: int) -> str:
    title = RANKS[0][1]
    for threshold, name in RANKS:
        if msg_count >= threshold:
            title = name
        else:
            break
    return title

def fmt_user(urow) -> str:
    if urow["username"]:
        return "@" + urow["username"]
    if urow["first_name"]:
        return urow["first_name"]
    return str(urow["user_id"])

# -----------------------
# Reactions (feature 4)
# -----------------------
REACTION_CHANCE = 0.06
REACTION_COOLDOWN = 25
_chat_last_react = defaultdict(int)

KEYWORD_REACTIONS = {
    "ахаха": ["🤣", "😂", "💀"],
    "ору": ["💀", "🤣"],
    "рофл": ["🤣", "💀", "😂"],
    "лол": ["😂", "🤣"],
    "xd": ["💀", "🤣"],
    "хд": ["💀", "😂"],
    "мем": ["🗿", "😂", "🔥"],
    "жесть": ["😳", "💀", "🫣"],
}
RANDOM_REACTIONS = ["😂", "🤣", "💀", "😳", "🗿", "🔥", "🫡", "🤝", "😈", "😱"]

async def maybe_react(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or not msg.text:
        return

    text = msg.text.lower().strip()

    # прикол: если ровно "бот"
    if text == "бот":
        await msg.reply_text("сам бот 😡")
        return

    # шутка про мут
    if "чел ты а муте" in text or "чел ты в муте" in text:
        await msg.reply_text("🔇 Человек отправлен в мут 😎")
        return

    now = time.time()
    if now - _chat_last_react[chat.id] < REACTION_COOLDOWN:
        return
    if random.random() > REACTION_CHANCE:
        return

    for kw, emojis in KEYWORD_REACTIONS.items():
        if kw in text:
            _chat_last_react[chat.id] = now
            await msg.reply_text(random.choice(emojis))
            return

    _chat_last_react[chat.id] = now
    await msg.reply_text(random.choice(RANDOM_REACTIONS))

# -----------------------
# Mini-AI (reply every N messages + always on tag + tease)
# -----------------------
_chat_message_counter = defaultdict(int)
_ai_last_reply_ts = defaultdict(int)

AI_EMOJIS = ["😳", "💀", "🤣", "😂", "🗿", "🔥", "😈", "🤝", "🫡", "🥴", "🫠", "😮‍💨"]
AI_PHRASES = [
    "ну это база 😎",
    "жёстко… 💀",
    "ахах хорош 😂",
    "я в шоке 😳",
    "мне нравится ход мыслей 🗿",
    "чисто по факту 🤝",
    "ладно-ладно, понял 😅",
    "я такое одобряю 🫡",
    "молчу-молчу 🤐",
    "это уже интересно 👀",
    "вот это поворот 😈",
    "спокойно, не кипишуем 🫠",
]
AI_TEASES = [
    "{u}, ты опять начинаешь 😎",
    "{u}, ну ты даёшь 😂",
    "{u}, это было мощно 💀",
    "{u}, аккуратнее, ща легендой станешь 🗿",
    "{u}, не пали контору 🤫",
    "{u}, давай без фанатизма 😈",
]

def bot_is_tagged(text: str) -> bool:
    t = (text or "").lower()
    if BOT_USERNAME and f"@{BOT_USERNAME}" in t:
        return True
    # слово "бот" как отдельное слово
    if re.search(r"(?<!\w)бот(?!\w)", t):
        return True
    return False

async def maybe_ai_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return
    if msg.text and msg.text.startswith("/"):
        return

    now = int(time.time())
    if now - _ai_last_reply_ts[chat.id] < AI_COOLDOWN_SECONDS:
        return

    tagged = bot_is_tagged(text)

    _chat_message_counter[chat.id] += 1
    should_reply = tagged or (_chat_message_counter[chat.id] % max(1, AI_EVERY_MESSAGES) == 0)

    if not should_reply:
        return

    _ai_last_reply_ts[chat.id] = now

    # Иногда делаем подкол с упоминанием автора
    tease = tagged and (random.random() < AI_TEASE_CHANCE)
    if tease:
        mention = user.mention_html()
        phrase = random.choice(AI_TEASES).format(u=mention)
        await msg.reply_text(phrase, parse_mode=ParseMode.HTML)
        return

    # обычный ответ
    if tagged:
        reply = random.choice(AI_PHRASES + AI_EMOJIS)
    else:
        # раз в N сообщений — чаще эмодзи (чтобы не флудить)
        reply = random.choice(AI_EMOJIS + AI_EMOJIS + AI_PHRASES)

    await msg.reply_text(reply)

# -----------------------
# RSS anti-duplicate
# -----------------------
RSS_STATE_FILE = os.getenv("RSS_STATE_FILE", "rss_state.json").strip()

def load_rss_state() -> Dict[str, List[str]]:
    try:
        with open(RSS_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": []}

def save_rss_state(state: Dict[str, List[str]]):
    try:
        with open(RSS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def rss_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Instagram", url=BTN_INSTAGRAM),
        InlineKeyboardButton("Facebook", url=BTN_FACEBOOK),
        InlineKeyboardButton("Site", url=BTN_SITE),
    ]])

# -----------------------
# URL helpers
# -----------------------
async def resolve_final_url(url: str) -> str:
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
    return URL_RE.findall(text or "")

def friendly_block_reason(err_text: str) -> Optional[str]:
    t = (err_text or "").lower()
    if "unavailable for certain audiences" in t or "inappropriate" in t or "sensitive" in t:
        return (
            "Чаще всего если пост:\n"
            "🔞 18+ / sensitive content\n"
            "🔒 только для залогиненных\n"
            "🚫 ограничен по региону\n"
            "👤 аккаунт private\n"
            "⚠️ Instagram пометил как sensitive"
        )
    if "login" in t or "cookie" in t or "sign in" in t:
        return (
            "Похоже контент доступен только залогиненным.\n\n"
            "Чаще всего если пост:\n"
            "🔒 только для залогиненных\n"
            "👤 аккаунт private"
        )
    if "not available in your country" in t or "geo" in t or "region" in t:
        return "Похоже контент ограничен по региону.\n\n🚫 ограничен по региону"
    return None

# -----------------------
# yt-dlp download
# -----------------------
def ytldp_options(outtmpl: str, url: str) -> dict:
    fmt = "best"
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
        "merge_output_format": None,
        "postprocessors": [],
        "overwrites": True,
        "restrictfilenames": False,
    }

def pick_downloaded_files(folder: Path) -> List[Path]:
    files = []
    for p in folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in [".mp4", ".mov", ".mkv", ".webm", ".m4v", ".jpg", ".jpeg", ".png"]:
            files.append(p)
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return files

async def download_media_from_url(url: str) -> Tuple[List[Path], Optional[str]]:
    base = Path(tempfile.mkdtemp(prefix="yomabar_dl_"))
    outtmpl = str(base / "%(title).80s_%(id)s.%(ext)s")

    def _blocking() -> Tuple[List[Path], Optional[str]]:
        try:
            with YoutubeDL(ytldp_options(outtmpl, url)) as ydl:
                ydl.extract_info(url, download=True)
            files = pick_downloaded_files(base)
            if not files:
                return [], "Не нашёл скачанный файл (yt-dlp ничего не сохранил)."
            return files, None
        except Exception as e:
            return [], str(e)

    files, err = await asyncio.to_thread(_blocking)

    if err:
        try:
            for p in base.rglob("*"):
                if p.is_file():
                    p.unlink(missing_ok=True)
            base.rmdir()
        except Exception:
            pass
        return [], err

    return files, None

async def cleanup_files(files: List[Path]):
    if not files:
        return
    try:
        base = files[0].parent
        for p in base.rglob("*"):
            if p.is_file():
                p.unlink(missing_ok=True)
        try:
            base.rmdir()
        except Exception:
            pass
    except Exception:
        pass

# -----------------------
# COMMANDS
# -----------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Бот работает 😎\n"
        "Команды: /rules /nick /ping /rank /stats\n"
        f"Авто: удаляет /qrand через {DELETE_QRAND_AFTER_SECONDS}с, RSS->канал, "
        "и пробует превращать ссылки Instagram/TikTok в видео.\n"
        "Фото пока не качает 😅"
    )

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(RULES_TEXT)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.effective_message.reply_text(f"pong ✅\nchat_type={chat.type}\nchat_id={chat.id}")

async def cmd_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    con = db()
    cur = con.cursor()
    cur.execute("SELECT * FROM user_stats WHERE chat_id=? AND user_id=?", (chat.id, user.id))
    row = cur.fetchone()
    con.close()

    msg_count = row["msg_count"] if row else 0
    media_ok = row["media_ok"] if row else 0
    media_fail = row["media_fail"] if row else 0
    rank = get_rank(msg_count)

    next_thr = None
    for thr, _name in RANKS:
        if thr > msg_count:
            next_thr = thr
            break
    prog = "Ты уже на максимальном ранге 👑" if next_thr is None else f"До следующего ранга: {next_thr - msg_count} сообщений"

    await update.effective_message.reply_text(
        f"🏆 Твой ранг: {rank}\n"
        f"💬 Сообщений: {msg_count}\n"
        f"✅ Видео успешно: {media_ok}\n"
        f"❌ Видео не получилось: {media_fail}\n"
        f"➡️ {prog}"
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    con = db()
    cur = con.cursor()

    cur.execute("SELECT * FROM chat_stats WHERE chat_id=?", (chat.id,))
    c = cur.fetchone()
    if not c:
        con.close()
        await update.effective_message.reply_text("Пока нет статистики 🙃")
        return

    cur.execute("""
        SELECT * FROM user_stats
        WHERE chat_id=?
        ORDER BY msg_count DESC
        LIMIT 5
    """, (chat.id,))
    top = cur.fetchall()
    con.close()

    uptime = int(time.time()) - int(c["started_ts"])
    days = uptime // 86400
    hours = (uptime % 86400) // 3600

    lines = []
    for i, u in enumerate(top, start=1):
        lines.append(f"{i}) {fmt_user(u)} — {u['msg_count']} сообщений")

    top_text = "\n".join(lines) if lines else "нет данных"

    await update.effective_message.reply_text(
        f"📊 Статистика чата\n"
        f"⏱️ Веду статистику: {days}д {hours}ч\n"
        f"💬 Всего сообщений: {c['total_msgs']}\n"
        f"✅ Видео успешно: {c['total_media_ok']}\n"
        f"❌ Видео не получилось: {c['total_media_fail']}\n\n"
        f"🏅 Топ активных:\n{top_text}"
    )

async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    try:
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
        if not getattr(me, "can_promote_members", False):
            await msg.reply_text("❌ У бота нет права добавлять админов (can_promote_members).")
            return

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
        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat.id,
            user_id=user.id,
            custom_title=title,
        )
        await msg.reply_text(f"✅ Ок, твой ник теперь: {title}")
    except Exception as e:
        await msg.reply_text(f"❌ Не получилось поставить ник.\nТех: {e}")

# -----------------------
# /qrand delete
# -----------------------
async def delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    message_id = job.data["message_id"]
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def on_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    context.job_queue.run_once(
        delete_message_job,
        when=DELETE_QRAND_AFTER_SECONDS,
        data={"chat_id": msg.chat_id, "message_id": msg.message_id},
        name=f"del_qrand_{msg.chat_id}_{msg.message_id}"
    )

# -----------------------
# Stats counter + reactions + miniAI
# -----------------------
async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    inc_message(chat.id, user.id, user.username, user.first_name)

    await maybe_react(update, context)
    await maybe_ai_reply(update, context)

# -----------------------
# Links IG/TT
# -----------------------
async def on_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    text = msg.text or ""
    urls = extract_urls(text)
    if not urls:
        return

    url = urls[0]
    final_url = await resolve_final_url(url)

    is_instagram = bool(IG_RE.search(final_url))
    is_tiktok = bool(TT_RE.search(final_url))
    if not (is_instagram or is_tiktok):
        return

    # TikTok photo
    if is_tiktok and TT_PHOTO_HINT_RE.search(final_url):
        await msg.reply_text(PHOTO_SORRY_TEXT)
        return

    await msg.reply_text("⏳ Пытаюсь скачать...")

    files, err = await download_media_from_url(final_url)
    if err:
        # Instagram photo post
        if is_instagram and ("there is no video in this post" in err.lower() or "no video" in err.lower()):
            await msg.reply_text(PHOTO_SORRY_TEXT)
            return

        # TikTok photo sometimes triggers Unsupported URL
        if is_tiktok and ("unsupported url" in err.lower()):
            await msg.reply_text(PHOTO_SORRY_TEXT)
            return

        friendly = friendly_block_reason(err)
        if friendly:
            await msg.reply_text("❌ Не получилось скачать.\n\n" + friendly)
            inc_media_result(chat.id, user.id, ok=False)
            return

        await msg.reply_text(
            "❌ Не получилось.\n"
            "Причина: Не получилось скачать. Возможно защита/блокировка или ссылка странная.\n\n"
            f"Тех.деталь: {err}"
        )
        inc_media_result(chat.id, user.id, ok=False)
        return

    try:
        fp = files[0]
        if fp.suffix.lower() in [".jpg", ".jpeg", ".png"]:
            await msg.reply_text(PHOTO_SORRY_TEXT)
            inc_media_result(chat.id, user.id, ok=False)
            return

        await msg.reply_video(video=fp.open("rb"))
        inc_media_result(chat.id, user.id, ok=True)

    except Exception as e:
        await msg.reply_text(f"❌ Не смог отправить видео.\nТех: {e}")
        inc_media_result(chat.id, user.id, ok=False)
    finally:
        await cleanup_files(files)

# -----------------------
# RSS JOB
# -----------------------
async def rss_tick(context: ContextTypes.DEFAULT_TYPE):
    if not RSS_URL or not RSS_CHANNEL_ID:
        return

    state = load_rss_state()
    seen = set(state.get("seen", []))

    feed = feedparser.parse(RSS_URL)
    entries = getattr(feed, "entries", []) or []
    if not entries:
        return

    new_entries = []
    for e in entries[:30]:
        key = (getattr(e, "id", None) or getattr(e, "guid", None) or getattr(e, "link", None) or "").strip()
        if not key or key in seen:
            continue
        new_entries.append((key, e))

    if not new_entries:
        return

    new_entries.reverse()
    kb = rss_keyboard()

    for key, e in new_entries:
        title = (getattr(e, "title", "") or "").strip()
        link = (getattr(e, "link", "") or "").strip()
        text = title if title else "Новый пост"
        if link:
            text += f"\n{link}"

        try:
            await context.bot.send_message(
                chat_id=int(RSS_CHANNEL_ID),
                text=text,
                reply_markup=kb,
                disable_web_page_preview=False,
            )
            seen.add(key)
        except Exception as ex:
            log.warning("RSS send failed: %s", ex)
            break

    state["seen"] = list(seen)[-600:]
    save_rss_state(state)

# -----------------------
# MAIN
# -----------------------
def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty")

    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("nick", cmd_nick))
    app.add_handler(CommandHandler("rank", cmd_rank))
    app.add_handler(CommandHandler("stats", cmd_stats))

    # /qrand
    app.add_handler(MessageHandler(filters.Regex(r"^/qrand\b"), on_qrand), group=5)

    # IG/TT links (раньше, чем on_any_message, чтобы считал media_ok/fail корректно)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_links), group=10)

    # Count msgs + reactions + miniAI (после)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_any_message), group=50)

    # RSS
    if RSS_URL and RSS_CHANNEL_ID:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10, name="rss_tick")

    return app

def main():
    app = build_app()
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
import os
import re
import json
import base64
import asyncio
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime, timezone

import feedparser
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
    InputMediaPhoto,
)
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# =========================
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@yomabar").strip()  # куда RSS постить
RSS_URL = os.getenv("RSS_URL", "").strip()
RSS_POLL_SECONDS = int(os.getenv("RSS_POLL_SECONDS", "120").strip())

# Instagram cookies (base64), optional
IG_COOKIES_B64 = os.getenv("IG_COOKIES", "").strip()

# =========================
# CONFIG
# =========================
DELETE_QRAND_AFTER_SECONDS = 30

WELCOME_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)

START_TEXT = (
    "Бот работает 😎\n"
    "Команды: /rules /nick /rssstatus /ping\n"
    f"Авто: удаляет /qrand через {DELETE_QRAND_AFTER_SECONDS}с, приветствует новых, "
    "RSS->канал, и пробует превращать ссылки Instagram/TikTok в видео/фото."
)

# link patterns
RE_IG = re.compile(r"(https?://(?:www\.)?instagram\.com/[^\s]+)", re.IGNORECASE)
RE_TT = re.compile(r"(https?://(?:www\.)?(?:tiktok\.com|vm\.tiktok\.com)/[^\s]+)", re.IGNORECASE)

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VID_EXT = {".mp4", ".mov", ".mkv", ".webm"}

# simple RSS dedupe storage (in-memory)
SEEN_RSS = set()

# =========================
# HELPERS
# =========================

def _write_ig_cookies_file(tmpdir: str) -> str | None:
    """
    IG_COOKIES must be base64 of Netscape cookies.txt (from browser).
    Returns path or None.
    """
    if not IG_COOKIES_B64:
        return None
    try:
        raw = base64.b64decode(IG_COOKIES_B64.encode("utf-8"))
        path = os.path.join(tmpdir, "cookies.txt")
        with open(path, "wb") as f:
            f.write(raw)
        return path
    except Exception:
        return None


def _pick_downloaded_files(download_dir: str):
    p = Path(download_dir)
    files = sorted([f for f in p.iterdir() if f.is_file()], key=lambda x: x.stat().st_mtime)
    videos = [f for f in files if f.suffix.lower() in VID_EXT]
    images = [f for f in files if f.suffix.lower() in IMG_EXT]
    return videos, images, files


def _run_yt_dlp(url: str, outdir: str) -> tuple[int, str]:
    """
    Returns (returncode, combined_output).
    We avoid merges (no ffmpeg needed).
    We DO NOT force video format, so image posts can download too (when IG allows).
    """
    outtpl = os.path.join(outdir, "%(id)s_%(autonumber)03d.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--restrict-filenames",
        "--no-call-home",
        "--no-check-certificate",
        "--geo-bypass",
        "--socket-timeout", "20",
        "--retries", "2",
        "--concurrent-fragments", "1",
        "-o", outtpl,

        # IMPORTANT: do not force -f bestvideo+bestaudio etc (needs ffmpeg and kills photo posts)
        # "-f", "best",   # even this can break some photo-only pages on IG; keep none.

        # headers / UA
        "--user-agent",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "--add-header", "Accept-Language:en-US,en;q=0.9",
    ]

    # IG-specific referer helps sometimes
    if "instagram.com" in url:
        cmd += ["--add-header", "Referer:https://www.instagram.com/"]

    # Cookies (optional but often required for IG now)
    cookies_path = _write_ig_cookies_file(outdir)
    if cookies_path:
        cmd += ["--cookies", cookies_path]

    cmd.append(url)

    proc = subprocess.run(cmd, capture_output=True, text=True)
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode, combined.strip()


def _nice_fail_reason(log: str) -> str:
    l = log.lower()

    if "ffmpeg is not installed" in l:
        return "Нужен ffmpeg (а у тебя его нет). Я настроен без merge, но в коде где-то всё ещё включено объединение форматов."
    if "unable to extract" in l or "unable to" in l:
        return "Instagram/TikTok не дают вытащить прямую ссылку (часто защита/блок). Для Instagram обычно помогает cookies."
    if "private" in l or "login" in l or "cookies" in l:
        return "Похоже пост требует логин. Для Instagram нужна переменная IG_COOKIES."
    if "there is no video in this post" in l:
        return "Это фото/карусель. Я попробовал скачать фото, но Instagram не отдал медиа (скорее всего нужна авторизация/cookies)."
    if "403" in l or "forbidden" in l:
        return "Доступ запрещён (403). Обычно помогает cookies/авторизация."
    if "429" in l:
        return "Слишком много запросов (429). Instagram/TikTok душат. Подожди и попробуй снова."
    return "Не получилось скачать. Возможно защита/блокировка или ссылка странная."


# =========================
# COMMANDS
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_TEXT)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(f"pong ✅\nchat_type={chat.type}\nchat_id={chat.id}")

async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"RSS_URL: {'✅' if RSS_URL else '❌'}\n"
        f"CHANNEL: {CHANNEL_USERNAME}\n"
        f"POLL: {RSS_POLL_SECONDS}s\n"
        f"SEEN: {len(SEEN_RSS)}"
    )
    await update.message.reply_text(msg)

async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /nick <name up to 16>
    Works only in supergroup (Telegram restriction for admin-title feature).
    """
    chat = update.effective_chat
    user = update.effective_user
    if chat.type != ChatType.SUPERGROUP:
        await update.message.reply_text("❌ /nick работает только в супер-группе.")
        return

    if not context.args:
        await update.message.reply_text("Напиши так: /nick Невеста")
        return

    nick = " ".join(context.args).strip()
    if len(nick) > 16:
        await update.message.reply_text("❌ Ник максимум 16 символов.")
        return

    # user must be admin; we can only set custom title for admins
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status not in ("administrator", "creator"):
            await update.message.reply_text("❌ Чтобы поставить ник, ты должен быть админом (Telegram так устроен).")
            return

        await context.bot.set_chat_administrator_custom_title(
            chat_id=chat.id,
            user_id=user.id,
            custom_title=nick,
        )
        await update.message.reply_text(f"✅ Ник поставлен: {nick}")

    except Exception as e:
        await update.message.reply_text(f"❌ Не получилось поставить ник.\nПричина: {e}")


# =========================
# AUTO: welcome / left -> ban button
# =========================

async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    chat = update.effective_chat

    old = result.old_chat_member
    new = result.new_chat_member
    user = new.user

    # joined
    if old.status in ("left", "kicked") and new.status in ("member", "administrator"):
        await context.bot.send_message(chat.id, WELCOME_TEXT)
        return

    # left
    if old.status in ("member", "administrator") and new.status == "left":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚫 Забанить ушедшего", callback_data=f"ban:{user.id}")
        ]])
        await context.bot.send_message(
            chat.id,
            f"👋 {user.full_name} вышел(ла). Если это спамер — жми бан:",
            reply_markup=kb
        )

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if not data.startswith("ban:"):
        return
    chat = update.effective_chat
    target_id = int(data.split(":")[1])

    try:
        await context.bot.ban_chat_member(chat.id, target_id)
        await q.edit_message_text("✅ Забанил.")
    except Exception as e:
        await q.edit_message_text(f"❌ Не смог забанить: {e}")


# =========================
# AUTO: delete /qrand after 30s
# =========================

async def on_text_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    # delete spam command
    if msg.text.strip().startswith("/qrand"):
        async def delayed_delete():
            await asyncio.sleep(DELETE_QRAND_AFTER_SECONDS)
            try:
                await msg.delete()
            except Exception:
                pass

        asyncio.create_task(delayed_delete())


# =========================
# LINKS: Instagram / TikTok -> download and send
# =========================

async def on_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text.strip()

    # ignore commands (handled elsewhere)
    if text.startswith("/"):
        return

    m = RE_IG.search(text) or RE_TT.search(text)
    if not m:
        return

    url = m.group(1).strip()

    status = await msg.reply_text("⏳ Пытаюсь скачать...")

    # run yt-dlp in a thread (avoid blocking)
    with tempfile.TemporaryDirectory(prefix="dl_") as tmpdir:
        rc, log = await asyncio.to_thread(_run_yt_dlp, url, tmpdir)
        videos, images, allfiles = _pick_downloaded_files(tmpdir)

        # If files downloaded -> send
        try:
            if videos:
                # send first video
                await context.bot.send_video(
                    chat_id=msg.chat_id,
                    video=videos[0].open("rb"),
                    caption=f"🎬 {url}",
                )
                await status.delete()
                return

            if images:
                # if multiple images -> media group
                if len(images) == 1:
                    await context.bot.send_photo(
                        chat_id=msg.chat_id,
                        photo=images[0].open("rb"),
                        caption=f"🖼️ {url}",
                    )
                else:
                    media = []
                    for i, img in enumerate(images[:10]):  # TG limit for album is 10
                        if i == 0:
                            media.append(InputMediaPhoto(img.open("rb"), caption=f"🖼️ {url}"))
                        else:
                            media.append(InputMediaPhoto(img.open("rb")))
                    await context.bot.send_media_group(chat_id=msg.chat_id, media=media)

                await status.delete()
                return

            # No files -> show reason
            reason = _nice_fail_reason(log)
            # keep short tech details
            short_log = log[-700:] if len(log) > 700 else log
            await status.edit_text(
                "❌ Не получилось.\n"
                f"Причина: {reason}\n\n"
                f"Тех.деталь: {short_log}"
            )

        except Exception as e:
            await status.edit_text(f"❌ Ошибка отправки в Telegram: {e}")


# =========================
# RSS -> channel
# =========================

async def rss_tick(context: ContextTypes.DEFAULT_TYPE):
    if not RSS_URL:
        return
    try:
        feed = feedparser.parse(RSS_URL)
        entries = feed.entries or []
        # oldest -> newest
        for e in reversed(entries):
            uid = (e.get("id") or e.get("link") or e.get("title") or "")[:500]
            if not uid or uid in SEEN_RSS:
                continue

            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            summary = (e.get("summary") or "").strip()

            # add your links
            extra = (
                "\n\n"
                "🔗 Instagram: https://www.instagram.com/yomabar.lt\n"
                "🔗 Facebook: https://www.facebook.com/share/1P3dFJ5f5Y/?mibextid=wwXIfr\n"
                "🔗 Website: https://www.yomahayoma.show/"
            )

            text = ""
            if title:
                text += f"<b>{title}</b>\n"
            if summary:
                # keep it not too long
                text += summary[:1500] + ("\n" if len(summary) > 0 else "")
            if link:
                text += f"\n<a href=\"{link}\">Открыть пост</a>"
            text += extra

            await context.bot.send_message(
                chat_id=CHANNEL_USERNAME,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            SEEN_RSS.add(uid)

    except Exception:
        # silent to avoid spam in logs
        return


# =========================
# MAIN
# =========================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty")

    app = Application.builder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("rssstatus", cmd_rssstatus))
    app.add_handler(CommandHandler("nick", cmd_nick))

    # chat member updates
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, lambda u, c: None))

    # callbacks
    app.add_handler(MessageHandler(filters.ALL, on_text_commands), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_links), group=1)
    app.add_handler(MessageHandler(filters.UpdateType.CALLBACK_QUERY, on_callback), group=2)

    # RSS job
    if RSS_URL:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
import os
import re
import json
import html
import asyncio
from typing import Optional, List, Tuple

import feedparser
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG (можно менять)
# =========================

WELCOME_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)

DELETE_QRAND_AFTER_SECONDS = 30

# Ссылки-футер (добавляется в RSS посты)
LINK_INSTAGRAM = "https://www.instagram.com/yomabar.lt?igsh=NmZxMzBnNWFjaHQy"
LINK_FACEBOOK = "https://www.facebook.com/share/1P3dFJ5f5Y/?mibextid=wwXIfr"
LINK_SITE = "https://www.yomahayoma.show/"

RSS_STATE_FILE = "rss_state.json"
RSS_STATE_MAX = 250  # сколько последних id помнить

SUPPORTED_DOMAINS = (
    "instagram.com",
    "tiktok.com",
    "vm.tiktok.com",
)

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)

# =========================
# UTILS
# =========================

def extract_text_or_caption(update: Update) -> str:
    msg = update.effective_message
    if not msg:
        return ""
    return (msg.text or msg.caption or "").strip()

def find_first_url(text: str) -> Optional[str]:
    m = URL_RE.search(text or "")
    if not m:
        return None
    url = m.group(1).strip()
    # Telegram часто добавляет скобки/пунктуацию
    url = url.rstrip(").,!?]}>\"'")
    return url

def is_supported_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(d in u for d in SUPPORTED_DOMAINS)

def load_rss_state() -> List[str]:
    try:
        if os.path.exists(RSS_STATE_FILE):
            with open(RSS_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [str(x) for x in data]
    except Exception:
        pass
    return []

def save_rss_state(items: List[str]) -> None:
    try:
        items = items[-RSS_STATE_MAX:]
        with open(RSS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
    except Exception:
        pass

def html_footer_links() -> str:
    return (
        f'\n\n🔗 <a href="{html.escape(LINK_INSTAGRAM)}">Instagram</a>'
        f' | <a href="{html.escape(LINK_FACEBOOK)}">Facebook</a>'
        f' | <a href="{html.escape(LINK_SITE)}">Сайт</a>'
    )

# =========================
# DOWNLOAD (yt-dlp)
# =========================

async def download_with_ytdlp(url: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """
    Возвращает (files, error_text)
    files = список путей файлов, уже скачанных (фото/видео)
    """
    try:
        import yt_dlp
    except Exception:
        return None, "yt-dlp не установлен (добавь в requirements.txt: yt-dlp)"

    outtmpl = "download_%(id)s_%(autonumber)s.%(ext)s"

    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,

        # Чтобы НЕ требовать ffmpeg — стараемся брать один mp4, иначе yt-dlp может хотеть merge
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
    }

    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            files: List[str] = []

            def _collect(one: dict):
                fname = ydl.prepare_filename(one)
                if os.path.exists(fname):
                    files.append(fname)
                    return
                vid = one.get("id")
                if vid:
                    for f in os.listdir("."):
                        if f.startswith("download_") and vid in f:
                            files.append(f)

            if isinstance(info, dict) and info.get("entries"):
                for e in info["entries"]:
                    if isinstance(e, dict):
                        _collect(e)
            elif isinstance(info, dict):
                _collect(info)

            # уникальные + существующие
            files = list(dict.fromkeys([f for f in files if os.path.exists(f)]))
            return files

    try:
        files = await asyncio.to_thread(_run)
        if not files:
            return None, "Скачивание прошло, но файлов не нашёл (возможно блок/редирект)."
        return files, None
    except Exception as e:
        return None, str(e)

# =========================
# HANDLERS
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Бот работает 😎\n"
        "Команды: /rules /nick /rssstatus\n"
        "Авто: удаляет /qrand через 30 сек, приветствует новых, бан-кнопка после выхода, RSS->канал, "
        "и пробует превращать ссылки Instagram/TikTok в фото/видео."
    )

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(WELCOME_TEXT)

async def cmd_rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rss_url = os.getenv("RSS_URL", "").strip()
    chan = os.getenv("CHANNEL_USERNAME", "").strip()
    poll = os.getenv("RSS_POLL_SECONDS", "").strip()
    state = load_rss_state()
    await update.effective_message.reply_text(
        "RSS статус:\n"
        f"- RSS_URL: {rss_url or '❌ нет'}\n"
        f"- CHANNEL_USERNAME: {chan or '❌ нет'}\n"
        f"- RSS_POLL_SECONDS: {poll or '❌ нет'}\n"
        f"- remembered items: {len(state)}"
    )

async def cmd_nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /nick <title up to 16>
    Делает пользователя админом с минимальными правами и ставит кастомный title.
    Работает только в супер-группе и если бот админ.
    """
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    if chat.type not in ("supergroup", "group"):
        await msg.reply_text("❌ /nick работает только в группе/супергруппе.")
        return

    title = " ".join(context.args).strip()
    if not title:
        await msg.reply_text("Напиши так: /nick твой_ник (до 16 символов)")
        return

    if len(title) > 16:
        await msg.reply_text("❌ Ник слишком длинный. Максимум 16 символов.")
        return

    try:
        me = await context.bot.get_chat_member(chat.id, context.bot.id)
        if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await msg.reply_text("❌ Я должен быть админом в чате, чтобы ставить ники.")
            return
    except Exception:
        pass

    # минимальные админ-права (всё False)
    rights = {
        "can_manage_chat": False,
        "can_delete_messages": False,
        "can_manage_video_chats": False,
        "can_restrict_members": False,
        "can_promote_members": False,
        "can_change_info": False,
        "can_invite_users": False,
        "can_post_stories": False,
        "can_edit_stories": False,
        "can_delete_stories": False,
        "can_pin_messages": False,
        "can_manage_topics": False,
    }

    try:
        # 1) делаем админом (если уже админ — Telegram просто применит права)
        await context.bot.promote_chat_member(chat.id, user.id, **rights)

        # 2) ставим custom title
        await context.bot.set_chat_administrator_custom_title(chat.id, user.id, title)

        await msg.reply_text(f"✅ Ник установлен: {title}")

    except BadRequest as e:
        await msg.reply_text(
            "❌ Не получилось.\n"
            "Возможные причины:\n"
            "- чат не супергруппа\n"
            "- я не админ\n"
            "- Telegram не дал выдать админку\n\n"
            f"Тех.деталь: {e}"
        )
    except Exception as e:
        await msg.reply_text(f"❌ Ошибка: {e}")

async def handle_qrand_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return

    text = msg.text.strip().lower()
    # учитываем /qrand@botname
    if not (text.startswith("/qrand") or text.startswith("/qrand@")):
        return

    async def _delete_later(ctx: ContextTypes.DEFAULT_TYPE):
        chat_id = ctx.job.data["chat_id"]
        message_id = ctx.job.data["message_id"]
        try:
            await ctx.bot.delete_message(chat_id, message_id)
        except Exception:
            pass

    context.job_queue.run_once(
        _delete_later,
        when=DELETE_QRAND_AFTER_SECONDS,
        data={"chat_id": msg.chat_id, "message_id": msg.message_id},
        name=f"del_qrand_{msg.chat_id}_{msg.message_id}",
    )

async def on_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    - приветствуем новых
    - на выход даём кнопку бан
    """
    chat = update.effective_chat
    if not chat:
        return

    cmu = update.chat_member
    if not cmu:
        return

    old = cmu.old_chat_member
    new = cmu.new_chat_member
    user = new.user

    # join
    if old.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) and new.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    ):
        try:
            await context.bot.send_message(chat.id, WELCOME_TEXT)
        except Exception:
            pass

    # leave
    if old.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER) and new.status == ChatMemberStatus.LEFT:
        name = user.full_name
        cb = f"ban:{user.id}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🔨 Забанить {name}", callback_data=cb)]])
        try:
            await context.bot.send_message(
                chat.id,
                f"🚪 {name} вышел(ла). Если надо — бан:",
                reply_markup=kb,
            )
        except Exception:
            pass

async def on_ban_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()

    data = q.data or ""
    if not data.startswith("ban:"):
        return

    try:
        user_id = int(data.split(":", 1)[1])
    except Exception:
        return

    chat = q.message.chat if q.message else None
    if not chat:
        return

    # Проверим, что нажал админ
    try:
        clicker = q.from_user
        member = await context.bot.get_chat_member(chat.id, clicker.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await q.edit_message_text("❌ Только админы могут банить.")
            return
    except Exception:
        pass

    try:
        await context.bot.ban_chat_member(chat.id, user_id)
        await q.edit_message_text("✅ Забанен.")
    except Exception as e:
        await q.edit_message_text(f"❌ Не получилось забанить: {e}")

async def on_supported_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    text = extract_text_or_caption(update)
    url = find_first_url(text)
    if not url or not is_supported_url(url):
        return

    status = await msg.reply_text("⏳ Пытаюсь скачать (фото/видео)…")

    files: List[str] = []
    try:
        files, err = await download_with_ytdlp(url)
        if err or not files:
            await status.edit_text(
                "❌ Не получилось.\n"
                "Причина: Не получилось скачать. Возможно защита/блокировка или ссылка странная.\n\n"
                f"Тех.деталь: {err}"
            )
            return

        photos = []
        videos = []
        other = []

        for f in files:
            ext = os.path.splitext(f.lower())[1]
            if ext in VIDEO_EXTS:
                videos.append(f)
            elif ext in PHOTO_EXTS:
                photos.append(f)
            else:
                other.append(f)

        media = []
        for f in photos:
            media.append(InputMediaPhoto(media=open(f, "rb")))
        for f in videos:
            media.append(InputMediaVideo(media=open(f, "rb")))

        sent_any = False

        if media:
            for i in range(0, len(media), 10):
                chunk = media[i : i + 10]
                await context.bot.send_media_group(chat_id=chat.id, media=chunk)
            sent_any = True

        for f in other:
            await context.bot.send_document(chat_id=chat.id, document=open(f, "rb"))
            sent_any = True

        if sent_any:
            await status.delete()
        else:
            await status.edit_text("❌ Скачал, но нечего отправлять (неизвестные форматы).")

    except Exception as e:
        try:
            await status.edit_text(f"❌ Ошибка: {e}")
        except Exception:
            pass
    finally:
        # cleanup
        for f in files or []:
            try:
                os.remove(f)
            except Exception:
                pass

# =========================
# RSS JOB
# =========================

async def rss_tick(context: ContextTypes.DEFAULT_TYPE):
    rss_url = os.getenv("RSS_URL", "").strip()
    channel = os.getenv("CHANNEL_USERNAME", "").strip()
    if not rss_url or not channel:
        return

    seen = load_rss_state()

    try:
        feed = await asyncio.to_thread(feedparser.parse, rss_url)
        entries = feed.entries or []
    except Exception:
        return

    # идём от старых к новым
    new_items = []
    for e in reversed(entries):
        uid = str(getattr(e, "id", "") or getattr(e, "guid", "") or getattr(e, "link", "") or "")
        if not uid:
            continue
        if uid in seen:
            continue
        new_items.append((uid, e))

    if not new_items:
        return

    for uid, e in new_items:
        title = (getattr(e, "title", "") or "").strip()
        link = (getattr(e, "link", "") or "").strip()

        desc = ""
        if hasattr(e, "summary") and e.summary:
            desc = re.sub(r"<[^>]+>", "", e.summary)  # грубо чистим html
            desc = desc.strip()

        parts = []
        if title:
            parts.append(f"<b>{html.escape(title)}</b>")
        if desc:
            # ограничим чтоб не было километров
            if len(desc) > 1200:
                desc = desc[:1200] + "…"
            parts.append(html.escape(desc))

        if link:
            parts.append(f'👉 <a href="{html.escape(link)}">Открыть пост</a>')

        text = "\n\n".join(parts) + html_footer_links()

        try:
            await context.bot.send_message(
                chat_id=channel,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
        except Exception:
            # если не получилось — всё равно не спамим бесконечно одним и тем же
            pass

        seen.append(uid)

    save_rss_state(seen)

# =========================
# MAIN
# =========================

def main():
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения.")

    app = ApplicationBuilder().token(token).build()

    # команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("rssstatus", cmd_rssstatus))
    app.add_handler(CommandHandler("nick", cmd_nick))

    # авто-удаление /qrand
    app.add_handler(MessageHandler(filters.TEXT & (~filters.UpdateType.EDITED_MESSAGE), handle_qrand_delete))

    # ссылки IG/TT (и текст и caption)
    app.add_handler(MessageHandler((filters.TEXT | filters.CaptionRegex(URL_RE.pattern)) & (~filters.UpdateType.EDITED_MESSAGE), on_supported_link))

    # join/leave
    app.add_handler(ChatMemberHandler(on_member_update, ChatMemberHandler.CHAT_MEMBER))

    # бан-кнопка
    app.add_handler(CallbackQueryHandler(on_ban_button, pattern=r"^ban:\d+$"))

    # RSS job
    poll_s = int(os.getenv("RSS_POLL_SECONDS", "120").strip() or "120")
    app.job_queue.run_repeating(rss_tick, interval=poll_s, first=10)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
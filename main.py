import os
import re
import json
import html
import asyncio
import tempfile
from pathlib import Path
from typing import Optional, Tuple, List

import feedparser
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest


# =======================
# ENV / SETTINGS
# =======================
TOKEN = os.environ.get("BOT_TOKEN", "").strip()

RSS_URL = os.environ.get("RSS_URL", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()  # @yomabar
RSS_POLL_SECONDS = int(os.environ.get("RSS_POLL_SECONDS", "120"))
RSS_STATE_FILE = "rss_state.json"

DELETE_WELCOME_AFTER_SECONDS = 30
DELETE_QRAND_AFTER_SECONDS = 30

# удалить исходную ссылку на видео после успешной отправки (0 = не удалять)
DELETE_VIDEO_LINK_AFTER_SECONDS = 30

# ограничим размер отправки видео (Telegram может больше, но так надежнее)
MAX_VIDEO_MB = 50
MAX_VIDEO_BYTES = MAX_VIDEO_MB * 1024 * 1024

# ссылки для кнопок под RSS постами
INSTAGRAM_URL = "https://www.instagram.com/yomabar.lt?igsh=NmZxMzBnNWFjaHQy"
FACEBOOK_URL = "https://www.facebook.com/share/1P3dFJ5f5Y/?mibextid=wwXIfr"
WEBSITE_URL = "https://www.yomahayoma.show/?fbclid=IwVERFWAQSeMZleHRuA2FlbQIxMABzcnRjBmFwcF9pZAo2NjI4NTY4Mzc5AAEesge47GAJQ72RstwAGARsRXJktokh_iExhSv_5IPnccBzVBz8tW9oLkKuFtY_aem_0ZLfyoSFOW9iUSYpi0ElTQ"

RULES_TEXT = (
    "😼😳😨🤨Добро пожаловать в наш клаб хаус🤨😨😳😼\n\n"
    "🤩🥺Наши правила:🥺🤩\n"
    "😖🤬Без политики! 🤬😣\n"
    "😶‍🌫️🤯😳Не обижать друг друга!😳🤯😶‍🌫️"
)
WELCOME_TEXT = "👋 {mention}\n\n" + RULES_TEXT


# =======================
# REGEX (TikTok + IG)
# =======================
IG_RE = re.compile(r"(https?://(?:www\.)?instagram\.com/[^\s]+)", re.IGNORECASE)
TT_RE = re.compile(r"(https?://(?:www\.)?(?:vm\.)?tiktok\.com/[^\s]+|https?://(?:www\.)?tiktok\.com/[^\s]+)",
                   re.IGNORECASE)


# =======================
# HELPERS
# =======================
def mention_html(user) -> str:
    name = (user.full_name or "пользователь").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user.id}">{html.escape(name)}</a>'


async def delete_later(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except:
        pass


async def is_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except:
        return False


def load_rss_state() -> dict:
    if os.path.exists(RSS_STATE_FILE):
        try:
            with open(RSS_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_rss_state(state: dict):
    try:
        with open(RSS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except:
        pass


def entry_id(e) -> str:
    return getattr(e, "id", "") or getattr(e, "guid", "") or getattr(e, "link", "") or ""


def entry_link(e) -> str:
    return getattr(e, "link", "") or ""


def entry_caption(e) -> str:
    for k in ("title", "summary", "description"):
        v = getattr(e, k, "")
        if v:
            return v
    return ""


def entry_images(e) -> List[str]:
    urls = []
    media_content = getattr(e, "media_content", None)
    if media_content:
        for m in media_content:
            u = m.get("url")
            if u:
                urls.append(u)

    media_thumbnail = getattr(e, "media_thumbnail", None)
    if media_thumbnail:
        for m in media_thumbnail:
            u = m.get("url")
            if u:
                urls.append(u)

    enclosures = getattr(e, "enclosures", None)
    if enclosures:
        for it in enclosures:
            u = it.get("href") or it.get("url")
            if u:
                urls.append(u)

    out, seen = [], set()
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out[:10]


def social_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📸 Instagram", url=INSTAGRAM_URL),
        InlineKeyboardButton("📘 Facebook", url=FACEBOOK_URL),
        InlineKeyboardButton("🌐 Website", url=WEBSITE_URL),
    ]])


def pick_first_supported_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = TT_RE.search(text)
    if m:
        return m.group(1)
    m = IG_RE.search(text)
    if m:
        return m.group(1)
    return None


def pretty_reason(err: str) -> str:
    s = (err or "").lower()
    if "ffmpeg" in s:
        return "На сервере нет ffmpeg (мы его не используем), но сайт мог отдать только раздельные потоки."
    if "private" in s:
        return "Видео приватное/закрыто."
    if "login" in s or "cookies" in s or "sign in" in s:
        return "Требуется вход/куки. Instagram часто не даёт скачать без доступа."
    if "403" in s or "forbidden" in s:
        return "Доступ запрещён (403). Сайт мог заблокировать сервер."
    if "404" in s or "not found" in s:
        return "Видео не найдено (возможно удалено)."
    if "geo" in s or "not available in your country" in s:
        return "Региональная блокировка (geo)."
    if "timeout" in s or "timed out" in s:
        return "Таймаут сети. Попробуй позже."
    return "Не получилось скачать. Возможно защита/блокировка или ссылка странная."


# =======================
# VIDEO DOWNLOAD (NO FFMPEG)
# =======================
def ytdlp_download_no_ffmpeg(url: str, out_dir: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Скачиваем только готовый mp4. Без merge, без ffmpeg.
    Возвращает (filepath, error)
    """
    outtmpl = str(Path(out_dir) / "%(id)s.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 15,
        # ВАЖНО: только готовый mp4, иначе ffmpeg нужен
        "format": "best[ext=mp4]/best",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Mobile Safari/537.36"
        },
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        # ищем любой файл в папке
        files = sorted(Path(out_dir).glob("*"))
        for f in files:
            if f.is_file() and f.stat().st_size > 0:
                return str(f), None

        return None, "Файл не появился после скачивания."

    except DownloadError as e:
        return None, str(e)[:400]
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:400]}"


async def on_video_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    text = msg.text or msg.caption or ""
    url = pick_first_supported_url(text)
    if not url:
        return

    # не реагируем на /qrand (его отдельный хендлер)
    if (msg.text or "").strip().startswith("/qrand"):
        return

    status = await msg.reply_text("⏳ Пытаюсь скачать видео…")

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath, err = await asyncio.to_thread(ytdlp_download_no_ffmpeg, url, tmpdir)

        if not filepath:
            await status.edit_text(f"❌ Не получилось.\nПричина: {pretty_reason(err)}\n\nТех.деталь: {err}\n\nСсылка: {url}")
            return

        try:
            size = os.path.getsize(filepath)
            if size > MAX_VIDEO_BYTES:
                await status.edit_text(
                    f"❌ Видео слишком большое ({size/1024/1024:.1f} MB).\n"
                    f"Лимит {MAX_VIDEO_MB} MB.\nСсылка: {url}"
                )
                return

            with open(filepath, "rb") as f:
                await context.bot.send_video(
                    chat_id=msg.chat_id,
                    video=f,
                    caption="✅ Видео (авто)\n" + url,
                    supports_streaming=True,
                    reply_to_message_id=msg.message_id,
                )

            await status.delete()

            if DELETE_VIDEO_LINK_AFTER_SECONDS and DELETE_VIDEO_LINK_AFTER_SECONDS > 0:
                context.application.create_task(
                    delete_later(context, msg.chat_id, msg.message_id, DELETE_VIDEO_LINK_AFTER_SECONDS)
                )

        except BadRequest as e:
            await status.edit_text(
                "❌ Скачал, но Telegram не принял файл.\n"
                f"Причина: {e.message if hasattr(e,'message') else str(e)}\n"
                f"Ссылка: {url}"
            )
        except Exception as e:
            await status.edit_text(f"❌ Ошибка при отправке видео: {type(e).__name__}\nСсылка: {url}")


# =======================
# COMMANDS
# =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Бот работает 😎\nКоманды: /rules /nick /rssstatus")


async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(RULES_TEXT)


async def rssstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = context.application.bot_data.get("rss_state") or load_rss_state()
    await update.effective_message.reply_text(
        f"RSS_URL: {'✅' if RSS_URL else '❌'}\n"
        f"CHANNEL_ID: {CHANNEL_ID or '❌'}\n"
        f"poll: {RSS_POLL_SECONDS}s\n"
        f"last_id: {st.get('last_id', '') or '(пусто)'}"
    )


async def nick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type != "supergroup":
        await msg.reply_text("❌ /nick работает только в супергруппе.")
        return

    if not context.args:
        await msg.reply_text("Использование: /nick ТвойНик (до 16 символов)")
        return

    title = " ".join(context.args).strip()
    if len(title) > 16:
        await msg.reply_text("❌ Ник слишком длинный. Максимум 16 символов.")
        return

    try:
        # минимально делаем админом (иначе титул не поставить)
        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            can_manage_chat=True,
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
            custom_title=title
        )

        await msg.reply_text(f"✅ Ник установлен: {title}")

    except BadRequest as e:
        await msg.reply_text(
            "❌ Не получилось поставить ник.\n"
            "Проверь:\n"
            "1) бот админ и включено право «Добавлять администраторов»\n"
            "2) ты не владелец чата\n"
            f"\nОшибка: {e.message if hasattr(e,'message') else str(e)}"
        )


# =======================
# CHAT EVENTS
# =======================
async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return

    for u in msg.new_chat_members:
        sent = await msg.reply_text(
            WELCOME_TEXT.format(mention=mention_html(u)),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        context.application.create_task(
            delete_later(context, sent.chat_id, sent.message_id, DELETE_WELCOME_AFTER_SECONDS)
        )


async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.left_chat_member:
        return

    left = msg.left_chat_member
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🚫 Забанить {left.full_name}", callback_data=f"ban:{left.id}")
    ]])

    await msg.reply_text(
        f"👋 {left.full_name} вышел(ла) из чата.\nЕсли это спамер — можно забанить.",
        reply_markup=kb
    )


async def on_ban_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await is_admin(q.message.chat_id, q.from_user.id, context):
        await q.answer("Только админы могут банить.", show_alert=True)
        return

    try:
        target_id = int(q.data.split("ban:", 1)[1])
    except:
        await q.answer("Ошибка кнопки.", show_alert=True)
        return

    try:
        await context.bot.ban_chat_member(q.message.chat_id, target_id)
        await q.message.edit_text("✅ Забанен.")
    except Exception as e:
        await q.message.edit_text(f"❌ Не смог забанить: {type(e).__name__}")


async def on_qrand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.effective_message.text or "").strip()
    if txt.startswith("/qrand") or txt.startswith("/qrand@"):
        context.application.create_task(
            delete_later(context, update.effective_message.chat_id, update.effective_message.message_id, DELETE_QRAND_AFTER_SECONDS)
        )


# =======================
# RSS (no duplicates) + buttons
# =======================
async def rss_tick(context: ContextTypes.DEFAULT_TYPE):
    if not RSS_URL or not CHANNEL_ID:
        return

    state = context.application.bot_data.setdefault("rss_state", load_rss_state())
    last_id = state.get("last_id", "")

    feed = feedparser.parse(RSS_URL)
    entries = getattr(feed, "entries", []) or []
    if not entries:
        return

    # первый запуск — запоминаем самый свежий, не спамим старым
    if not last_id:
        newest = entry_id(entries[0])
        if newest:
            state["last_id"] = newest
            save_rss_state(state)
        return

    new_entries = []
    for e in entries:
        eid = entry_id(e)
        if not eid:
            continue
        if eid == last_id:
            break
        new_entries.append(e)

    if not new_entries:
        return

    new_entries.reverse()

    kb = social_buttons()

    for e in new_entries:
        eid = entry_id(e)
        link = entry_link(e)
        caption = (entry_caption(e) or "").strip()
        if link:
            caption = (caption + "\n\n" + link).strip()

        imgs = entry_images(e)

        try:
            if imgs:
                if len(imgs) > 1:
                    media = []
                    for i, url in enumerate(imgs[:10]):
                        if i == 0 and caption:
                            media.append(InputMediaPhoto(media=url, caption=caption[:1024]))
                        else:
                            media.append(InputMediaPhoto(media=url))
                    await context.bot.send_media_group(chat_id=CHANNEL_ID, media=media)
                    # кнопки отдельно (Telegram не поддерживает кнопки на альбом)
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text="🔗 Links:",
                        reply_markup=kb,
                        disable_web_page_preview=True
                    )
                else:
                    await context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=imgs[0],
                        caption=caption[:1024] if caption else None,
                        reply_markup=kb
                    )
            else:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=caption[:4096] if caption else link,
                    reply_markup=kb,
                    disable_web_page_preview=True
                )

            state["last_id"] = eid
            save_rss_state(state)

        except:
            pass


# =======================
# MAIN
# =======================
def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не задан.")

    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", rules))
    app.add_handler(CommandHandler("nick", nick))
    app.add_handler(CommandHandler("rssstatus", rssstatus))

    # Chat events
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))
    app.add_handler(CallbackQueryHandler(on_ban_button, pattern=r"^ban:"))

    # /qrand delete
    app.add_handler(MessageHandler(filters.COMMAND, on_qrand))
    app.add_handler(MessageHandler(filters.TEXT, on_qrand))

    # TikTok/Instagram links -> video
    app.add_handler(MessageHandler(filters.TEXT | filters.Caption, on_video_link))

    # RSS job
    if RSS_URL and CHANNEL_ID:
        app.job_queue.run_repeating(rss_tick, interval=RSS_POLL_SECONDS, first=10)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
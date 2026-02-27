import os
import time
import textwrap
from io import BytesIO
from dataclasses import dataclass, field
from typing import Dict, Tuple, Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "5"))

# ===== STATE =====
last_used: Dict[Tuple[int, int], float] = {}

@dataclass
class VoteState:
    up: int = 0
    down: int = 0
    voters: Dict[int, int] = field(default_factory=dict)

votes: Dict[int, VoteState] = {}

# avatar cache (to reduce Telegram calls)
avatar_cache: Dict[int, bytes] = {}

# ===== UI =====
def keyboard(poll_id: int, up: int, down: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"👍 {up}", callback_data=f"v|{poll_id}|up"),
            InlineKeyboardButton(f"👎 {down}", callback_data=f"v|{poll_id}|down"),
        ],
        [InlineKeyboardButton("❌ Убрать голос", callback_data=f"v|{poll_id}|clear")]
    ])

def extract_text(m) -> str:
    if not m:
        return ""
    t = (m.text or m.caption or "").strip()
    if not t or t.startswith("/"):
        return ""
    return t

def load_font(size: int) -> ImageFont.FreeTypeFont:
    # Гарантированный шрифт: положи DejaVuSans.ttf рядом с main.py
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except:
        # fallback (может не уметь кириллицу)
        return ImageFont.load_default()

def circle_crop(img: Image.Image, size: int) -> Image.Image:
    img = img.convert("RGB")
    img = ImageOps.fit(img, (size, size), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out

def wrap_by_pixels(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_w: int) -> str:
    # перенос по словам по пикселям
    words = text.split()
    if not words:
        return ""
    lines = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if (bbox[2] - bbox[0]) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return "\n".join(lines)

def render_sticker(author: str, text: str, avatar: Optional[Image.Image]) -> Image.Image:
    """
    Делает красивый 512x512 RGBA стикер.
    Важно: bubble и текст полностью непрозрачные => не будет "серой пустоты".
    """
    W = H = 512
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = 26
    bubble_w = W - pad * 2
    bubble_h = H - pad * 2

    # тень
    shadow = Image.new("RGBA", (bubble_w, bubble_h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((0, 0, bubble_w, bubble_h), radius=44, fill=(0, 0, 0, 180))
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    img.paste(shadow, (pad + 4, pad + 8), shadow)

    # bubble (чуть градиент)
    bubble = Image.new("RGBA", (bubble_w, bubble_h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bubble)
    bd.rounded_rectangle((0, 0, bubble_w, bubble_h), radius=44, fill=(42, 42, 46, 255))
    img.paste(bubble, (pad, pad), bubble)

    # layout
    avatar_size = 74
    left = pad + 18
    top = pad + 18
    gap = 16

    # avatar
    if avatar is not None:
        av = circle_crop(avatar, avatar_size)
    else:
        av = Image.new("RGBA", (avatar_size, avatar_size), (0, 0, 0, 0))
        ad = ImageDraw.Draw(av)
        ad.ellipse((0, 0, avatar_size, avatar_size), fill=(90, 90, 95, 255))
    img.paste(av, (left, top), av)

    # fonts
    name_font = load_font(28)

    # author (обрезаем чтобы не убегал)
    author = (author or "Unknown").strip()
    if len(author) > 22:
        author = author[:21] + "…"

    text_x = left + avatar_size + gap
    max_text_w = (pad + bubble_w - 18) - text_x

    # рисуем имя
    draw.text((text_x, top + 2), author, font=name_font, fill=(170, 210, 255, 255))

    # подбираем шрифт текста, чтобы влазило по высоте
    # доступная высота под текст
    text_top = top + 38
    max_text_h = (pad + bubble_h - 18) - text_top

    # убираем слишком длинное
    text = (text or "").strip()
    if len(text) > 500:
        text = text[:500] + "…"

    best_font = None
    best_wrapped = None

    for fs in range(36, 18, -2):
        f = load_font(fs)
        wrapped = wrap_by_pixels(draw, text, f, max_text_w)
        bbox = draw.multiline_textbbox((0, 0), wrapped, font=f, spacing=8)
        th = bbox[3] - bbox[1]
        if th <= max_text_h:
            best_font = f
            best_wrapped = wrapped
            break

    if best_font is None:
        best_font = load_font(18)
        best_wrapped = wrap_by_pixels(draw, text[:250] + "…", best_font, max_text_w)

    # текст (контраст)
    draw.multiline_text(
        (text_x, text_top),
        best_wrapped,
        font=best_font,
        fill=(245, 245, 245, 255),
        spacing=8
    )

    return img

def to_webp(img: Image.Image) -> BytesIO:
    bio = BytesIO()
    bio.name = "quand.webp"

    # Ужимаем до адекватного размера (Telegram любит небольшие стикеры)
    # transparency сохраняется
    for q in (90, 80, 70, 60, 50):
        bio.seek(0)
        bio.truncate(0)
        img.save(bio, format="WEBP", quality=q, method=6)
        if bio.tell() <= 450 * 1024:
            break

    bio.seek(0)
    return bio

async def get_avatar_image(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> Optional[Image.Image]:
    try:
        if user_id in avatar_cache:
            return Image.open(BytesIO(avatar_cache[user_id]))

        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count <= 0:
            return None

        file_id = photos.photos[0][-1].file_id
        f = await context.bot.get_file(file_id)
        data = await f.download_as_bytearray()
        avatar_cache[user_id] = bytes(data)
        return Image.open(BytesIO(data))
    except:
        return None

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Reply на сообщение → /quand (сделаю стикер-цитату + голосование)")

async def quand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user

    # cooldown
    key = (chat.id, user.id)
    now = time.time()
    if now - last_used.get(key, 0.0) < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last_used.get(key, 0.0)))
        await msg.reply_text(f"Подожди {wait} сек 🙂")
        return
    last_used[key] = now

    if not msg.reply_to_message:
        await msg.reply_text("Нужно reply на сообщение.")
        return

    text = extract_text(msg.reply_to_message)
    if not text:
        await msg.reply_text("В реплае нет текста (или это команда).")
        return

    ru = msg.reply_to_message.from_user
    author = ru.full_name if ru else "Unknown"
    avatar = await get_avatar_image(context, ru.id) if ru else None

    try:
        sticker_img = render_sticker(author, text, avatar)
        webp = to_webp(sticker_img)

        sent = await msg.reply_sticker(sticker=webp)

        poll = await context.bot.send_message(
            chat_id=chat.id,
            text="Голосование: 👍 0 | 👎 0",
            reply_to_message_id=sent.message_id
        )
        votes[poll.message_id] = VoteState()
        await poll.edit_reply_markup(reply_markup=keyboard(poll.message_id, 0, 0))
    except Exception as e:
        await msg.reply_text(f"Ошибка стикера: {type(e).__name__}. Если хочешь — скинь Railway Logs, добью до идеала.")

async def vote_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    parts = (q.data or "").split("|")
    if len(parts) != 3:
        return

    poll_id = int(parts[1])
    action = parts[2]
    state = votes.get(poll_id)
    if not state:
        return

    uid = q.from_user.id
    prev = state.voters.get(uid, 0)

    def set_vote(v: int):
        nonlocal prev
        if prev == 1:
            state.up -= 1
        elif prev == -1:
            state.down -= 1

        if v == 1:
            state.up += 1
        elif v == -1:
            state.down += 1

        if v == 0:
            state.voters.pop(uid, None)
        else:
            state.voters[uid] = v

    if action == "up":
        set_vote(1 if prev != 1 else 0)
    elif action == "down":
        set_vote(-1 if prev != -1 else 0)
    elif action == "clear":
        set_vote(0)

    await q.message.edit_text(
        f"Голосование: 👍 {state.up} | 👎 {state.down}",
        reply_markup=keyboard(poll_id, state.up, state.down)
    )

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quand", quand))
    app.add_handler(CommandHandler("q", quand))
    app.add_handler(CallbackQueryHandler(vote_handler))

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

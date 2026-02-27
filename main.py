import os
import time
import math
import textwrap
from io import BytesIO
from dataclasses import dataclass, field
from typing import Dict, Tuple, Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.environ.get("BOT_TOKEN", "")
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "5"))

last_used: Dict[Tuple[int, int], float] = {}

@dataclass
class VoteState:
    up: int = 0
    down: int = 0
    voters: Dict[int, int] = field(default_factory=dict)

votes: Dict[int, VoteState] = {}

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
    # DejaVuSans обычно есть на Linux/railway
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except:
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

def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    # перенос по словам, чтобы влезло по ширине
    words = text.split()
    if not words:
        return ""
    lines = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        bbox = draw.textbbox((0,0), test, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return "\n".join(lines)

def render_quote(author: str, text: str, avatar: Optional[Image.Image]) -> BytesIO:
    W = 720
    padding = 36
    bubble_radius = 42
    avatar_size = 84
    gap = 18

    # фон (тёмный как в твоём чате)
    bg = Image.new("RGB", (W, 600), (18, 18, 18))
    draw = ImageDraw.Draw(bg)

    # шрифты
    name_font = load_font(34)
    text_font = load_font(36)

    # зона текста (справа от аватарки)
    text_x = padding + avatar_size + gap
    max_text_width = W - text_x - padding

    # перенос текста
    wrapped = wrap_text(draw, text, text_font, max_text_width)
    if len(wrapped) > 900:
        wrapped = wrapped[:900] + "…"

    # размеры имени/текста
    name_bbox = draw.textbbox((0,0), author, font=name_font)
    name_h = name_bbox[3] - name_bbox[1]

    text_bbox = draw.multiline_textbbox((0,0), wrapped, font=text_font, spacing=10)
    text_h = text_bbox[3] - text_bbox[1]

    bubble_h = padding + name_h + 14 + text_h + padding
    bubble_w = W - 2*padding
    H = bubble_h + 2*padding

    bg = Image.new("RGB", (W, H), (18, 18, 18))
    draw = ImageDraw.Draw(bg)

    # bubble
    bubble_x1 = padding
    bubble_y1 = padding
    bubble_x2 = padding + bubble_w
    bubble_y2 = padding + bubble_h

    bubble = Image.new("RGBA", (bubble_w, bubble_h), (0,0,0,0))
    bd = ImageDraw.Draw(bubble)
    bd.rounded_rectangle((0,0,bubble_w,bubble_h), radius=bubble_radius, fill=(40, 40, 40, 255))

    bg.paste(bubble, (bubble_x1, bubble_y1), bubble)

    # аватар
    if avatar is not None:
        av = circle_crop(avatar, avatar_size)
        bg.paste(av, (padding + 18, padding + 18), av)
    else:
        # заглушка кружок
        tmp = Image.new("RGBA", (avatar_size, avatar_size), (0,0,0,0))
        td = ImageDraw.Draw(tmp)
        td.ellipse((0,0,avatar_size,avatar_size), fill=(90,90,90,255))
        bg.paste(tmp, (padding + 18, padding + 18), tmp)

    # имя + текст
    name_pos = (text_x, padding + 18)
    text_pos = (text_x, padding + 18 + name_h + 14)

    draw.text(name_pos, author, font=name_font, fill=(180, 210, 255))
    draw.multiline_text(text_pos, wrapped, font=text_font, fill=(240, 240, 240), spacing=10)

    bio = BytesIO()
    bio.name = "quand.png"
    bg.save(bio, "PNG", optimize=True)
    bio.seek(0)
    return bio

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Reply на сообщение → /quand (сделаю quote-стикер + голосование)")

async def quand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user

    # cooldown
    key = (chat.id, user.id)
    now = time.time()
    last = last_used.get(key, 0.0)
    if now - last < COOLDOWN_SECONDS:
        await msg.reply_text("Подожди чуть-чуть 🙂")
        return
    last_used[key] = now

    if not msg.reply_to_message:
        await msg.reply_text("Нужно reply на сообщение.")
        return

    text = extract_text(msg.reply_to_message)
    if not text:
        await msg.reply_text("В реплае нет текста (или это команда).")
        return

    author = msg.reply_to_message.from_user.full_name if msg.reply_to_message.from_user else "Unknown"

    # пытаемся получить аватар автора
    avatar_img = None
    try:
        uid = msg.reply_to_message.from_user.id
        photos = await context.bot.get_user_profile_photos(uid, limit=1)
        if photos.total_count > 0:
            file_id = photos.photos[0][-1].file_id
            f = await context.bot.get_file(file_id)
            data = await f.download_as_bytearray()
            avatar_img = Image.open(BytesIO(data))
    except:
        avatar_img = None

    image_bio = render_quote(author, text, avatar_img)
    sent = await msg.reply_photo(photo=image_bio)

    poll = await context.bot.send_message(
        chat_id=chat.id,
        text="Голосование: 👍 0 | 👎 0",
        reply_to_message_id=sent.message_id
    )
    votes[poll.message_id] = VoteState()
    await poll.edit_reply_markup(reply_markup=keyboard(poll.message_id, 0, 0))

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
        if prev == 1: state.up -= 1
        elif prev == -1: state.down -= 1
        if v == 1: state.up += 1
        elif v == -1: state.down += 1
        if v == 0: state.voters.pop(uid, None)
        else: state.voters[uid] = v

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
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quand", quand))
    app.add_handler(CallbackQueryHandler(vote_handler))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

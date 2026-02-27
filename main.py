import os
import time
import textwrap
from io import BytesIO
from dataclasses import dataclass, field
from typing import Dict, Tuple, Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ===== SETTINGS =====
TOKEN = os.environ.get("BOT_TOKEN", "")
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "5"))

# ===== STATE =====
last_used: Dict[Tuple[int, int], float] = {}

@dataclass
class VoteState:
    up: int = 0
    down: int = 0
    voters: Dict[int, int] = field(default_factory=dict)  # user_id -> 1/-1

votes: Dict[int, VoteState] = {}

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

def load_font(size: int):
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

def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    words = text.split()
    if not words:
        return ""
    lines = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return "\n".join(lines)

def render_quote_sticker(author: str, text: str, avatar: Optional[Image.Image]) -> Image.Image:
    """
    Возвращает RGBA 512x512 (под требования статического стикера).
    """
    W = H = 512
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))  # прозрачный фон
    draw = ImageDraw.Draw(img)

    pad = 28
    bubble_radius = 44
    avatar_size = 72
    gap = 16

    # bubble
    bubble = Image.new("RGBA", (W - 2 * pad, H - 2 * pad), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bubble)
    bd.rounded_rectangle(
        (0, 0, bubble.size[0], bubble.size[1]),
        radius=bubble_radius,
        fill=(40, 40, 40, 235),
    )
    img.paste(bubble, (pad, pad), bubble)

    # fonts
    name_font = load_font(28)
    # текстовый шрифт будем подбирать по размеру
    text_font_size = 34

    # зоны
    text_x = pad + 18 + avatar_size + gap
    max_text_w = W - text_x - pad - 18

    # avatar
    if avatar is not None:
        av = circle_crop(avatar, avatar_size)
        img.paste(av, (pad + 18, pad + 18), av)
    else:
        ph = Image.new("RGBA", (avatar_size, avatar_size), (0, 0, 0, 0))
        pd = ImageDraw.Draw(ph)
        pd.ellipse((0, 0, avatar_size, avatar_size), fill=(90, 90, 90, 255))
        img.paste(ph, (pad + 18, pad + 18), ph)

    # имя
    draw.text((text_x, pad + 14), author, font=name_font, fill=(180, 210, 255, 255))

    # подбор размера шрифта для текста, чтобы влезло по высоте
    available_h = (H - pad - 18) - (pad + 14 + 34 + 18)  # грубо, но стабильно

    best_font = None
    best_wrapped = None
    for fs in range(text_font_size, 18, -2):
        f = load_font(fs)
        wrapped = wrap_text(draw, text, f, max_text_w)
        bbox = draw.multiline_textbbox((0, 0), wrapped, font=f, spacing=8)
        th = bbox[3] - bbox[1]
        if th <= available_h:
            best_font = f
            best_wrapped = wrapped
            break

    if best_font is None:
        best_font = load_font(18)
        best_wrapped = wrap_text(draw, text[:240] + "…", best_font, max_text_w)

    # текст
    text_y = pad + 14 + 34 + 14
    draw.multiline_text(
        (text_x, text_y),
        best_wrapped,
        font=best_font,
        fill=(245, 245, 245, 255),
        spacing=8
    )

    return img

def to_webp_sticker(img: Image.Image) -> BytesIO:
    """
    Конвертит в WEBP для Telegram sticker.
    Стараемся уложиться в лимиты (обычно <=512KB).
    """
    bio = BytesIO()
    bio.name = "quand.webp"

    # Понижаем качество если надо, чтобы уменьшить вес.
    # lossless=True иногда делает файл слишком большим — поэтому используем quality.
    for q in (90, 80, 70, 60, 50):
        bio.seek(0)
        bio.truncate(0)
        img.save(bio, format="WEBP", quality=q, method=6)
        size = bio.tell()
        if size <= 480 * 1024:  # запас под лимиты
            break

    bio.seek(0)
    return bio

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Reply на сообщение → /quand (сделаю СТИКЕР + голосование)")

async def quand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user

    # cooldown
    key = (chat.id, user.id)
    now = time.time()
    if now - last_used.get(key, 0.0) < COOLDOWN_SECONDS:
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

    # avatar
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

    try:
        sticker_img = render_quote_sticker(author, text, avatar_img)
        webp = to_webp_sticker(sticker_img)

        sent_sticker = await msg.reply_sticker(sticker=webp)

        poll = await context.bot.send_message(
            chat_id=chat.id,
            text="Голосование: 👍 0 | 👎 0",
            reply_to_message_id=sent_sticker.message_id
        )
        votes[poll.message_id] = VoteState()
        await poll.edit_reply_markup(reply_markup=keyboard(poll.message_id, 0, 0))

    except Exception as e:
        # Если WEBP не поддерживается окружением — увидишь это сообщением
        await msg.reply_text(f"Ошибка при создании стикера: {type(e).__name__}\nОткрой Railway Logs — скажу точный фикс.")

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

import os
import time
import textwrap
from io import BytesIO
from dataclasses import dataclass, field
from typing import Dict, Tuple

from PIL import Image, ImageDraw, ImageFont
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ===== НАСТРОЙКИ =====
TOKEN = os.environ.get("BOT_TOKEN", "")
COOLDOWN_SECONDS = 5

# ===== СОСТОЯНИЕ =====
last_used: Dict[Tuple[int, int], float] = {}

@dataclass
class VoteState:
    up: int = 0
    down: int = 0
    voters: Dict[int, int] = field(default_factory=dict)

votes: Dict[int, VoteState] = {}

# ===== КНОПКИ =====
def keyboard(poll_id: int, up: int, down: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"👍 {up}", callback_data=f"v|{poll_id}|up"),
            InlineKeyboardButton(f"👎 {down}", callback_data=f"v|{poll_id}|down"),
        ],
        [
            InlineKeyboardButton("❌ Убрать голос", callback_data=f"v|{poll_id}|clear")
        ]
    ])

# ===== ВЫТАСКИВАЕМ ТЕКСТ =====
def extract_text(msg):
    if not msg:
        return ""
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return ""
    if text.startswith("/"):
        return ""
    return text

# ===== СТАРТ =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Сделай reply на сообщение и напиши /quand"
    )

# ===== ГЕНЕРАЦИЯ =====
async def quand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user

    # кулдаун
    key = (chat.id, user.id)
    now = time.time()
    last = last_used.get(key, 0)

    if now - last < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last))
        await msg.reply_text(f"Подожди {wait} сек 🙂")
        return

    last_used[key] = now

    if not msg.reply_to_message:
        await msg.reply_text("Нужно reply на сообщение")
        return

    text = extract_text(msg.reply_to_message)

    if not text:
        await msg.reply_text("В реплае нет текста")
        return

    # создаём картинку в памяти
    W, H = 512, 512
    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    pad = 24
    draw.rounded_rectangle((pad, pad, W - pad, H - pad), radius=40, fill=(240, 240, 240))

    wrapped = textwrap.fill(text, width=18)
    font = ImageFont.load_default()

    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=6)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    x = (W - tw) // 2
    y = (H - th) // 2

    draw.multiline_text((x, y), wrapped, font=font, fill=(20, 20, 20), spacing=6)

    bio = BytesIO()
    bio.name = "quand.png"
    img.save(bio, "PNG")
    bio.seek(0)

    sent = await msg.reply_photo(photo=bio)

    poll = await context.bot.send_message(
        chat_id=chat.id,
        text="Голосование: 👍 0 | 👎 0",
        reply_to_message_id=sent.message_id
    )

    votes[poll.message_id] = VoteState()
    await poll.edit_reply_markup(
        reply_markup=keyboard(poll.message_id, 0, 0)
    )

# ===== ГОЛОСОВАНИЕ =====
async def vote_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split("|")
    if len(data) != 3:
        return

    poll_id = int(data[1])
    action = data[2]

    state = votes.get(poll_id)
    if not state:
        return

    user_id = query.from_user.id
    prev = state.voters.get(user_id, 0)

    def set_vote(new_vote):
        nonlocal prev
        if prev == 1:
            state.up -= 1
        elif prev == -1:
            state.down -= 1

        if new_vote == 1:
            state.up += 1
        elif new_vote == -1:
            state.down += 1

        if new_vote == 0:
            state.voters.pop(user_id, None)
        else:
            state.voters[user_id] = new_vote

    if action == "up":
        set_vote(1 if prev != 1 else 0)
    elif action == "down":
        set_vote(-1 if prev != -1 else 0)
    elif action == "clear":
        set_vote(0)

    await query.message.edit_text(
        f"Голосование: 👍 {state.up} | 👎 {state.down}",
        reply_markup=keyboard(poll_id, state.up, state.down)
    )

# ===== ЗАПУСК =====
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quand", quand))
    app.add_handler(CallbackQueryHandler(vote_handler))

    app.run_polling()

if __name__ == "__main__":
    main()

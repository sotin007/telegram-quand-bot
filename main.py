# === TELEGRAM QUAND BOT ===
# VERSION 2
import os
import time
import textwrap
from dataclasses import dataclass, field
from typing import Dict, Tuple

from PIL import Image, ImageDraw, ImageFont
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

TOKEN = os.environ.get("BOT_TOKEN")
COOLDOWN_SECONDS = 20

last_used: Dict[Tuple[int, int], float] = {}

@dataclass
class Vote:
    up: int = 0
    down: int = 0
    voters: Dict[int, int] = field(default_factory=dict)

votes: Dict[int, Vote] = {}

def make_image(text, path):
    W, H = 512, 512
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle((20, 20, W-20, H-20), radius=40, fill=(255,255,255,240))

    font = ImageFont.load_default()
    wrapped = textwrap.fill(text, width=18)

    bbox = draw.multiline_textbbox((0,0), wrapped, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    x = (W - tw) // 2
    y = (H - th) // 2

    draw.multiline_text((x,y), wrapped, font=font, fill=(0,0,0))
    img.save(path)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Reply на сообщение и напиши /quand")

async def quand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.message

    if not msg.reply_to_message:
        await msg.reply_text("Нужно reply на сообщение")
        return

    key = (chat.id, user.id)
    now = time.time()

    if now - last_used.get(key, 0) < COOLDOWN_SECONDS:
        await msg.reply_text("Подожди немного 🙂")
        return

    last_used[key] = now

    text = msg.reply_to_message.text
    if not text:
        await msg.reply_text("Нет текста")
        return

    path = "/tmp/sticker.png"
    make_image(text, path)

    sent = await msg.reply_photo(photo=InputFile(path))

    poll = await context.bot.send_message(
        chat_id=chat.id,
        text="Голосование: 👍0 | 👎0",
        reply_to_message_id=sent.message_id,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👍 0", callback_data="up"),
                InlineKeyboardButton("👎 0", callback_data="down")
            ]
        ])
    )

    votes[poll.message_id] = Vote()

async def vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    state = votes.get(q.message.message_id)
    if not state:
        return

    user_id = q.from_user.id
    prev = state.voters.get(user_id)

    if q.data == "up":
        if prev == 1:
            return
        if prev == -1:
            state.down -= 1
        state.up += 1
        state.voters[user_id] = 1

    if q.data == "down":
        if prev == -1:
            return
        if prev == 1:
            state.up -= 1
        state.down += 1
        state.voters[user_id] = -1

    await q.message.edit_text(
        f"Голосование: 👍{state.up} | 👎{state.down}",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"👍 {state.up}", callback_data="up"),
                InlineKeyboardButton(f"👎 {state.down}", callback_data="down")
            ]
        ])
    )

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quand", quand))
    app.add_handler(CallbackQueryHandler(vote))
    app.run_polling()

if __name__ == "__main__":
    main()

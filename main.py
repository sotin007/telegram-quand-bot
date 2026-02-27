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

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "10"))  # поставил 10 сек, чтобы не мешало

last_used: Dict[Tuple[int, int], float] = {}

@dataclass
class VoteState:
    up: int = 0
    down: int = 0
    voters: Dict[int, int] = field(default_factory=dict)

votes: Dict[int, VoteState] = {}

def make_image(text: str, path: str) -> None:
    W, H = 512, 512
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 24
    draw.rounded_rectangle((pad, pad, W - pad, H - pad), radius=42, fill=(255, 255, 255, 240))

    text = (text or "").strip()
    if len(text) > 320:
        text = text[:320] + "…"
    wrapped = textwrap.fill(text, width=18)

    font = ImageFont.load_default()
    bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=8)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (W - tw) // 2
    y = (H - th) // 2

    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            draw.multiline_text((x + dx, y + dy), wrapped, font=font, fill=(0, 0, 0, 255), spacing=8)
    draw.multiline_text((x, y), wrapped, font=font, fill=(20, 20, 20, 255), spacing=8)

    img.save(path, "PNG")

def keyboard(poll_msg_id: int, up: int, down: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"👍 {up}", callback_data=f"v|{poll_msg_id}|up"),
            InlineKeyboardButton(f"👎 {down}", callback_data=f"v|{poll_msg_id}|down"),
        ],
        [InlineKeyboardButton("❌ Убрать голос", callback_data=f"v|{poll_msg_id}|clear")]
    ])

def extract_reply_text(m) -> str:
    # text OR caption, и убираем команды
    text = (m.text or m.caption or "").strip()
    if text.startswith("/"):
        return ""
    return text

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Сделай reply на сообщение с текстом и напиши /quand")

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    r = msg.reply_to_message
    if not r:
        await msg.reply_text("DEBUG: reply_to_message = None (бот не видит reply).")
        return
    t = (r.text or r.caption or "")
    await msg.reply_text(
        "DEBUG:\n"
        f"- reply есть ✅\n"
        f"- reply.text: {repr(r.text)}\n"
        f"- reply.caption: {repr(r.caption)}\n"
        f"- итоговый текст: {repr(extract_reply_text(r))}"
    )

async def quand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    # cooldown
    key = (chat.id, user.id)
    now = time.time()
    last = last_used.get(key, 0.0)
    if now - last < COOLDOWN_SECONDS:
        wait = int(COOLDOWN_SECONDS - (now - last))
        await msg.reply_text(f"Кулдаун 🙂 Подожди {wait} сек.")
        return
    last_used[key] = now

    if not msg.reply_to_message:
        await msg.reply_text("Нужно reply на сообщение (на текст), потом /quand.")
        return

    text = extract_reply_text(msg.reply_to_message)
    if not text:
        await msg.reply_text("В реплае не нашёл текст (возможно reply на команду/медиа без подписи).")
        return

    path = f"/tmp/quand_{chat.id}_{msg.message_id}.png"
    make_image(text, path)

    sent = await msg.reply_photo(photo=InputFile(path), caption="Кванд ✅")

    poll_msg = await context.bot.send_message(
        chat_id=chat.id,
        text="Голосование: 👍 0  |  👎 0",
        reply_to_message_id=sent.message_id,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("…", callback_data="noop")]]),
    )
    votes[poll_msg.message_id] = VoteState()
    await poll_msg.edit_reply_markup(reply_markup=keyboard(poll_msg.message_id, 0, 0))

async def on_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.data:
        return
    if q.data == "noop":
        await q.answer()
        return
    await q.answer(cache_time=1)

    parts = q.data.split("|")
    if len(parts) != 3 or parts[0] != "v":
        return

    poll_msg_id = int(parts[1])
    action = parts[2]
    state = votes.get(poll_msg_id)
    if not state:
        await q.answer("Голосовалка устарела (бот перезапускался).", show_alert=True)
        return

    user_id = q.from_user.id
    prev = state.voters.get(user_id, 0)

    def set_vote(new_vote: int):
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

    await q.message.edit_text(
        f"Голосование: 👍 {state.up}  |  👎 {state.down}",
        reply_markup=keyboard(poll_msg_id, state.up, state.down)
    )

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CommandHandler("quand", quand))
    app.add_handler(CallbackQueryHandler(on_vote))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

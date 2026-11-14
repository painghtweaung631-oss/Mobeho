import os
import json
import asyncio
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from telegram.error import RetryAfter, Forbidden, BadRequest

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
JSONBIN_ID = os.environ.get("JSONBIN_ID", "")
JSONBIN_KEY = os.environ.get("JSONBIN_KEY", "")
DATA_FILE = "texts.json"

active_tasks = {}

# ================= CLOUD + LOCAL DATA STORAGE =================
def load_texts():
    if JSONBIN_ID and JSONBIN_KEY:
        try:
            url = f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}/latest"
            headers = {"X-Master-Key": JSONBIN_KEY}
            res = requests.get(url, headers=headers, timeout=8)
            if res.status_code == 200:
                data = res.json().get("record", [])
                if isinstance(data, list) and data:
                    return data
        except Exception:
            pass

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return ["ဘာတွေလုပ်နေလဲ", "စားပြီးပြီလား"]

def save_texts(texts):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(texts, f, ensure_ascii=False, indent=2)

    if JSONBIN_ID and JSONBIN_KEY:
        try:
            url = f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}"
            headers = {
                "Content-Type": "application/json",
                "X-Master-Key": JSONBIN_KEY
            }
            requests.put(url, json=texts, headers=headers, timeout=8)
        except Exception:
            pass

# ================= OWNER CHECK =================
def is_owner(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == OWNER_ID

# ================= ESTIMATED CREATION YEAR =================
def estimate_creation_year(user_id: int) -> str:
    if user_id < 100000000:
        return "2013 - 2015 (ခန့်မှန်းခြေ)"
    elif user_id < 500000000:
        return "2016 - 2018 (ခန့်မှန်းခြေ)"
    elif user_id < 1200000000:
        return "2019 - 2020 (ခန့်မှန်းခြေ)"
    elif user_id < 2200000000:
        return "2021 (ခန့်မှန်းခြေ)"
    elif user_id < 5500000000:
        return "2022 - 2023 (ခန့်မှန်းခြေ)"
    elif user_id < 7200000000:
        return "2024 (ခန့်မှန်းခြေ)"
    else:
        return "2025 - 2026 (ခန့်မှန်းခြေ)"

# ================= COMMANDS =================
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    target = update.message.reply_to_message.from_user if update.message.reply_to_message else update.effective_user
    full_name = f"{target.first_name or ''} {target.last_name or ''}".strip()
    username = f"@{target.username}" if target.username else "မရှိပါ"
    est_year = estimate_creation_year(target.id)

    info = (
        f"👤 **User Info**\n"
        f"• **Name:** {full_name}\n"
        f"• **User ID:** `{target.id}`\n"
        f"• **Username:** {username}\n"
        f"• **Account Created:** {est_year}"
    )
    await update.message.reply_text(info, parse_mode=ParseMode.MARKDOWN)

async def cmd_addtext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    new_text = " ".join(context.args).strip()
    if not new_text:
        await update.message.reply_text("ထည့်ချင်တဲ့ စာသားပါ တစ်ခါတည်း ရိုက်ပါ။\nဥပမာ: `/addtext ဘာတွေလုပ်`", parse_mode=ParseMode.MARKDOWN)
        return

    texts = load_texts()
    texts.append(new_text)
    save_texts(texts)
    await update.message.reply_text(f"✅ ထည့်ပြီးပါပြီ (#{len(texts)}): {new_text}")

async def cmd_listtext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    texts = load_texts()
    if not texts:
        await update.message.reply_text("စာသားတစ်ခုမှ မရှိသေးပါဘူး။ `/addtext` နဲ့ ထည့်ပါ။", parse_mode=ParseMode.MARKDOWN)
        return

    lines = [f"{i}. {t}" for i, t in enumerate(texts, start=1)]
    await update.message.reply_text("📝 **သိမ်းထားသော စာသားများ:**\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_edittext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text("ပုံစံမှားနေပါတယ်။\nဥပမာ: `/edittext 1 စားပြီးပြီလား`", parse_mode=ParseMode.MARKDOWN)
        return

    idx = int(context.args[0]) - 1
    updated_text = " ".join(context.args[1:]).strip()
    texts = load_texts()

    if idx < 0 or idx >= len(texts):
        await update.message.reply_text(f"❌ နံပါတ် {idx + 1} မရှိပါဘူး။ `/listtext` နဲ့ ပြန်စစ်ပါ။", parse_mode=ParseMode.MARKDOWN)
        return

    texts[idx] = updated_text
    save_texts(texts)
    await update.message.reply_text(f"✏️ ပြင်ပြီးပါပြီ (#{idx + 1}): {updated_text}")

async def cmd_removetext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("ဖျက်ချင်တဲ့ နံပါတ်ထည့်ပါ။\nဥပမာ: `/removetext 1`", parse_mode=ParseMode.MARKDOWN)
        return

    idx = int(context.args[0]) - 1
    texts = load_texts()

    if idx < 0 or idx >= len(texts):
        await update.message.reply_text(f"❌ နံပါတ် {idx + 1} မရှိပါဘူး။", parse_mode=ParseMode.MARKDOWN)
        return

    removed = texts.pop(idx)
    save_texts(texts)
    await update.message.reply_text(f"🗑️ ဖျက်ပြီးပါပြီ: {removed}")

async def spam_loop(chat_id: int, target_id: int, target_name: str, context: ContextTypes.DEFAULT_TYPE):
    index = 0
    mention = f'<a href="tg://user?id={target_id}">{target_name}</a>'
    texts = load_texts()

    while True:
        if index % len(texts) == 0:
            texts = load_texts()
        if not texts:
            texts = ["ဟေ့"]

        msg_text = texts[index % len(texts)]
        index += 1

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{mention} {msg_text}",
                parse_mode=ParseMode.HTML
            )
            await asyncio.sleep(2.0)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (Forbidden, BadRequest):
            break
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(3.0)

async def cmd_selsen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    chat_id = update.effective_chat.id

    if not update.message.reply_to_message:
        await update.message.reply_text("စချင်တဲ့သူရဲ့ စာကို **Reply ပြန်ပြီး** `/selsen` လို့ ရိုက်ပါ။", parse_mode=ParseMode.MARKDOWN)
        return

    if chat_id in active_tasks and not active_tasks[chat_id].done():
        active_tasks[chat_id].cancel()

    target = update.message.reply_to_message.from_user
    target_name = target.first_name or "User"

    task = asyncio.create_task(spam_loop(chat_id, target.id, target_name, context))
    active_tasks[chat_id] = task

async def cmd_yat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    chat_id = update.effective_chat.id
    task = active_tasks.get(chat_id)

    if task and not task.done():
        task.cancel()
        active_tasks.pop(chat_id, None)

    await update.message.reply_text("Okay")

# ================= RENDER PORT BINDING =================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def log_message(self, format, *args):
        return

def start_web_server():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

# ================= MAIN =================
def main():
    threading.Thread(target=start_web_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["id", "Id"], cmd_id))
    app.add_handler(CommandHandler(["addtext", "Addtext"], cmd_addtext))
    app.add_handler(CommandHandler(["listtext", "Listtext"], cmd_listtext))
    app.add_handler(CommandHandler(["edittext", "Edittext"], cmd_edittext))
    app.add_handler(CommandHandler(["removetext", "Removetext"], cmd_removetext))
    app.add_handler(CommandHandler(["selsen", "Selsen"], cmd_selsen))
    app.add_handler(CommandHandler(["yat", "Yat"], cmd_yat))

    print("Mention Bot Started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

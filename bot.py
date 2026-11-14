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
DATA_FILE = "bot_data.json"

active_tasks = {}
sent_messages = {}  # {chat_id: [message_id, ...]}

DEFAULT_TEXTS = ["ဘာတွေလုပ်နေလဲ", "စားပြီးပြီလား"]

# ================= CLOUD + LOCAL DATA STORAGE =================
def load_data() -> dict:
    if JSONBIN_ID and JSONBIN_KEY:
        try:
            url = f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}/latest"
            headers = {"X-Master-Key": JSONBIN_KEY}
            res = requests.get(url, headers=headers, timeout=8)
            if res.status_code == 200:
                record = res.json().get("record", {})
                # ယခင် list format ဟောင်း ဖြစ်နေပါက dict format အသစ်သို့ အလိုအလျောက် ပြောင်းပေးမည်
                if isinstance(record, list):
                    data = {"texts": record, "permitted_users": []}
                    save_data(data)
                    return data
                if isinstance(record, dict):
                    return {
                        "texts": record.get("texts", []),
                        "permitted_users": record.get("permitted_users", [])
                    }
        except Exception:
            pass

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                record = json.load(f)
                if isinstance(record, list):
                    return {"texts": record, "permitted_users": []}
                if isinstance(record, dict):
                    return {
                        "texts": record.get("texts", []),
                        "permitted_users": record.get("permitted_users", [])
                    }
        except Exception:
            pass

    return {"texts": DEFAULT_TEXTS.copy(), "permitted_users": []}

def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    if JSONBIN_ID and JSONBIN_KEY:
        try:
            url = f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}"
            headers = {
                "Content-Type": "application/json",
                "X-Master-Key": JSONBIN_KEY
            }
            requests.put(url, json=data, headers=headers, timeout=8)
        except Exception:
            pass

def load_texts() -> list:
    return load_data().get("texts", [])

def save_texts(texts: list):
    data = load_data()
    data["texts"] = texts
    save_data(data)

def load_permitted() -> list:
    return load_data().get("permitted_users", [])

def save_permitted(users: list):
    data = load_data()
    data["permitted_users"] = users
    save_data(data)

# ================= MESSAGE TRACKING HELPER =================
def track_msg(chat_id: int, message_id: int):
    if chat_id not in sent_messages:
        sent_messages[chat_id] = []
    sent_messages[chat_id].append(message_id)
    # Memory မပြည့်စေရန် နောက်ဆုံး အစောင် ၁၀၀၀ အထိ မှတ်ထားမည်
    if len(sent_messages[chat_id]) > 1000:
        sent_messages[chat_id] = sent_messages[chat_id][-1000:]

async def send_tracked_reply(update: Update, text: str, parse_mode=None):
    msg = await update.message.reply_text(text, parse_mode=parse_mode)
    track_msg(update.effective_chat.id, msg.message_id)
    return msg

# ================= PERMISSION CHECK =================
def is_owner(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == OWNER_ID

def has_access(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    if user.id == OWNER_ID:
        return True
    return user.id in load_permitted()

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

# ================= PERMIT COMMANDS (OWNER ONLY) =================
async def cmd_apermit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    target_id = None
    if context.args and context.args[0].lstrip("-").isdigit():
        target_id = int(context.args[0])
    elif update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id

    if not target_id:
        await send_tracked_reply(update, "User ID ထည့်ပါ (သို့) User စာကို Reply ပြန်ပါ။\nဥပမာ: `/apermit 123456789`", parse_mode=ParseMode.MARKDOWN)
        return

    permitted = load_permitted()
    if target_id not in permitted:
        permitted.append(target_id)
        save_permitted(permitted)

    await send_tracked_reply(update, "done")

async def cmd_rmpermit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    target_id = None
    if context.args and context.args[0].lstrip("-").isdigit():
        target_id = int(context.args[0])
    elif update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id

    if not target_id:
        await send_tracked_reply(update, "User ID ထည့်ပါ (သို့) User စာကို Reply ပြန်ပါ။\nဥပမာ: `/rmpermit 123456789`", parse_mode=ParseMode.MARKDOWN)
        return

    permitted = load_permitted()
    if target_id in permitted:
        permitted.remove(target_id)
        save_permitted(permitted)

    await send_tracked_reply(update, "user access removed")

# ================= COMMANDS (OWNER + PERMITTED USERS) =================
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(update):
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
    await send_tracked_reply(update, info, parse_mode=ParseMode.MARKDOWN)

async def cmd_addtext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(update):
        return

    new_text = " ".join(context.args).strip()
    if not new_text:
        await send_tracked_reply(update, "ထည့်ချင်တဲ့ စာသားပါ တစ်ခါတည်း ရိုက်ပါ။\nဥပမာ: `/addtext ဘာတွေလုပ်`", parse_mode=ParseMode.MARKDOWN)
        return

    texts = load_texts()
    texts.append(new_text)
    save_texts(texts)
    await send_tracked_reply(update, f"✅ ထည့်ပြီးပါပြီ (#{len(texts)}): {new_text}")

async def cmd_listtext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(update):
        return

    texts = load_texts()
    if not texts:
        await send_tracked_reply(update, "စာသားတစ်ခုမှ မရှိသေးပါဘူး။ `/addtext` နဲ့ ထည့်ပါ။", parse_mode=ParseMode.MARKDOWN)
        return

    lines = [f"{i}. {t}" for i, t in enumerate(texts, start=1)]
    await send_tracked_reply(update, "📝 **သိမ်းထားသော စာသားများ:**\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_edittext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(update):
        return

    if len(context.args) < 2 or not context.args[0].isdigit():
        await send_tracked_reply(update, "ပုံစံမှားနေပါတယ်။\nဥပမာ: `/edittext 1 စားပြီးပြီလား`", parse_mode=ParseMode.MARKDOWN)
        return

    idx = int(context.args[0]) - 1
    updated_text = " ".join(context.args[1:]).strip()
    texts = load_texts()

    if idx < 0 or idx >= len(texts):
        await send_tracked_reply(update, f"❌ နံပါတ် {idx + 1} မရှိပါဘူး။ `/listtext` နဲ့ ပြန်စစ်ပါ။", parse_mode=ParseMode.MARKDOWN)
        return

    texts[idx] = updated_text
    save_texts(texts)
    await send_tracked_reply(update, f"✏️ ပြင်ပြီးပါပြီ (#{idx + 1}): {updated_text}")

async def cmd_removetext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(update):
        return

    if not context.args or not context.args[0].isdigit():
        await send_tracked_reply(update, "ဖျက်ချင်တဲ့ နံပါတ်ထည့်ပါ။\nဥပမာ: `/removetext 1`", parse_mode=ParseMode.MARKDOWN)
        return

    idx = int(context.args[0]) - 1
    texts = load_texts()

    if idx < 0 or idx >= len(texts):
        await send_tracked_reply(update, f"❌ နံပါတ် {idx + 1} မရှိပါဘူး။", parse_mode=ParseMode.MARKDOWN)
        return

    removed = texts.pop(idx)
    save_texts(texts)
    await send_tracked_reply(update, f"🗑️ ဖျက်ပြီးပါပြီ: {removed}")

async def cmd_removealltext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(update):
        return

    save_texts([])
    await send_tracked_reply(update, "🗑️ သိမ်းထားသော စာသားများအားလုံးကို ဖျက်ပြီးပါပြီ။")

async def cmd_deleownmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(update):
        return

    chat_id = update.effective_chat.id

    # လက်ရှိ Run နေတဲ့ spam_loop ရှိပါက အရင်ရပ်ပေးမည်
    task = active_tasks.get(chat_id)
    if task and not task.done():
        task.cancel()
        active_tasks.pop(chat_id, None)

    msg_ids = sent_messages.get(chat_id, []).copy()
    sent_messages[chat_id] = []

    # Bot ပို့ထားသော စာများကို ၁၀၀ စောင်တစ်တွဲစီ အမြန်ဆုံး ဖျက်မည်
    if msg_ids:
        for i in range(0, len(msg_ids), 100):
            chunk = msg_ids[i:i + 100]
            try:
                await context.bot.delete_messages(chat_id=chat_id, message_ids=chunk)
            except Exception:
                # delete_messages မရပါက တစ်စောင်ချင်းစီ လိုက်ဖျက်မည်
                for mid in chunk:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=mid)
                    except Exception:
                        pass

    # /deleownmsg ရိုက်လိုက်တဲ့ Command စာကိုပါ ရှင်းလင်းပေးမည် (Bot မှာ Delete Permission ရှိပါက)
    try:
        await update.message.delete()
    except Exception:
        pass

    confirm_msg = await context.bot.send_message(chat_id=chat_id, text="🧹 Bot ပို့ထားသော စာများအားလုံးကို ဖျက်ပြီးပါပြီ။")
    await asyncio.sleep(3.0)
    try:
        await confirm_msg.delete()
    except Exception:
        pass

async def spam_loop(chat_id: int, target_id: int, target_name: str, context: ContextTypes.DEFAULT_TYPE):
    index = 0
    mention = f'<a href="tg://user?id={target_id}">{target_name}</a>'
    texts = load_texts()

    while True:
        if not texts or index % len(texts) == 0:
            texts = load_texts()
        if not texts:
            texts = ["ဟေ့"]

        msg_text = texts[index % len(texts)]
        index += 1

        try:
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=f"{mention} {msg_text}",
                parse_mode=ParseMode.HTML
            )
            track_msg(chat_id, sent.message_id)
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
    if not has_access(update):
        return

    chat_id = update.effective_chat.id

    if not update.message.reply_to_message:
        await send_tracked_reply(update, "စချင်တဲ့သူရဲ့ စာကို **Reply ပြန်ပြီး** `/selsen` လို့ ရိုက်ပါ။", parse_mode=ParseMode.MARKDOWN)
        return

    if chat_id in active_tasks and not active_tasks[chat_id].done():
        active_tasks[chat_id].cancel()

    target = update.message.reply_to_message.from_user
    target_name = target.first_name or "User"

    task = asyncio.create_task(spam_loop(chat_id, target.id, target_name, context))
    active_tasks[chat_id] = task

async def cmd_yat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(update):
        return

    chat_id = update.effective_chat.id
    task = active_tasks.get(chat_id)

    if task and not task.done():
        task.cancel()
        active_tasks.pop(chat_id, None)

    await send_tracked_reply(update, "Okay")

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
    app.add_handler(CommandHandler(["apermit", "Apermit"], cmd_apermit))
    app.add_handler(CommandHandler(["rmpermit", "Rmpermit"], cmd_rmpermit))
    app.add_handler(CommandHandler(["id", "Id"], cmd_id))
    app.add_handler(CommandHandler(["addtext", "Addtext"], cmd_addtext))
    app.add_handler(CommandHandler(["listtext", "Listtext"], cmd_listtext))
    app.add_handler(CommandHandler(["edittext", "Edittext"], cmd_edittext))
    app.add_handler(CommandHandler(["removetext", "Removetext"], cmd_removetext))
    app.add_handler(CommandHandler(["removealltext", "Removealltext"], cmd_removealltext))
    app.add_handler(CommandHandler(["deleownmsg", "Deleownmsg"], cmd_deleownmsg))
    app.add_handler(CommandHandler(["selsen", "Selsen"], cmd_selsen))
    app.add_handler(CommandHandler(["yat", "Yat"], cmd_yat))

    print("Mention Bot Started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
        

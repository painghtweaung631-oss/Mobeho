import os
import re
import io
import json
import time
import math
import random
import asyncio
import logging
import html
import urllib.parse
from datetime import datetime, timezone, timedelta

import aiohttp
import feedparser
import qrcode
from PIL import Image, ImageDraw, ImageFont
from gtts import gTTS
from deep_translator import GoogleTranslator
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatType
from telegram.error import TelegramError
from aiohttp import web

# ==============================================================================
# CONFIGURATION & ENVIRONMENT VARIABLES
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
JSONBIN_API_KEY = os.getenv("JSONBIN_API_KEY", "")
JSONBIN_BIN_ID = os.getenv("JSONBIN_BIN_ID", "")
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("MobehoBot")

START_TIME = time.time()

# ==============================================================================
# IN-MEMORY STATE & JSONBIN CLOUD SYNC
# ==============================================================================
DEFAULT_DATA = {
    "permitted_users": [],
    "spam_texts": [
        "မင်္ဂလာပါ! ဘာတွေလုပ်နေကြလဲဗျို့ 👋",
        "စကားလေးဘာလေး ပြောကြပါဦးနော် 💬",
        "Mobeho Bot မှ နှုတ်ခွန်းဆက်သအပ်ပါတယ် 🚀",
    ],
    "quiz_scores": {},        # {user_id_str: {"name": str, "score": int}}
    "quiz_enabled_chats": [], # [chat_id_int]
    "chat_langs": {},         # {chat_id_str: "mm" | "en"}
    "notes": {},              # {user_id_str: {note_key: note_content}}
    "known_chats": [],        # [chat_id_int]
}

bot_data_store = json.loads(json.dumps(DEFAULT_DATA))
_save_lock = asyncio.Lock()
_dirty_flag = False

# Runtime volatile states
active_spam_tasks = {}      # {chat_id: asyncio.Task}
bot_sent_messages = {}      # {chat_id: [msg_id, ...]}
afk_users = {}              # {user_id: {"reason": str, "since": float}}
active_quizzes = {}         # {chat_id: {"answer": int, "answered": set(), "msg_id": int}}
news_cache = {}             # {cache_key: [{"title": str, "summary": str, "link": str, "source": str, "published": str}]}


async def load_from_jsonbin():
    global bot_data_store
    if not JSONBIN_API_KEY or not JSONBIN_BIN_ID:
        logger.warning("JSONBin credentials missing. Using in-memory storage only.")
        return
    url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest"
    headers = {"X-Master-Key": JSONBIN_API_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status == 200:
                    payload = await resp.json()
                    record = payload.get("record", {})
                    for k, v in DEFAULT_DATA.items():
                        bot_data_store[k] = record.get(k, v)
                    logger.info("Successfully loaded state from JSONBin.")
                else:
                    logger.error(f"JSONBin load error: HTTP {resp.status}")
    except Exception as e:
        logger.error(f"Failed to load from JSONBin: {e}")


async def save_to_jsonbin(force: bool = False):
    global _dirty_flag
    _dirty_flag = True
    if not JSONBIN_API_KEY or not JSONBIN_BIN_ID:
        return
    if not force:
        return
    async with _save_lock:
        url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
        headers = {
            "Content-Type": "application/json",
            "X-Master-Key": JSONBIN_API_KEY,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(url, json=bot_data_store, headers=headers, timeout=15) as resp:
                    if resp.status == 200:
                        _dirty_flag = False
                    else:
                        logger.error(f"JSONBin save error: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"Failed to save to JSONBin: {e}")


async def periodic_sync_worker():
    while True:
        await asyncio.sleep(20)
        if _dirty_flag:
            await save_to_jsonbin(force=True)


# ==============================================================================
# ACCESS CONTROL & LOCALIZATION HELPERS
# ==============================================================================
def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_permitted(user_id: int) -> bool:
    return is_owner(user_id) or (user_id in bot_data_store["permitted_users"])


def get_lang(chat) -> str:
    cid = str(chat.id)
    if cid in bot_data_store["chat_langs"]:
        return bot_data_store["chat_langs"][cid]
    if chat.type == ChatType.PRIVATE:
        return "en"
    return "mm"


def track_chat(chat_id: int):
    if chat_id not in bot_data_store["known_chats"]:
        bot_data_store["known_chats"].append(chat_id)
        asyncio.create_task(save_to_jsonbin(force=False))


def track_bot_msg(chat_id: int, message_id: int):
    bot_sent_messages.setdefault(chat_id, []).append(message_id)
    if len(bot_sent_messages[chat_id]) > 200:
        bot_sent_messages[chat_id] = bot_sent_messages[chat_id][-200:]


async def send_tracked(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs):
    msg = await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    track_bot_msg(chat_id, msg.message_id)
    return msg


async def reply_tracked(message, text: str, **kwargs):
    msg = await message.reply_text(text=text, **kwargs)
    track_bot_msg(message.chat_id, msg.message_id)
    return msg


# ==============================================================================
# BURMESE GENERAL KNOWLEDGE QUIZ BANK (100% MYANMAR LANGUAGE)
# ==============================================================================
MYANMAR_QUIZ_BANK = [
    {"q": "မြန်မာနိုင်ငံ၏ အမြင့်ဆုံးတောင်ထွဋ်မှာ အဘယ်နည်း?", "opts": ["ခါကာဘိုရာဇီတောင်", "ပုပ္ပားတောင်", "ဇွဲကပင်တောင်"], "ans": 0},
    {"q": "ပုဂံခေတ်ကို စတင်တည်ထောင်ခဲ့သော မင်းမှာ မည်သူနည်း?", "opts": ["ကျန်စစ်သားမင်း", "အနော်ရထာမင်း", "ဘုရင့်နောင်မင်း"], "ans": 1},
    {"q": "ကမ္ဘာပေါ်တွင် အကြီးဆုံး သမုဒ္ဒရာမှာ အဘယ်နည်း?", "opts": ["အတ္တလန္တိတ်သမုဒ္ဒရာ", "အိန္ဒိယသမုဒ္ဒရာ", "ပစိဖိတ်သမုဒ္ဒရာ"], "ans": 2},
    {"q": "မြန်မာနိုင်ငံ၏ အရှည်ဆုံးမြစ်မှာ မည်သည့်မြစ်နည်း?", "opts": ["သံလွင်မြစ်", "ဧရာဝတီမြစ်", "ချင်းတွင်းမြစ်"], "ans": 1},
    {"q": "နေအဖွဲ့အစည်းတွင် နေနှင့် အနီးဆုံးဂြိုဟ်မှာ အဘယ်နည်း?", "opts": ["ဗုဒ္ဓဟူးဂြိုဟ် (Mercury)", "သောကြာဂြိုဟ် (Venus)", "အင်္ဂါဂြိုဟ် (Mars)"], "ans": 0},
    {"q": "အာဆီယံ (ASEAN) အဖွဲ့ဝင် နိုင်ငံပေါင်း မည်မျှရှိသနည်း?", "opts": ["၉ နိုင်ငံ", "၁၀ နိုင်ငံ", "၁၁ နိုင်ငံ"], "ans": 1},
    {"q": "ရေ၏ ဓာတုသင်္ကေတမှာ အဘယ်နည်း?", "opts": ["CO2", "H2O", "O2"], "ans": 1},
    {"q": "မြန်မာနိုင်ငံ လွတ်လပ်ရေးရရှိခဲ့သော ခုနှစ်မှာ အဘယ်နည်း?", "opts": ["၁၉၄၇ ခုနှစ်", "၁၉၄၈ ခုနှစ်", "၁၉၅၀ ခုနှစ်"], "ans": 1},
    {"q": "လူ့ခန္ဓာကိုယ်တွင် အကြီးဆုံး အင်္ဂါအစိတ်အပိုင်းမှာ အဘယ်နည်း?", "opts": ["အသည်း", "ဦးနှောက်", "အရေပြား"], "ans": 2},
    {"q": "ကမ္ဘာ့အမြင့်ဆုံး ဧဝရတ်တောင်သည် မည်သည့်တောင်တန်းတွင် တည်ရှိသနည်း?", "opts": ["ဟိမဝန္တာတောင်တန်း", "အဲလ်ပ်တောင်တန်း", "အင်န်ဒီးစ်တောင်တန်း"], "ans": 0},
    {"q": "အလင်း၏ တစ်စက္ကန့်လျှင် သွားနှုန်းမှာ ခန့်မှန်းခြေ မည်မျှနည်း?", "opts": ["ကီလိုမီတာ ၃ သိန်း", "ကီလိုမီတာ ၃ သောင်း", "ကီလိုမီတာ ၃ ထောင်"], "ans": 0},
    {"q": "ကမ္ဘာ့ပထမဆုံး အာကာသယာဉ်မှူးမှာ မည်သူနည်း?", "opts": ["နေးလ် အမ်းစထရောင်း", "ယူရီ ဂါဂါရင်", "အယ်လွန် မတ်စ်"], "ans": 1},
    {"q": "အင်းလေးကန်သည် မြန်မာနိုင်ငံ၏ မည်သည့်ပြည်နယ်တွင် တည်ရှိသနည်း?", "opts": ["ကချင်ပြည်နယ်", "ရှမ်းပြည်နယ်", "ကယားပြည်နယ်"], "ans": 1},
    {"q": "ရွှေ၏ ဓာတုသင်္ကေတမှာ အဘယ်နည်း?", "opts": ["Ag", "Fe", "Au"], "ans": 2},
    {"q": "ကမ္ဘာပေါ်တွင် လူဦးရေ အများဆုံးနိုင်ငံမှာ လက်ရှိတွင် မည်သည့်နိုင်ငံနည်း?", "opts": ["အိန္ဒိယနိုင်ငံ", "တရုတ်နိုင်ငံ", "အမေရိကန်နိုင်ငံ"], "ans": 0},
    {"q": "မြန်မာသက္ကရာဇ် နှစ်သစ်ကူး သင်္ကြန်ပွဲတော်သည် မည်သည့်လတွင် ကျရောက်လေ့ရှိသနည်း?", "opts": ["မတ်လ", "ဧပြီလ", "မေလ"], "ans": 1},
    {"q": "ကွန်ပျူတာ၏ ဦးနှောက်ဟု ခေါ်ဆိုသော အစိတ်အပိုင်းမှာ အဘယ်နည်း?", "opts": ["RAM", "CPU", "Hard Disk"], "ans": 1},
    {"q": "ပင်လယ်ရေ ငန်ရခြင်း၏ အဓိကအကြောင်းရင်းမှာ မည်သည့်ဓာတ်ပေါင်း ပါဝင်သောကြောင့်နည်း?", "opts": ["ဆိုဒီယမ် ကလိုရိုက် (ဆား)", "ကယ်လ်စီယမ်", "ပိုတက်စီယမ်"], "ans": 0},
    {"q": "မြန်မာနိုင်ငံ၏ ပထမဆုံး ဝန်ကြီးချုပ်မှာ မည်သူနည်း?", "opts": ["ဗိုလ်ချုပ်အောင်ဆန်း", "ဦးနု", "ဦးဘဆွေ"], "ans": 1},
    {"q": "နေကြတ်ခြင်း ဖြစ်ပေါ်ရခြင်းမှာ အဘယ်ကြောင့်နည်း?", "opts": ["နေနှင့်ကမ္ဘာကြား လရောက်ရှိခြင်း", "နေနှင့်လကြား ကမ္ဘာရောက်ရှိခြင်း", "ဂြိုဟ်များတန်းစီခြင်း"], "ans": 0},
]


# ==============================================================================
# 1. OWNER-ONLY COMMANDS (4 COMMANDS)
# ==============================================================================
async def cmd_apermit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        try:
            await update.message.delete()
        except TelegramError:
            pass

    target_id = None
    target_name = "User"
    if update.message.reply_to_message:
        u = update.message.reply_to_message.from_user
        target_id = u.id
        target_name = u.full_name
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
        target_name = str(target_id)

    if not target_id:
        await send_tracked(context, update.effective_chat.id, "⚠️ Reply to a user or provide a valid User ID: `/apermit 123456789`", parse_mode=ParseMode.MARKDOWN)
        return

    if target_id not in bot_data_store["permitted_users"]:
        bot_data_store["permitted_users"].append(target_id)
        await save_to_jsonbin(force=True)
    await send_tracked(context, update.effective_chat.id, f"✅ **Permission Granted:** `{html.escape(target_name)}` (`{target_id}`)", parse_mode=ParseMode.MARKDOWN)


async def cmd_rmpermit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        try:
            await update.message.delete()
        except TelegramError:
            pass

    target_id = None
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])

    if not target_id:
        await send_tracked(context, update.effective_chat.id, "⚠️ Reply to a user or provide a User ID: `/rmpermit 123456789`", parse_mode=ParseMode.MARKDOWN)
        return

    if target_id in bot_data_store["permitted_users"]:
        bot_data_store["permitted_users"].remove(target_id)
        await save_to_jsonbin(force=True)
        await send_tracked(context, update.effective_chat.id, f"🗑 **Permission Revoked:** `{target_id}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await send_tracked(context, update.effective_chat.id, f"ℹ️ User `{target_id}` is not in the permitted list.", parse_mode=ParseMode.MARKDOWN)


async def cmd_listpermit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        try:
            await update.message.delete()
        except TelegramError:
            pass

    users = bot_data_store["permitted_users"]
    if not users:
        await send_tracked(context, update.effective_chat.id, "📋 **Permitted Users:** None", parse_mode=ParseMode.MARKDOWN)
        return
    lines = ["📋 **Permitted Users List:**"] + [f"• `{uid}`" for uid in users]
    await send_tracked(context, update.effective_chat.id, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        try:
            await update.message.delete()
        except TelegramError:
            pass

    text = " ".join(context.args) if context.args else ""
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    if not text:
        await send_tracked(context, update.effective_chat.id, "⚠️ Usage: `/broadcast <message>` or reply to a message.", parse_mode=ParseMode.MARKDOWN)
        return

    sent, failed = 0, 0
    for cid in list(bot_data_store["known_chats"]):
        try:
            await send_tracked(context, cid, f"📢 **Announcement**\n\n{text}", parse_mode=ParseMode.MARKDOWN)
            sent += 1
            await asyncio.sleep(0.1)
        except Exception:
            failed += 1
    await send_tracked(context, update.effective_chat.id, f"✅ Broadcast complete! Sent: `{sent}`, Failed: `{failed}`", parse_mode=ParseMode.MARKDOWN)


# ==============================================================================
# 2. OWNER + PERMITTED COMMANDS (13 COMMANDS)
# ==============================================================================
async def _spam_worker(context: ContextTypes.DEFAULT_TYPE, chat_id: int, target_user):
    mention = f"<a href='tg://user?id={target_user.id}'>{html.escape(target_user.first_name)}</a>"
    idx = 0
    try:
        while True:
            texts = bot_data_store["spam_texts"]
            if not texts:
                break
            line = html.escape(texts[idx % len(texts)])
            idx += 1
            await send_tracked(context, chat_id, f"{mention} {line}", parse_mode=ParseMode.HTML)
            await asyncio.sleep(2.2)
    except asyncio.CancelledError:
        pass


async def cmd_selsen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    lang = get_lang(update.effective_chat)

    if not update.message.reply_to_message:
        msg = "⚠️ Reply ထောက်ပြီး `/selsen` လို့ ရိုက်ပါ။" if lang == "mm" else "⚠️ Please reply to a user's message with `/selsen`."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    if not bot_data_store["spam_texts"]:
        msg = "⚠️ စာသားစာရင်း အလွတ်ဖြစ်နေပါတယ်။ `/addtext` နဲ့ အရင်ထည့်ပါ။" if lang == "mm" else "⚠️ Text list is empty. Use `/addtext` first."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    if chat_id in active_spam_tasks and not active_spam_tasks[chat_id].done():
        active_spam_tasks[chat_id].cancel()

    target_user = update.message.reply_to_message.from_user
    task = asyncio.create_task(_spam_worker(context, chat_id, target_user))
    active_spam_tasks[chat_id] = task


async def cmd_yat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    lang = get_lang(update.effective_chat)

    if chat_id in active_spam_tasks and not active_spam_tasks[chat_id].done():
        active_spam_tasks[chat_id].cancel()
        del active_spam_tasks[chat_id]
        msg = "🛑 ရပ်တန့်လိုက်ပါပြီ။" if lang == "mm" else "🛑 Stopped."
    else:
        msg = "ℹ️ လက်ရှိ ပို့နေသော စာမရှိပါ။" if lang == "mm" else "ℹ️ No active loop running."
    await reply_tracked(update.message, msg)


async def cmd_delmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    msg_ids = bot_sent_messages.get(chat_id, [])
    deleted = 0
    for mid in reversed(msg_ids):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            deleted += 1
        except TelegramError:
            pass
    bot_sent_messages[chat_id] = []
    try:
        await update.message.delete()
    except TelegramError:
        pass
    confirm = await context.bot.send_message(chat_id=chat_id, text=f"🧹 Deleted `{deleted}` bot messages.", parse_mode=ParseMode.MARKDOWN)
    await asyncio.sleep(3)
    try:
        await confirm.delete()
    except TelegramError:
        pass


async def cmd_addtext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    lang = get_lang(update.effective_chat)
    text = " ".join(context.args).strip()
    if not text:
        msg = "⚠️ ထည့်ချင်တဲ့ စာသား ရိုက်ထည့်ပါ: `/addtext မင်္ဂလာပါ`" if lang == "mm" else "⚠️ Usage: `/addtext Hello world`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return
    bot_data_store["spam_texts"].append(text)
    await save_to_jsonbin(force=True)
    msg = f"✅ စာသားအသစ် ထည့်ပြီးပါပြီ (စုစုပေါင်း: `{len(bot_data_store['spam_texts'])}` ကြောင်း)" if lang == "mm" else f"✅ Text added (Total: `{len(bot_data_store['spam_texts'])}`)"
    await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_listtext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    lang = get_lang(update.effective_chat)
    texts = bot_data_store["spam_texts"]
    if not texts:
        msg = "📭 သိမ်းထားသော စာသား မရှိပါ။" if lang == "mm" else "📭 No saved texts."
        await reply_tracked(update.message, msg)
        return
    header = "📜 **သိမ်းထားသော စာသားများ:**\n" if lang == "mm" else "📜 **Saved Text List:**\n"
    body = "\n".join([f"`{i+1}.` {html.escape(t)}" for i, t in enumerate(texts)])
    await reply_tracked(update.message, header + body, parse_mode=ParseMode.MARKDOWN)


async def cmd_edittext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    lang = get_lang(update.effective_chat)
    if len(context.args) < 2 or not context.args[0].isdigit():
        msg = "⚠️ အသုံးပြုပုံ: `/edittext <နံပါတ်> <စာသားအသစ်>`" if lang == "mm" else "⚠️ Usage: `/edittext <number> <new text>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return
    idx = int(context.args[0]) - 1
    new_text = " ".join(context.args[1:]).strip()
    if idx < 0 or idx >= len(bot_data_store["spam_texts"]):
        msg = "❌ နံပါတ် မှားယွင်းနေပါသည်။" if lang == "mm" else "❌ Invalid index number."
        await reply_tracked(update.message, msg)
        return
    bot_data_store["spam_texts"][idx] = new_text
    await save_to_jsonbin(force=True)
    msg = f"✏️ နံပါတ် `{idx+1}` ကို ပြင်ဆင်ပြီးပါပြီ။" if lang == "mm" else f"✏️ Updated item `{idx+1}`."
    await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_removetext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    lang = get_lang(update.effective_chat)
    if not context.args or not context.args[0].isdigit():
        msg = "⚠️ အသုံးပြုပုံ: `/removetext <နံပါတ်>`" if lang == "mm" else "⚠️ Usage: `/removetext <number>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return
    idx = int(context.args[0]) - 1
    if idx < 0 or idx >= len(bot_data_store["spam_texts"]):
        msg = "❌ နံပါတ် မှားယွင်းနေပါသည်။" if lang == "mm" else "❌ Invalid index number."
        await reply_tracked(update.message, msg)
        return
    removed = bot_data_store["spam_texts"].pop(idx)
    await save_to_jsonbin(force=True)
    msg = f"🗑 ဖျက်လိုက်သော စာသား: `{html.escape(removed)}`" if lang == "mm" else f"🗑 Removed: `{html.escape(removed)}`"
    await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_cleartext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    lang = get_lang(update.effective_chat)
    bot_data_store["spam_texts"] = []
    await save_to_jsonbin(force=True)
    msg = "🗑 စာသားအားလုံးကို ဖျက်သိမ်းပြီးပါပြီ။" if lang == "mm" else "🗑 All saved texts cleared."
    await reply_tracked(update.message, msg)


async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    lang = get_lang(update.effective_chat)
    arg = context.args[0].lower() if context.args else ""

    if arg == "on":
        if chat_id not in bot_data_store["quiz_enabled_chats"]:
            bot_data_store["quiz_enabled_chats"].append(chat_id)
            await save_to_jsonbin(force=True)
        msg = "✅ ၅ မိနစ်တစ်ခါ မြန်မာဗဟုသုတ Quiz စနစ်ကို ဖွင့်လိုက်ပါပြီ! (စတင်မေးခွန်း ပို့ပေးနေပါသည်...)" if lang == "mm" else "✅ 5-minute Myanmar Auto-Quiz enabled! Sending first question..."
        await reply_tracked(update.message, msg)
        await send_quiz_to_chat(context, chat_id)
    elif arg == "off":
        if chat_id in bot_data_store["quiz_enabled_chats"]:
            bot_data_store["quiz_enabled_chats"].remove(chat_id)
            await save_to_jsonbin(force=True)
        msg = "🛑 Auto-Quiz စနစ်ကို ပိတ်လိုက်ပါပြီ။" if lang == "mm" else "🛑 Auto-Quiz disabled for this chat."
        await reply_tracked(update.message, msg)
    else:
        status = "ON 🟢" if chat_id in bot_data_store["quiz_enabled_chats"] else "OFF 🔴"
        msg = f"🧠 Auto-Quiz အခြေအနေ: **{status}**\nဖွင့်ရန် `/quiz on`၊ ပိတ်ရန် `/quiz off` ရိုက်ပါ။" if lang == "mm" else f"🧠 Auto-Quiz Status: **{status}**\nUse `/quiz on` or `/quiz off`."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)


async def send_quiz_to_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    q_item = random.choice(MYANMAR_QUIZ_BANK)
    buttons = [
        [InlineKeyboardButton(text=f"{['က', 'ခ', 'ဂ'][i]}။ {opt}", callback_data=f"quiz_{chat_id}_{i}")]
        for i, opt in enumerate(q_item["opts"])
    ]
    markup = InlineKeyboardMarkup(buttons)
    text = (
        f"🧠 **မြန်မာဗဟုသုတ Quiz (အချိန် ၁ မိနစ်)**\n\n"
        f"❓ {q_item['q']}\n\n"
        f"👇 အောက်ပါ ခလုတ် ၃ ခုထဲမှ အဖြေမှန်ကို ရွေးချယ်ပါ:"
    )
    try:
        msg = await send_tracked(context, chat_id, text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)
        active_quizzes[chat_id] = {
            "q": q_item["q"],
            "opts": q_item["opts"],
            "ans": q_item["ans"],
            "answered": set(),
            "winners": [],
            "msg_id": msg.message_id,
        }
        asyncio.create_task(_reveal_quiz_after_delay(context, chat_id, msg.message_id, q_item))
    except Exception as e:
        logger.error(f"Quiz send failed in {chat_id}: {e}")


async def _reveal_quiz_after_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int, q_item: dict):
    await asyncio.sleep(60)
    quiz_state = active_quizzes.get(chat_id)
    if not quiz_state or quiz_state["msg_id"] != msg_id:
        return
    correct_label = f"{['က', 'ခ', 'ဂ'][q_item['ans']]}။ {q_item['opts'][q_item['ans']]}"
    winners_str = ", ".join(quiz_state["winners"]) if quiz_state["winners"] else "မည်သူမျှ အဖြေမှန် မဖြေနိုင်ပါ"
    result_text = (
        f"⏰ **Quiz အချိန်စေ့သွားပါပြီ!**\n\n"
        f"❓ မေးခွန်း: {q_item['q']}\n"
        f"✅ အဖြေမှန်: **{correct_label}**\n"
        f"🏆 အဖြေမှန်သူများ (+10 မှတ်): {winners_str}"
    )
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=result_text, parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        pass
    del active_quizzes[chat_id]


async def auto_quiz_scheduler(app: Application):
    while True:
        await asyncio.sleep(300)
        for chat_id in list(bot_data_store["quiz_enabled_chats"]):
            await send_quiz_to_chat(app, chat_id)


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    cid = str(update.effective_chat.id)
    arg = context.args[0].lower() if context.args else ""
    if arg in ("mm", "my", "myanmar", "burmese"):
        bot_data_store["chat_langs"][cid] = "mm"
        await save_to_jsonbin(force=True)
        await reply_tracked(update.message, "🇲🇲 Bot ဘာသာစကားကို **မြန်မာဘာသာ** သို့ ပြောင်းလဲလိုက်ပါပြီ။", parse_mode=ParseMode.MARKDOWN)
    elif arg in ("en", "eng", "english"):
        bot_data_store["chat_langs"][cid] = "en"
        await save_to_jsonbin(force=True)
        await reply_tracked(update.message, "🇬🇧 Bot language switched to **English**.", parse_mode=ParseMode.MARKDOWN)
    else:
        curr = get_lang(update.effective_chat).upper()
        await reply_tracked(update.message, f"🌐 Current Language: **{curr}**\nUsage: `/lang mm` or `/lang en`", parse_mode=ParseMode.MARKDOWN)


async def cmd_afk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    uid = update.effective_user.id
    lang = get_lang(update.effective_chat)
    reason = " ".join(context.args).strip() or ("အနားယူနေပါသည်" if lang == "mm" else "Away from keyboard")
    afk_users[uid] = {"reason": reason, "since": time.time(), "name": update.effective_user.first_name}
    msg = f"💤 **{html.escape(update.effective_user.first_name)}** သည် AFK ဖြစ်သွားပါပြီ။\n📝 အကြောင်းပြချက်: _{html.escape(reason)}_" if lang == "mm" else f"💤 **{html.escape(update.effective_user.first_name)}** is now AFK.\n📝 Reason: _{html.escape(reason)}_"
    await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    uid = str(update.effective_user.id)
    lang = get_lang(update.effective_chat)
    user_notes = bot_data_store["notes"].setdefault(uid, {})

    if not context.args:
        msg = "⚠️ အသုံးပြုပုံ:\n• သိမ်းရန်: `/note <ခေါင်းစဉ်> <စာသား>`\n• ကြည့်ရန်: `/note <ခေါင်းစဉ်>`\n• ဖျက်ရန်: `/note del <ခေါင်းစဉ်>`" if lang == "mm" else "⚠️ Usage:\n• Save: `/note <key> <text>`\n• Get: `/note <key>`\n• Delete: `/note del <key>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    if context.args[0].lower() == "del" and len(context.args) >= 2:
        key = context.args[1].lower()
        if key in user_notes:
            del user_notes[key]
            await save_to_jsonbin(force=True)
            msg = f"🗑 မှတ်စု `{key}` ကို ဖျက်ပြီးပါပြီ။" if lang == "mm" else f"🗑 Deleted note `{key}`."
        else:
            msg = f"❌ မှတ်စု `{key}` မတွေ့ပါ။" if lang == "mm" else f"❌ Note `{key}` not found."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    key = context.args[0].lower()
    if len(context.args) == 1:
        if key in user_notes:
            await reply_tracked(update.message, f"📌 **{key}:**\n{user_notes[key]}", parse_mode=ParseMode.MARKDOWN)
        else:
            msg = f"❌ မှတ်စု `{key}` မတွေ့ပါ။" if lang == "mm" else f"❌ Note `{key}` not found."
            await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    content = " ".join(context.args[1:])
    user_notes[key] = content
    await save_to_jsonbin(force=True)
    msg = f"✅ မှတ်စု `{key}` ကို သိမ်းဆည်းပြီးပါပြီ။" if lang == "mm" else f"✅ Saved note `{key}`."
    await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    uid = str(update.effective_user.id)
    lang = get_lang(update.effective_chat)
    user_notes = bot_data_store["notes"].get(uid, {})
    if not user_notes:
        msg = "📭 သိမ်းထားသော မှတ်စု မရှိသေးပါ။" if lang == "mm" else "📭 You have no saved notes."
        await reply_tracked(update.message, msg)
        return
    header = "📒 **သင်၏ ကိုယ်ပိုင်မှတ်စု စာရင်း:**\n" if lang == "mm" else "📒 **Your Saved Notes:**\n"
    items = "\n".join([f"• `{k}` (ခေါ်ကြည့်ရန် `/note {k}`)" for k in user_notes.keys()])
    await reply_tracked(update.message, header + items, parse_mode=ParseMode.MARKDOWN)


# ==============================================================================
# 3. EVERYONE COMMANDS (24 COMMANDS)
# ==============================================================================
def estimate_account_year(user_id: int) -> str:
    if user_id < 100_000_000:
        return "2013 - 2015"
    elif user_id < 500_000_000:
        return "2016 - 2017"
    elif user_id < 1_000_000_000:
        return "2018 - 2019"
    elif user_id < 2_000_000_000:
        return "2020 - 2021"
    elif user_id < 5_500_000_000:
        return "2022 - 2023"
    elif user_id < 7_200_000_000:
        return "2024 - 2025"
    else:
        return "2026+"


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = update.message.reply_to_message.from_user if update.message.reply_to_message else update.effective_user
    lang = get_lang(update.effective_chat)
    year_est = estimate_account_year(target.id)
    role = "👑 Owner" if is_owner(target.id) else ("🔐 Permitted" if is_permitted(target.id) else "👤 Member")
    if lang == "mm":
        text = (
            f"🪪 **အကောင့် အချက်အလက် စစ်ဆေးချက်**\n\n"
            f"• အမည်: **{html.escape(target.full_name)}**\n"
            f"• Username: @{target.username or 'မရှိပါ'}\n"
            f"• User ID: `{target.id}`\n"
            f"• အဆင့်: {role}\n"
            f"• အကောင့်ဖွင့်ခဲ့သည့် ခန့်မှန်းနှစ်: **{year_est}**"
        )
    else:
        text = (
            f"🪪 **User Account Check**\n\n"
            f"• Name: **{html.escape(target.full_name)}**\n"
            f"• Username: @{target.username or 'None'}\n"
            f"• User ID: `{target.id}`\n"
            f"• Role: {role}\n"
            f"• Est. Creation Year: **{year_est}**"
        )
    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)


async def cmd_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    now_utc = datetime.now(timezone.utc)
    sea_zones = [
        ("🇲🇲 Myanmar (MMT)", 6.5),
        ("🇹🇭 Thailand (ICT)", 7.0),
        ("🇱🇦 Laos (ICT)", 7.0),
        ("🇰🇭 Cambodia (ICT)", 7.0),
        ("🇻🇳 Vietnam (ICT)", 7.0),
        ("🇸🇬 Singapore (SGT)", 8.0),
        ("🇲🇾 Malaysia (MYT)", 8.0),
        ("🇧🇳 Brunei (BNT)", 8.0),
        ("🇵🇭 Philippines (PHT)", 8.0),
        ("🇮🇩 Indonesia - Jakarta (WIB)", 7.0),
        ("🇹🇱 Timor-Leste (TLT)", 9.0),
    ]
    header = "🕒 **အရှေ့တောင်အာရှ (၁၁) နိုင်ငံ လက်ရှိအချိန်များ**\n\n" if lang == "mm" else "🕒 **Southeast Asia (11 Countries) Live Time**\n\n"
    lines = []
    for name, offset in sea_zones:
        local_dt = now_utc + timedelta(hours=offset)
        lines.append(f"• {name}: `{local_dt.strftime('%I:%M:%S %p')} ({local_dt.strftime('%d %b')})`")
    await reply_tracked(update.message, header + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_gevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    events_mm = [
        "• **ဇန်နဝါရီ ၄** — မြန်မာ့လွတ်လပ်ရေးနေ့ 🇲🇲",
        "• **ဖေဖော်ဝါရီ ၁၂** — ပြည်ထောင်စုနေ့ 🇲🇲",
        "• **မတ် ၂** — တောင်သူလယ်သမားနေ့ 🌾",
        "• **ဧပြီ ၁၃-၁၆** — မြန်မာ့ရိုးရာ မဟာသင်္ကြန်ပွဲတော် 💦",
        "• **မေ ၁** — ကမ္ဘာ့အလုပ်သမားနေ့ 🛠",
        "• **ဇူလိုင် ၁၉** — အာဇာနည်နေ့ 🕯",
        "• **သီတင်းကျွတ်လပြည့်** — မီးထွန်းပွဲတော် 🏮",
        "• **တန်ဆောင်မုန်းလပြည့်** — တန်ဆောင်တိုင် မီးထွန်းပွဲတော် ✨",
        "• **ဒီဇင်ဘာ ၂၅** — ခရစ္စမတ်နေ့ 🎄",
    ]
    events_en = [
        "• **Jan 4** — Myanmar Independence Day 🇲🇲",
        "• **Feb 12** — Union Day 🇲🇲",
        "• **Mar 2** — Peasants' Day 🌾",
        "• **Apr 13-16** — Thingyan Water Festival 💦",
        "• **May 1** — International Workers' Day 🛠",
        "• **Jul 19** — Martyrs' Day 🕯",
        "• **Oct (Full Moon)** — Thadingyut Lighting Festival 🏮",
        "• **Nov (Full Moon)** — Tazaungdaing Festival ✨",
        "• **Dec 25** — Christmas Day 🎄",
    ]
    header = "📅 **အဓိက နေ့ထူးနေ့မြတ်များ ပြက္ခဒိန်**\n\n" if lang == "mm" else "📅 **Key Public Holidays & Observances**\n\n"
    await reply_tracked(update.message, header + "\n".join(events_mm if lang == "mm" else events_en), parse_mode=ParseMode.MARKDOWN)


async def cmd_qtoday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    quotes = [
        ("The only way to do great work is to love what you do.", "Steve Jobs", "ကြီးကျယ်တဲ့ အလုပ်တွေကို လုပ်နိုင်ဖို့ တစ်ခုတည်းသော နည်းလမ်းက ကိုယ်လုပ်နေတဲ့ အလုပ်ကို ချစ်မြတ်နိုးဖို့ပါပဲ။"),
        ("In the middle of difficulty lies opportunity.", "Albert Einstein", "အခက်အခဲတွေရဲ့ အလယ်ဗဟိုမှာ အခွင့်အလမ်းတွေ ရှိနေတတ်ပါတယ်။"),
        ("Success is not final, failure is not fatal: it is the courage to continue that counts.", "Winston Churchill", "အောင်မြင်မှုဆိုတာ အဆုံးသတ်မဟုတ်သလို ကျရှုံးမှုဆိုတာလည်း သေဆုံးခြင်းမဟုတ်ပါဘူး—ဆက်လျှောက်ရဲတဲ့ သတ္တိကသာ အဓိကပါ။"),
        ("Knowledge is power.", "Francis Bacon", "ဗဟုသုတသည် စွမ်းအားဖြစ်သည်။"),
        ("Stay hungry, stay foolish.", "Stewart Brand", "အမြဲတမ်း သင်ယူလိုစိတ် ပြင်းပြနေပါစေ။"),
        ("It always seems impossible until it's done.", "Nelson Mandela", "မပြီးမြောက်သေးခင်အထိတော့ အရာရာဟာ မဖြစ်နိုင်ဘူးလို့ ထင်ရစမြဲပါ။"),
    ]
    hour_idx = int(time.time() // 3600) % len(quotes)
    en_q, author, mm_q = quotes[hour_idx]
    if lang == "mm":
        text = f"💡 **ယခုတစ်နာရီအတွက် မှတ်သားဖွယ် စကားစု**\n\n_\"{mm_q}\"_\n_\"{en_q}\"_\n— **{author}**"
    else:
        text = f"💡 **Quote of the Hour**\n\n_\"{en_q}\"_\n— **{author}**"
    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)


async def cmd_topq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    scores = bot_data_store["quiz_scores"]
    if not scores:
        msg = "🏆 Quiz ရမှတ်စာရင်း မရှိသေးပါ။ `/quiz on` ဖွင့်ပြီး ဖြေဆိုနိုင်ပါပြီ!" if lang == "mm" else "🏆 No quiz scores yet! Enable `/quiz on` to play."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return
    sorted_users = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)[:10]
    header = "🏆 **မြန်မာဗဟုသုတ Quiz ထိပ်တန်းစာရင်း (Top 10)**\n\n" if lang == "mm" else "🏆 **Quiz Leaderboard (Top 10)**\n\n"
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, (uid, info) in enumerate(sorted_users):
        badge = medals[idx] if idx < 3 else f"`{idx+1}.`"
        lines.append(f"{badge} **{html.escape(info['name'])}** — `{info['score']} pts`")
    await reply_tracked(update.message, header + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# --- /football (3 Buttons: Clubs, National Teams, League Standings + Knockouts) ---
async def cmd_football(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏟 Clubs (UCL / UEL / Top Leagues)", callback_data="fb_clubs")],
        [InlineKeyboardButton("🌍 National Teams (FIFA / International)", callback_data="fb_national")],
        [InlineKeyboardButton("📊 League Standings & Knockout Live", callback_data="fb_standings")],
    ])
    text = "⚽ **Live ဘောလုံးပွဲစဉ်များနှင့် ရလဒ်များ**\nအောက်ပါ ခလုတ် ၃ ခုထဲမှ ကြည့်ရှုလိုရာကို ရွေးချယ်ပါ:" if lang == "mm" else "⚽ **Live Football Hub**\nSelect a category below:"
    await reply_tracked(update.message, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


async def fetch_espn_football(mode: str) -> str:
    endpoints = {
        "fb_clubs": [
            ("UEFA Champions League", "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard"),
            ("English Premier League", "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"),
            ("La Liga", "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard"),
        ],
        "fb_national": [
            ("FIFA World Cup / Qualifiers", "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"),
            ("International Friendlies", "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.friendly/scoreboard"),
            ("UEFA Nations League", "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.nations/scoreboard"),
        ],
        "fb_standings": [
            ("Champions League Knockout/Table", "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard"),
            ("Europa League Knockout/Table", "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.europa/scoreboard"),
            ("Premier League Matches", "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"),
        ],
    }
    output = []
    async with aiohttp.ClientSession() as session:
        for title, url in endpoints.get(mode, []):
            try:
                async with session.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    events = data.get("events", [])[:5]
                    if not events:
                        continue
                    output.append(f"🏆 **{title}**")
                    for ev in events:
                        comp = ev["competitions"][0]
                        c1, c2 = comp["competitors"][0], comp["competitors"][1]
                        t1, s1 = c1["team"]["shortDisplayName"], c1.get("score", "0")
                        t2, s2 = c2["team"]["shortDisplayName"], c2.get("score", "0")
                        status = ev["status"]["type"]["shortDetail"]
                        output.append(f"  • {t1} `{s1} - {s2}` {t2} ({status})")
                    output.append("")
            except Exception:
                continue
    if not output:
        return "⚽ လက်ရှိ Live ပွဲစဉ် အချက်အလက် မတွေ့ရှိသေးပါ (ခေတ္တစောင့်ပြီး ပြန်စစ်ကြည့်ပါ)။"
    return "\n".join(output)


# --- /news (Myanmar & World Live RSS Feeds + Full Article Reader) ---
RSS_FEEDS = {
    "mm": [
        ("BBC Burmese", "https://feeds.bbci.co.uk/burmese/rss.xml"),
        ("VOA Burmese", "https://burmese.voanews.com/api/z$qymteyqi"),
        ("RFA Burmese", "https://www.rfa.org/burmese/rss2.xml"),
    ],
    "world": [
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("NPR World", "https://feeds.npr.org/1004/rss.xml"),
    ],
}


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇲🇲 ပြည်တွင်းသတင်းများ (Myanmar)", callback_data="news_cat_mm"),
            InlineKeyboardButton("🌍 ပြည်ပသတင်းများ (World)", callback_data="news_cat_world"),
        ]
    ])
    text = "📰 **Live သတင်းဌာန (Daily News Hub)**\nဖတ်ရှုလိုသော သတင်းအမျိုးအစားကို ရွေးချယ်ပါ:" if lang == "mm" else "📰 **Live Daily News Hub**\nChoose a news category:"
    await reply_tracked(update.message, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


def _parse_rss_sync(category: str) -> list:
    articles = []
    for source_name, feed_url in RSS_FEEDS.get(category, []):
        try:
            parsed = feedparser.parse(feed_url)
            for entry in parsed.entries[:4]:
                raw_summary = entry.get("summary", entry.get("description", ""))
                clean_summary = re.sub(r"<[^>]+>", "", html.unescape(raw_summary)).strip()
                if not clean_summary:
                    clean_summary = entry.get("title", "")
                articles.append({
                    "source": source_name,
                    "title": html.unescape(entry.get("title", "No Title")).strip(),
                    "summary": clean_summary[:1800],
                    "link": entry.get("link", ""),
                    "published": entry.get("published", "Live"),
                })
        except Exception:
            continue
    return articles[:9]


# --- /rate (Myanmar Currency + Crypto TON/USDT/BTC) ---
async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["💱 **Live ငွေလဲနှုန်းနှင့် Crypto ပေါက်ဈေး**\n"]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://open.er-api.com/v6/latest/USD", timeout=10) as r1:
                fx = await r1.json()
                rates = fx.get("rates", {})
                mmk_ref = rates.get("MMK", 2100)
                thb = rates.get("THB", 34.5)
                sgd = rates.get("SGD", 1.34)
                jpy = rates.get("JPY", 153.0)
                lines.append("💵 **Fiat Exchange (1 USD Base):**")
                lines.append(f"• 🇲🇲 MMK (Ref): `{mmk_ref:,.2f} MMK`")
                lines.append(f"• 🇹🇭 THB: `{thb:,.2f} THB`")
                lines.append(f"• 🇸🇬 SGD: `{sgd:,.2f} SGD`")
                lines.append(f"• 🇯🇵 JPY: `{jpy:,.2f} JPY`\n")

            cg_url = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network,bitcoin,tether,ethereum&vs_currencies=usd,thb"
            async with session.get(cg_url, timeout=10) as r2:
                cg = await r2.json()
                lines.append("🪙 **Crypto Live Prices:**")
                if "the-open-network" in cg:
                    lines.append(f"• 💎 TON: `${cg['the-open-network']['usd']:,.2f}` (`{cg['the-open-network']['thb']:,.2f} THB`)")
                if "bitcoin" in cg:
                    lines.append(f"• ₿ BTC: `${cg['bitcoin']['usd']:,.0f}` (`{cg['bitcoin']['thb']:,.0f} THB`)")
                if "ethereum" in cg:
                    lines.append(f"• Ξ ETH: `${cg['ethereum']['usd']:,.2f}`")
                if "tether" in cg:
                    lines.append(f"• 💵 USDT: `${cg['tether']['usd']:,.3f}` (`{cg['tether']['thb']:,.2f} THB`)")
    except Exception as e:
        lines.append(f"⚠️ Live data fetch error: `{e}`")
    await reply_tracked(update.message, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# --- /gold (World Spot Gold Live) ---
async def cmd_gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=pax-gold&vs_currencies=usd,thb", timeout=10) as resp:
                data = await resp.json()
                oz_usd = data["pax-gold"]["usd"]
                oz_thb = data["pax-gold"]["thb"]
                kyatthar_usd = oz_usd * (16.606 / 31.1035)
                if lang == "mm":
                    text = (
                        f"🥇 **ကမ္ဘာ့ရွှေဈေးနှုန်း (Live Spot Gold)**\n\n"
                        f"• 1 Troy Ounce (31.1g): `${oz_usd:,.2f}` (`{oz_thb:,.0f} THB`)\n"
                        f"• ၁ ကျပ်သား ညီမျှကမ္ဘာ့ပေါက်ဈေး: `${kyatthar_usd:,.2f} USD`\n"
                        f"_(မှတ်ချက်- ပြည်တွင်းရွှေဈေးသည် ပြင်ပဒေါ်လာပေါက်ဈေးပေါ် မူတည်၍ အပြောင်းအလဲရှိနိုင်ပါသည်)_"
                    )
                else:
                    text = (
                        f"🥇 **Live Spot Gold Price**\n\n"
                        f"• 1 Troy Ounce (31.1g): `${oz_usd:,.2f}` (`{oz_thb:,.0f} THB`)\n"
                        f"• 1 Myanmar Kyatthar (16.6g) Eq: `${kyatthar_usd:,.2f} USD`"
                    )
                await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Gold price fetch failed: `{e}`", parse_mode=ParseMode.MARKDOWN)


# --- /weather (Open-Meteo Geocoding + Live Weather) ---
async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    city = " ".join(context.args).strip() if context.args else "Yangon"
    try:
        async with aiohttp.ClientSession() as session:
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=1&language=en&format=json"
            async with session.get(geo_url, timeout=10) as r1:
                gdata = await r1.json()
                if not gdata.get("results"):
                    msg = f"❌ `{html.escape(city)}` မြို့ကို ရှာမတွေ့ပါ။" if lang == "mm" else f"❌ City `{html.escape(city)}` not found."
                    await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
                    return
                loc = gdata["results"][0]
                lat, lon = loc["latitude"], loc["longitude"]
                full_name = f"{loc['name']}, {loc.get('country', '')}"

            w_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation&timezone=auto"
            async with session.get(w_url, timeout=10) as r2:
                wdata = await r2.json()
                curr = wdata["current"]
                if lang == "mm":
                    text = (
                        f"🌤 **မိုးလေဝသ အခြေအနေ — {html.escape(full_name)}**\n\n"
                        f"• 🌡 အပူချိန်: `{curr['temperature_2m']}°C`\n"
                        f"• 💧 စိုထိုင်းဆ: `{curr['relative_humidity_2m']}%`\n"
                        f"• 💨 လေတိုက်နှုန်း: `{curr['wind_speed_10m']} km/h`\n"
                        f"• 🌧 မိုးရေချိန်: `{curr['precipitation']} mm`"
                    )
                else:
                    text = (
                        f"🌤 **Live Weather — {html.escape(full_name)}**\n\n"
                        f"• 🌡 Temperature: `{curr['temperature_2m']}°C`\n"
                        f"• 💧 Humidity: `{curr['relative_humidity_2m']}%`\n"
                        f"• 💨 Wind Speed: `{curr['wind_speed_10m']} km/h`\n"
                        f"• 🌧 Precipitation: `{curr['precipitation']} mm`"
                    )
                await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Weather error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# --- /tr (Instant Translator) ---
async def cmd_tr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    target_lang = "my"
    text_to_tr = ""

    if update.message.reply_to_message:
        text_to_tr = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
        if context.args:
            target_lang = context.args[0].lower()
    elif context.args:
        if len(context.args) >= 2 and len(context.args[0]) <= 5:
            target_lang = context.args[0].lower()
            text_to_tr = " ".join(context.args[1:])
        else:
            text_to_tr = " ".join(context.args)

    if target_lang in ("mm", "burmese"):
        target_lang = "my"

    if not text_to_tr:
        msg = "⚠️ စာကို Reply ထောက်ပြီး `/tr mm` (သို့) `/tr en` ရိုက်ပါ။" if lang == "mm" else "⚠️ Reply to a message with `/tr en` or `/tr my`, or `/tr my <text>`."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    try:
        translated = await asyncio.to_thread(lambda: GoogleTranslator(source="auto", target=target_lang).translate(text_to_tr))
        await reply_tracked(update.message, f"🌐 **Translation ({target_lang.upper()}):**\n\n{translated}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Translation error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# --- /wiki (Wikipedia Summary) ---
async def cmd_wiki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    query = " ".join(context.args).strip()
    if not query:
        msg = "⚠️ ရှာချင်တဲ့ ခေါင်းစဉ် ရိုက်ထည့်ပါ: `/wiki Kalaw`" if lang == "mm" else "⚠️ Usage: `/wiki <topic>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    wiki_lang = "my" if any("\u1000" <= ch <= "\u109F" for ch in query) else "en"
    url = f"https://{wiki_lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(query)}"
    headers = {"User-Agent": "MobehoTelegramBot/2.0"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    msg = f"❌ `{html.escape(query)}` အတွက် Wikipedia အချက်အလက် မတွေ့ပါ။" if lang == "mm" else f"❌ No Wikipedia article found for `{html.escape(query)}`."
                    await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
                    return
                data = await resp.json()
                title = data.get("title", query)
                extract = data.get("extract", "No summary available.")
                page_url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
                await reply_tracked(
                    update.message,
                    f"📚 **{html.escape(title)}**\n\n{html.escape(extract)}\n\n🔗 [Read More]({page_url})",
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Wiki error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# --- /qr (QR Code Generator) ---
async def cmd_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    data = " ".join(context.args).strip()
    if not data and update.message.reply_to_message:
        data = update.message.reply_to_message.text or ""
    if not data:
        msg = "⚠️ QR ပြောင်းချင်တဲ့ Link သို့မဟုတ် စာသား ရိုက်ထည့်ပါ: `/qr https://t.me`" if lang == "mm" else "⚠️ Usage: `/qr <text or link>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    def _make_qr():
        img = qrcode.make(data)
        bio = io.BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        return bio

    bio = await asyncio.to_thread(_make_qr)
    sent = await update.message.reply_photo(photo=bio, caption=f"📱 QR Code for: `{html.escape(data[:60])}`", parse_mode=ParseMode.MARKDOWN)
    track_bot_msg(update.effective_chat.id, sent.message_id)


# --- /calc (Safe Calculator) ---
async def cmd_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    expr = "".join(context.args).strip()
    if not expr:
        msg = "⚠️ တွက်ချက်လိုသော ပုစ္ဆာရိုက်ပါ: `/calc (2500*4)+1500`" if lang == "mm" else "⚠️ Usage: `/calc (2500*4)+1500`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return
    if not re.match(r"^[0-9+\-*/().%^ ]+$", expr):
        msg = "❌ ဂဏန်းနှင့် သင်္ချာသင်္ကေတများသာ ရိုက်ထည့်ပါ။" if lang == "mm" else "❌ Invalid mathematical expression."
        await reply_tracked(update.message, msg)
        return
    try:
        safe_expr = expr.replace("^", "**")
        result = eval(safe_expr, {"__builtins__": None}, {})
        await reply_tracked(update.message, f"🧮 `{expr}` = **`{result:,}`**" if isinstance(result, (int, float)) else f"🧮 `{expr}` = **`{result}`**", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await reply_tracked(update.message, "❌ တွက်ချက်၍ မရပါ။ ပုစ္ဆာပုံစံ ပြန်စစ်ပါ။" if lang == "mm" else "❌ Could not evaluate expression.")


# --- /movie (TVMaze / Movie & Series Info) ---
async def cmd_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    query = " ".join(context.args).strip()
    if not query:
        msg = "⚠️ ဇာတ်ကား သို့မဟုတ် Series နာမည် ရိုက်ပါ: `/movie Ben 10`" if lang == "mm" else "⚠️ Usage: `/movie <title>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    url = f"https://api.tvmaze.com/singlesearch/shows?q={urllib.parse.quote(query)}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    msg = f"❌ `{html.escape(query)}` ကို ရှာမတွေ့ပါ။" if lang == "mm" else f"❌ Title `{html.escape(query)}` not found."
                    await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
                    return
                show = await resp.json()
                name = show.get("name", query)
                premiered = show.get("premiered", "N/A")
                rating = show.get("rating", {}).get("average") or "N/A"
                genres = ", ".join(show.get("genres", [])) or "General"
                summary = re.sub(r"<[^>]+>", "", show.get("summary") or "No description.")[:600]
                img_url = (show.get("image") or {}).get("medium")
                caption = (
                    f"🎬 **{html.escape(name)}**\n"
                    f"⭐ Rating: `{rating}/10` | 📅 Premiered: `{premiered}`\n"
                    f"🎭 Genre: `{genres}`\n\n"
                    f"📝 {html.escape(summary)}"
                )
                if img_url:
                    sent = await update.message.reply_photo(photo=img_url, caption=caption, parse_mode=ParseMode.MARKDOWN)
                    track_bot_msg(update.effective_chat.id, sent.message_id)
                else:
                    await reply_tracked(update.message, caption, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Movie lookup error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# --- /dict (English Definition + Myanmar Translation) ---
async def cmd_dict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    word = context.args[0].strip().lower() if context.args else ""
    if not word:
        msg = "⚠️ အင်္ဂလိပ်စကားလုံး ရိုက်ပါ: `/dict resilience`" if lang == "mm" else "⚠️ Usage: `/dict <word>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    await reply_tracked(update.message, f"❌ Word `{html.escape(word)}` not found.", parse_mode=ParseMode.MARKDOWN)
                    return
                data = await resp.json()
                entry = data[0]
                phonetic = entry.get("phonetic", "")
                meaning = entry["meanings"][0]
                pos = meaning.get("partOfSpeech", "")
                defn = meaning["definitions"][0]["definition"]
                mm_word = await asyncio.to_thread(lambda: GoogleTranslator(source="en", target="my").translate(word))
                text = (
                    f"📖 **{html.escape(word.capitalize())}** `{phonetic}`\n"
                    f"🇲🇲 မြန်မာအဓိပ္ပာယ်: **{html.escape(mm_word)}**\n"
                    f"🏷 Type: _{pos}_\n"
                    f"📝 Definition: {html.escape(defn)}"
                )
                await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Dictionary error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# --- /qimg (Quote Card Image Generator) ---
async def cmd_qimg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    if not update.message.reply_to_message:
        msg = "⚠️ Quote ပုံထုတ်ချင်တဲ့ စာသားကို Reply ထောက်ပြီး `/qimg` ရိုက်ပါ။" if lang == "mm" else "⚠️ Reply to a message with `/qimg` to generate a Quote Card."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    target_msg = update.message.reply_to_message
    author_name = target_msg.from_user.full_name
    quote_text = target_msg.text or target_msg.caption or "[Media]"

    def _render_quote_card():
        W, H = 800, 420
        img = Image.new("RGB", (W, H), color=(15, 23, 42))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([(24, 24), (W - 24, H - 24)], radius=24, fill=(30, 41, 59), outline=(0, 255, 255), width=3)
        font_lg = ImageFont.load_default()
        draw.text((55, 55), f"“ {author_name} ”", fill=(0, 255, 255), font=font_lg)

        words = quote_text.split()
        lines, curr = [], ""
        for w in words:
            if len(curr) + len(w) + 1 <= 68:
                curr = (curr + " " + w).strip()
            else:
                lines.append(curr)
                curr = w
        if curr:
            lines.append(curr)

        y = 115
        for line in lines[:10]:
            draw.text((55, y), line, fill=(248, 250, 252), font=font_lg)
            y += 24
        draw.text((55, H - 60), "Mobeho Bot • Quote Card", fill=(148, 163, 184), font=font_lg)
        bio = io.BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        return bio

    bio = await asyncio.to_thread(_render_quote_card)
    sent = await update.message.reply_photo(photo=bio)
    track_bot_msg(update.effective_chat.id, sent.message_id)


# --- /stats (Bot Dashboard & Stats) ---
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    uptime_sec = int(time.time() - START_TIME)
    hrs, rem = divmod(uptime_sec, 3600)
    mins, secs = divmod(rem, 60)
    top_player = "None"
    if bot_data_store["quiz_scores"]:
        best = max(bot_data_store["quiz_scores"].values(), key=lambda x: x["score"])
        top_player = f"{best['name']} ({best['score']} pts)"

    if lang == "mm":
        text = (
            f"📊 **Mobeho Bot စာရင်းချုပ် (Dashboard)**\n\n"
            f"• ⏱ Uptime: `{hrs}h {mins}m {secs}s`\n"
            f"• 📜 သိမ်းထားသော စာသား: `{len(bot_data_store['spam_texts'])}` ကြောင်း\n"
            f"• 🔐 Permit ပေးထားသူ: `{len(bot_data_store['permitted_users'])}` ဦး\n"
            f"• 🧠 Quiz ဖွင့်ထားသည့် Group: `{len(bot_data_store['quiz_enabled_chats'])}` ခု\n"
            f"• 🏆 Quiz ထိပ်ဆုံး: **{html.escape(top_player)}**\n"
            f"• 💬 ချိတ်ဆက်ထားသော Chat: `{len(bot_data_store['known_chats'])}` ခု"
        )
    else:
        text = (
            f"📊 **Mobeho Bot Dashboard**\n\n"
            f"• ⏱ Uptime: `{hrs}h {mins}m {secs}s`\n"
            f"• 📜 Saved Spam Texts: `{len(bot_data_store['spam_texts'])}`\n"
            f"• 🔐 Permitted Users: `{len(bot_data_store['permitted_users'])}`\n"
            f"• 🧠 Active Quiz Chats: `{len(bot_data_store['quiz_enabled_chats'])}`\n"
            f"• 🏆 Top Quiz Scorer: **{html.escape(top_player)}**\n"
            f"• 💬 Known Chats: `{len(bot_data_store['known_chats'])}`"
        )
    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)


# --- /ship (Matchmaking Love Percentage) ---
async def cmd_ship(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    u1 = update.effective_user.first_name
    if update.message.reply_to_message:
        u2 = update.message.reply_to_message.from_user.first_name
    elif context.args:
        u2 = " ".join(context.args)
    else:
        u2 = "Mystery Soulmate"

    score = random.randint(15, 100)
    bar = "❤️" * (score // 20) + "🤍" * (5 - (score // 20))
    if lang == "mm":
        text = f"💘 **အချစ်ရာခိုင်နှုန်း တိုင်းတာချက်**\n\n**{html.escape(u1)}** 💞 **{html.escape(u2)}**\nရလဒ်: **{score}%** {bar}"
    else:
        text = f"💘 **Matchmaking Result**\n\n**{html.escape(u1)}** 💞 **{html.escape(u2)}**\nCompatibility: **{score}%** {bar}"
    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)


# --- /tts (Text-to-Speech Voice Message) ---
async def cmd_tts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    text = " ".join(context.args).strip()
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""
    if not text:
        msg = "⚠️ အသံပြောင်းချင်တဲ့ စာသား ရိုက်ပါ: `/tts Hello everyone`" if lang == "mm" else "⚠️ Usage: `/tts <text>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    tts_lang = "th" if any("\u0E00" <= ch <= "\u0E7F" for ch in text) else "en"

    def _gen_tts():
        tts = gTTS(text=text[:400], lang=tts_lang)
        bio = io.BytesIO()
        tts.write_to_fp(bio)
        bio.seek(0)
        return bio

    try:
        bio = await asyncio.to_thread(_gen_tts)
        sent = await update.message.reply_voice(voice=bio)
        track_bot_msg(update.effective_chat.id, sent.message_id)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ TTS error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# --- /slang (Urban Dictionary Slang Lookup) ---
async def cmd_slang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    term = " ".join(context.args).strip()
    if not term:
        msg = "⚠️ ရှာချင်တဲ့ ဗန်းစကား ရိုက်ပါ: `/slang rizz`" if lang == "mm" else "⚠️ Usage: `/slang <term>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    url = f"https://api.urbandictionary.com/v0/define?term={urllib.parse.quote(term)}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                items = data.get("list", [])
                if not items:
                    await reply_tracked(update.message, f"❌ No slang definition found for `{html.escape(term)}`.", parse_mode=ParseMode.MARKDOWN)
                    return
                top = items[0]
                defn = top.get("definition", "").replace("[", "").replace("]", "")[:700]
                ex = top.get("example", "").replace("[", "").replace("]", "")[:300]
                text = f"🔥 **Slang: {html.escape(term)}**\n\n📖 {html.escape(defn)}\n\n💬 _Example:_ {html.escape(ex)}"
                await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Slang error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# --- /tldr (Extractive Smart Summarizer) ---
async def cmd_tldr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    raw = ""
    if update.message.reply_to_message:
        raw = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    elif context.args:
        raw = " ".join(context.args)

    if len(raw.strip()) < 60:
        msg = "⚠️ အနှစ်ချုပ်ချင်တဲ့ စာအရှည်ကို Reply ထောက်ပြီး `/tldr` ရိုက်ပါ။" if lang == "mm" else "⚠️ Reply to a long text/article with `/tldr` to summarize it."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    sentences = [s.strip() for s in re.split(r"(?<=[.!?။])\s+", raw.strip()) if len(s.strip()) > 15]
    if len(sentences) <= 3:
        summary_bullets = [f"• {s}" for s in sentences]
    else:
        picked = [sentences[0], sentences[len(sentences) // 2], sentences[-1]]
        summary_bullets = [f"• {s}" for s in picked]

    header = "⚡ **အဓိက အနှစ်ချုပ် (TL;DR):**\n\n" if lang == "mm" else "⚡ **Quick Summary (TL;DR):**\n\n"
    await reply_tracked(update.message, header + "\n".join(summary_bullets), parse_mode=ParseMode.MARKDOWN)


# --- /factcheck (Cross-Source News Verification) ---
async def cmd_factcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    claim = ""
    if update.message.reply_to_message:
        claim = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    elif context.args:
        claim = " ".join(context.args)

    if not claim:
        msg = "⚠️ စစ်ဆေးချင်တဲ့ သတင်းစာသားကို Reply ထောက်ပြီး `/factcheck` ရိုက်ပါ။" if lang == "mm" else "⚠️ Reply to a claim/news message with `/factcheck`."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    search_q = urllib.parse.quote(claim[:120])
    rss_url = f"https://news.google.com/rss/search?q={search_q}&hl=en-US&gl=US&ceid=US:en"
    parsed = await asyncio.to_thread(feedparser.parse, rss_url)
    matches = parsed.entries[:3]

    if matches:
        sources = "\n".join([f"• [{html.escape(m.title)}]({m.link})" for m in matches])
        if lang == "mm":
            text = f"🔍 **သတင်း အချက်အလက် တိုက်ဆိုင်စစ်ဆေးချက်**\n\n✅ နိုင်ငံတကာ သတင်းရင်းမြစ်များတွင် ဆက်စပ်သတင်း **{len(matches)} ပုဒ်** တွေ့ရှိရပါသည်:\n\n{sources}"
        else:
            text = f"🔍 **Fact-Check Cross-Reference**\n\n✅ Found **{len(matches)} matching news reports** from indexed sources:\n\n{sources}"
    else:
        if lang == "mm":
            text = "⚠️ **သတိပြုရန် (Unverified):**\nအဓိက နိုင်ငံတကာ သတင်းရင်းမြစ်များတွင် ဤအကြောင်းအရာနှင့် တိုက်ရိုက်ကိုက်ညီသော သတင်း မတွေ့ရှိရသေးပါ။ သတင်းတု/ကောလာဟလ ဖြစ်နိုင်သဖြင့် သေချာစိစစ်ပါ။"
        else:
            text = "⚠️ **Unverified Claim:**\nNo matching reports found across indexed global news outlets. Treat with caution."
    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


# --- /imagine (AI Text-to-Image Generator via Pollinations Free Endpoint) ---
async def cmd_imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    prompt = " ".join(context.args).strip()
    if not prompt:
        msg = "⚠️ AI ပုံထုတ်ချင်တဲ့ အကြောင်းအရာ ရိုက်ပါ: `/imagine cyber wolf neon rain`" if lang == "mm" else "⚠️ Usage: `/imagine <prompt>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    wait_msg = await reply_tracked(update.message, "🎨 AI ပုံဖန်တီးနေပါသည် ခဏစောင့်ပါ..." if lang == "mm" else "🎨 Generating AI artwork, please wait...")
    encoded = urllib.parse.quote(prompt)
    seed = random.randint(1, 999999)
    img_url = f"https://image.pollinations.ai/prompt/{encoded}?width=768&height=768&seed={seed}&nologo=true"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(img_url, timeout=35) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                img_bytes = await resp.read()
                bio = io.BytesIO(img_bytes)
                bio.seek(0)
                sent = await update.message.reply_photo(photo=bio, caption=f"🎨 **AI Image:** `{html.escape(prompt[:100])}`", parse_mode=ParseMode.MARKDOWN)
                track_bot_msg(update.effective_chat.id, sent.message_id)
                try:
                    await wait_msg.delete()
                except TelegramError:
                    pass
    except Exception as e:
        await wait_msg.edit_text(f"⚠️ AI Image generation failed: `{e}`", parse_mode=ParseMode.MARKDOWN)


# ==============================================================================
# 4. BOT DM ONLY COMMAND (/cmlist)
# ==============================================================================
async def cmd_cmlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    uid = update.effective_user.id
    lang = get_lang(update.effective_chat)

    sections = []
    if is_owner(uid):
        sections.append(
            "👑 **Owner Only Commands (4)**\n"
            "• `/apermit` — Grant user permission\n"
            "• `/rmpermit` — Revoke user permission\n"
            "• `/listpermit` — View all permitted users\n"
            "• `/broadcast` — Broadcast message to all chats"
        )

    if is_permitted(uid):
        sections.append(
            "🔐 **Permitted Commands (13)**\n"
            "• `/selsen` — Reply & mention loop spam\n"
            "• `/yat` — Stop active loop spam\n"
            "• `/delmsg` — Delete all bot messages in chat\n"
            "• `/addtext` — Add text to spam list\n"
            "• `/listtext` — View saved spam texts\n"
            "• `/edittext` — Edit a saved text by number\n"
            "• `/removetext` — Delete a saved text by number\n"
            "• `/cleartext` — Clear all saved spam texts\n"
            "• `/quiz on|off` — Toggle 5-min Myanmar Auto-Quiz\n"
            "• `/lang mm|en` — Switch bot language\n"
            "• `/afk [reason]` — Set AFK auto-reply status\n"
            "• `/note <key> [text]` — Save/get/del private note\n"
            "• `/notes` — List all your private notes"
        )

    sections.append(
        "🌍 **Public Utility & Info Commands (24)**\n"
        "• `/check` — Check User ID & est. account year\n"
        "• `/time` — Live time across 11 SEA countries\n"
        "• `/gevent` — Key holidays & observances\n"
        "• `/qtoday` — Quote of the hour\n"
        "• `/topq` — Myanmar Quiz Top 10 leaderboard\n"
        "• `/football` — Live Clubs, National & Standings\n"
        "• `/news` — Live Myanmar & World RSS news reader\n"
        "• `/rate` — Live USD/MMK/THB & Crypto rates\n"
        "• `/gold` — Live Spot Gold price\n"
        "• `/weather <city>` — Live city weather forecast\n"
        "• `/tr [lang] <text>` — Instant translator\n"
        "• `/wiki <topic>` — Wikipedia summary\n"
        "• `/qr <text/link>` — Generate QR code image\n"
        "• `/calc <expr>` — Quick calculator\n"
        "• `/movie <title>` — Movie & TV series info\n"
        "• `/dict <word>` — English-Myanmar dictionary\n"
        "• `/qimg` — Reply to create a Quote Card image\n"
        "• `/stats` — Bot uptime & live statistics\n"
        "• `/ship` — Matchmaking percentage\n"
        "• `/tts <text>` — Convert text to voice message\n"
        "• `/slang <term>` — Urban Dictionary slang lookup\n"
        "• `/tldr` — Summarize long text/article\n"
        "• `/factcheck` — Cross-check news claim\n"
        "• `/imagine <prompt>` — AI Text-to-Image generator"
    )

    header = "🤖 **Mobeho Bot — Available Command List**\n\n" if lang == "en" else "🤖 **Mobeho Bot — အသုံးပြုနိုင်သော Command စာရင်း**\n\n"
    await reply_tracked(update.message, header + "\n\n".join(sections), parse_mode=ParseMode.MARKDOWN)


# ==============================================================================
# 5. INLINE CALLBACK ROUTER (QUIZ, FOOTBALL, NEWS)
# ==============================================================================
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    chat_id = query.message.chat_id

    if data.startswith("quiz_"):
        parts = data.split("_")
        q_chat_id = int(parts[1])
        chosen_idx = int(parts[2])
        quiz_state = active_quizzes.get(q_chat_id)
        if not quiz_state or quiz_state["msg_id"] != query.message.message_id:
            await query.answer("⏰ ဤ Quiz မေးခွန်း အချိန်စေ့သွားပါပြီ!", show_alert=True)
            return
        if user.id in quiz_state["answered"]:
            await query.answer("⚠️ သင် ဖြေဆိုပြီးသား ဖြစ်ပါသည်!", show_alert=True)
            return

        quiz_state["answered"].add(user.id)
        if chosen_idx == quiz_state["ans"]:
            uid_str = str(user.id)
            rec = bot_data_store["quiz_scores"].setdefault(uid_str, {"name": user.full_name, "score": 0})
            rec["name"] = user.full_name
            rec["score"] += 10
            quiz_state["winners"].append(user.full_name)
            await save_to_jsonbin(force=False)
            await query.answer("🎉 မှန်ကန်ပါသည်! +10 မှတ် ရရှိပါပြီ!", show_alert=True)
        else:
            await query.answer("❌ မှားယွင်းနေပါသည်!", show_alert=True)
        return

    if data.startswith("fb_"):
        await query.answer("⚽ Live ရလဒ်များ ဆွဲယူနေပါသည်...")
        result_text = await fetch_espn_football(data)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🏟 Clubs", callback_data="fb_clubs"),
                InlineKeyboardButton("🌍 National", callback_data="fb_national"),
                InlineKeyboardButton("📊 Standings", callback_data="fb_standings"),
            ]
        ])
        try:
            await query.edit_message_text(text=result_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except TelegramError:
            pass
        return

    if data.startswith("news_cat_"):
        cat = data.replace("news_cat_", "")
        await query.answer("📰 Live သတင်းများ ဆွဲယူနေပါသည်...")
        articles = await asyncio.to_thread(_parse_rss_sync, cat)
        if not articles:
            await query.edit_message_text("⚠️ လက်ရှိ သတင်းများ ဆွဲယူ၍ မရသေးပါ။ ခဏနေ ပြန်စမ်းကြည့်ပါ။")
            return
        cache_key = f"{chat_id}_{cat}"
        news_cache[cache_key] = articles

        header = "🇲🇲 **ပြည်တွင်း Live သတင်းခေါင်းစဉ်များ**\nဖတ်လိုသော သတင်းနံပါတ်ကို နှိပ်ပါ:\n\n" if cat == "mm" else "🌍 **ပြည်ပ Live သတင်းခေါင်းစဉ်များ**\nဖတ်လိုသော သတင်းနံပါတ်ကို နှိပ်ပါ:\n\n"
        lines = []
        btn_row = []
        btn_rows = []
        for idx, art in enumerate(articles):
            lines.append(f"**{idx+1}.** [{art['source']}] {html.escape(art['title'])}")
            btn_row.append(InlineKeyboardButton(f"📄 {idx+1}", callback_data=f"news_read_{cat}_{idx}"))
            if len(btn_row) == 3:
                btn_rows.append(btn_row)
                btn_row = []
        if btn_row:
            btn_rows.append(btn_row)
        btn_rows.append([InlineKeyboardButton("🔄 Switch Category", callback_data="news_back_main")])

        await query.edit_message_text(
            text=header + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(btn_rows),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if data.startswith("news_read_"):
        _, _, cat, idx_str = data.split("_")
        idx = int(idx_str)
        cache_key = f"{chat_id}_{cat}"
        articles = news_cache.get(cache_key, [])
        if idx >= len(articles):
            await query.answer("⚠️ သတင်း သက်တမ်းကုန်သွားပါပြီ။ /news ပြန်ခေါ်ပါ။", show_alert=True)
            return
        art = articles[idx]
        await query.answer()
        article_text = (
            f"📰 **{html.escape(art['title'])}**\n"
            f"📡 Source: `{art['source']}` | 🕒 `{art['published']}`\n\n"
            f"{html.escape(art['summary'])}\n\n"
            f"🔗 [မူရင်းသတင်း အပြည့်အစုံဖတ်ရန်]({art['link']})"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ သတင်းစာရင်းသို့ ပြန်သွားရန်", callback_data=f"news_cat_{cat}")]])
        await query.edit_message_text(text=article_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return

    if data == "news_back_main":
        await query.answer()
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🇲🇲 ပြည်တွင်းသတင်းများ (Myanmar)", callback_data="news_cat_mm"),
                InlineKeyboardButton("🌍 ပြည်ပသတင်းများ (World)", callback_data="news_cat_world"),
            ]
        ])
        await query.edit_message_text("📰 **Live သတင်းဌာန (Daily News Hub)**\nဖတ်ရှုလိုသော သတင်းအမျိုးအစားကို ရွေးချယ်ပါ:", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


# ==============================================================================
# 6. GENERAL MESSAGE LISTENER (AFK & #NOTE SHORTCUT)
# ==============================================================================
async def general_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    chat = update.effective_chat
    user = update.effective_user
    track_chat(chat.id)
    lang = get_lang(chat)

    text = update.message.text or ""
    if user.id in afk_users and not text.startswith("/afk"):
        del afk_users[user.id]
        msg = f"👋 **{html.escape(user.first_name)}** ပြန်လည်ရောက်ရှိလာပါပြီ (AFK ဖြုတ်ပြီး)။" if lang == "mm" else f"👋 **{html.escape(user.first_name)}** is back (AFK removed)."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        replied_uid = update.message.reply_to_message.from_user.id
        if replied_uid in afk_users and replied_uid != user.id:
            info = afk_users[replied_uid]
            mins = max(1, int((time.time() - info["since"]) // 60))
            msg = (
                f"💤 **{html.escape(info['name'])}** သည် လက်ရှိ AFK ဖြစ်နေပါသည် ({mins} မိနစ်ကြာ)\n"
                f"📝 အကြောင်းပြချက်: _{html.escape(info['reason'])}_"
            ) if lang == "mm" else (
                f"💤 **{html.escape(info['name'])}** is currently AFK ({mins}m ago)\n"
                f"📝 Reason: _{html.escape(info['reason'])}_"
            )
            await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)

    if text.startswith("#") and len(text) > 1 and is_permitted(user.id):
        key = text[1:].split()[0].lower()
        user_notes = bot_data_store["notes"].get(str(user.id), {})
        if key in user_notes:
            await reply_tracked(update.message, f"📌 **{key}:**\n{user_notes[key]}", parse_mode=ParseMode.MARKDOWN)


# ==============================================================================
# 7. SCOPED COMMAND MENU SETUP (HIDDEN IN GROUPS, VISIBLE ONLY IN BOT DM)
# ==============================================================================
async def post_init_setup(app: Application):
    await start_health_server()
    await load_from_jsonbin()

    await app.bot.set_my_commands([], scope=BotCommandScopeAllGroupChats())

    public_dm_commands = [
        BotCommand("cmlist", "View available commands (Bot DM only)"),
        BotCommand("check", "Check User ID & est. account year"),
        BotCommand("time", "Live time across 11 SEA countries"),
        BotCommand("gevent", "Key holidays & observances"),
        BotCommand("qtoday", "Quote of the hour"),
        BotCommand("topq", "Myanmar Quiz Top 10 leaderboard"),
        BotCommand("football", "Live Clubs, National & Standings"),
        BotCommand("news", "Live Myanmar & World RSS news"),
        BotCommand("rate", "Live currency & crypto rates"),
        BotCommand("gold", "Live Spot Gold price"),
        BotCommand("weather", "Live city weather forecast"),
        BotCommand("tr", "Instant translator"),
        BotCommand("wiki", "Wikipedia summary"),
        BotCommand("qr", "Generate QR code image"),
        BotCommand("calc", "Quick calculator"),
        BotCommand("movie", "Movie & TV series info"),
        BotCommand("dict", "English-Myanmar dictionary"),
        BotCommand("qimg", "Create Quote Card image"),
        BotCommand("stats", "Bot uptime & live stats"),
        BotCommand("ship", "Matchmaking percentage"),
        BotCommand("tts", "Text to voice message"),
        BotCommand("slang", "Urban Dictionary slang lookup"),
        BotCommand("tldr", "Summarize long text/article"),
        BotCommand("factcheck", "Cross-check news claim"),
        BotCommand("imagine", "AI Text-to-Image generator"),
    ]
    await app.bot.set_my_commands(public_dm_commands, scope=BotCommandScopeAllPrivateChats())

    if OWNER_ID:
        owner_dm_commands = [
            BotCommand("apermit", "[Owner] Grant user permission"),
            BotCommand("rmpermit", "[Owner] Revoke user permission"),
            BotCommand("listpermit", "[Owner] List permitted users"),
            BotCommand("broadcast", "[Owner] Broadcast to all chats"),
            BotCommand("selsen", "[Permit] Reply & mention loop"),
            BotCommand("yat", "[Permit] Stop active loop"),
            BotCommand("delmsg", "[Permit] Delete bot messages"),
            BotCommand("addtext", "[Permit] Add spam text"),
            BotCommand("listtext", "[Permit] List spam texts"),
            BotCommand("edittext", "[Permit] Edit spam text"),
            BotCommand("removetext", "[Permit] Remove spam text"),
            BotCommand("cleartext", "[Permit] Clear all spam texts"),
            BotCommand("quiz", "[Permit] Toggle 5-min Myanmar Quiz"),
            BotCommand("lang", "[Permit] Switch language (mm/en)"),
            BotCommand("afk", "[Permit] Set AFK status"),
            BotCommand("note", "[Permit] Save/get/del private note"),
            BotCommand("notes", "[Permit] List private notes"),
        ] + public_dm_commands
        try:
            await app.bot.set_my_commands(owner_dm_commands, scope=BotCommandScopeChat(chat_id=OWNER_ID))
        except TelegramError as e:
            logger.warning(f"Could not set Owner DM menu yet: {e}")

    asyncio.create_task(periodic_sync_worker())
    asyncio.create_task(auto_quiz_scheduler(app))
    logger.info("Mobeho Bot initialized with scoped menus and background tasks.")


# ==============================================================================
# 8. RENDER WEB HEALTH-CHECK SERVER & MAIN RUNNER
# ==============================================================================
async def start_health_server():
    app_web = web.Application()
    app_web.router.add_get("/", lambda request: web.Response(text="Mobeho Bot is running!"))
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health server listening on port {PORT}")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing!")

    application = Application.builder().token(BOT_TOKEN).post_init(post_init_setup).build()

    # Owner Only (4)
    application.add_handler(CommandHandler("apermit", cmd_apermit))
    application.add_handler(CommandHandler("rmpermit", cmd_rmpermit))
    application.add_handler(CommandHandler("listpermit", cmd_listpermit))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # Owner + Permitted (13 + legacy aliases)
    application.add_handler(CommandHandler("selsen", cmd_selsen))
    application.add_handler(CommandHandler("yat", cmd_yat))
    application.add_handler(CommandHandler(["delmsg", "deleownmsg"], cmd_delmsg))
    application.add_handler(CommandHandler("addtext", cmd_addtext))
    application.add_handler(CommandHandler("listtext", cmd_listtext))
    application.add_handler(CommandHandler("edittext", cmd_edittext))
    application.add_handler(CommandHandler("removetext", cmd_removetext))
    application.add_handler(CommandHandler(["cleartext", "removealltext"], cmd_cleartext))
    application.add_handler(CommandHandler("quiz", cmd_quiz))
    application.add_handler(CommandHandler("lang", cmd_lang))
    application.add_handler(CommandHandler("afk", cmd_afk))
    application.add_handler(CommandHandler("note", cmd_note))
    application.add_handler(CommandHandler("notes", cmd_notes))

    # Everyone (24 + legacy aliases)
    application.add_handler(CommandHandler(["check", "id"], cmd_check))
    application.add_handler(CommandHandler("time", cmd_time))
    application.add_handler(CommandHandler("gevent", cmd_gevent))
    application.add_handler(CommandHandler("qtoday", cmd_qtoday))
    application.add_handler(CommandHandler("topq", cmd_topq))
    application.add_handler(CommandHandler("football", cmd_football))
    application.add_handler(CommandHandler(["news", "dailynews"], cmd_news))
    application.add_handler(CommandHandler(["rate", "currency"], cmd_rate))
    application.add_handler(CommandHandler("gold", cmd_gold))
    application.add_handler(CommandHandler("weather", cmd_weather))
    application.add_handler(CommandHandler("tr", cmd_tr))
    application.add_handler(CommandHandler("wiki", cmd_wiki))
    application.add_handler(CommandHandler("qr", cmd_qr))
    application.add_handler(CommandHandler("calc", cmd_calc))
    application.add_handler(CommandHandler("movie", cmd_movie))
    application.add_handler(CommandHandler("dict", cmd_dict))
    application.add_handler(CommandHandler(["qimg", "quoteimg"], cmd_qimg))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("ship", cmd_ship))
    application.add_handler(CommandHandler("tts", cmd_tts))
    application.add_handler(CommandHandler(["slang", "define_slang"], cmd_slang))
    application.add_handler(CommandHandler("tldr", cmd_tldr))
    application.add_handler(CommandHandler("factcheck", cmd_factcheck))
    application.add_handler(CommandHandler("imagine", cmd_imagine))

    # Bot DM Only (1)
    application.add_handler(CommandHandler("cmlist", cmd_cmlist))

    # Inline Buttons & General Messages
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, general_message_handler))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

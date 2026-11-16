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
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

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
afk_users = {}              # {user_id: {"reason": str, "since": float, "name": str}}
active_quizzes = {}         # {chat_id: {"q": str, "opts": list, "ans": int, "answered": set, "winners": list, "msg_id": int}}
news_cache = {}             # {cache_key: [{"title": str, "summary": str, "link": str, "source": str, "published": str, "image": str}]}


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
# ACCESS CONTROL, TEXT & LOCALIZATION HELPERS
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


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def smart_truncate(text: str, max_len: int = 550) -> str:
    text = clean_text(text)
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    for sep in (". ", "။ ", "! ", "? "):
        idx = cut.rfind(sep)
        if idx > int(max_len * 0.45):
            return cut[: idx + 1].strip()
    space_idx = cut.rfind(" ")
    if space_idx > int(max_len * 0.5):
        return cut[:space_idx].strip() + "..."
    return cut.strip() + "..."


async def send_tracked(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs):
    msg = await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    track_bot_msg(chat_id, msg.message_id)
    return msg


async def reply_tracked(message, text: str, **kwargs):
    msg = await message.reply_text(text=text, **kwargs)
    track_bot_msg(message.chat_id, msg.message_id)
    return msg


# ==============================================================================
# ASYNC GOOGLE TRANSLATE + MYMEMORY FALLBACK ENGINE
# ==============================================================================
async def async_translate(text: str, target: str = "my") -> str:
    if not text.strip():
        return ""
    target = "my" if target in ("mm", "burmese", "myanmar") else target
    snippet = text[:2500]

    gtx_url = (
        f"https://translate.googleapis.com/translate_a/single"
        f"?client=gtx&sl=auto&tl={urllib.parse.quote(target)}&dt=t&q={urllib.parse.quote(snippet)}"
    )
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(gtx_url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and data[0]:
                        translated = "".join(seg[0] for seg in data[0] if seg and seg[0])
                        if translated:
                            return translated
    except Exception as e:
        logger.warning(f"GTX Translate fallback triggered: {e}")

    mm_url = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(snippet[:500])}&langpair=en|{urllib.parse.quote(target)}"
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(mm_url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    res = data.get("responseData", {}).get("translatedText", "")
                    if res:
                        return html.unescape(res)
    except Exception as e:
        logger.error(f"MyMemory Translate failed: {e}")

    raise RuntimeError("Translation services temporarily busy.")


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
# 1. OWNER-ONLY COMMANDS (13 COMMANDS: 4 ADMIN + 9 EXCLUSIVE AI/PRO SUITE)
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
    await send_tracked(context, update.effective_chat.id, f"✅ *Permission Granted:* `{target_name}` (`{target_id}`)", parse_mode=ParseMode.MARKDOWN)


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
        await send_tracked(context, update.effective_chat.id, f"🗑 *Permission Revoked:* `{target_id}`", parse_mode=ParseMode.MARKDOWN)
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
        await send_tracked(context, update.effective_chat.id, "📋 *Permitted Users:* None", parse_mode=ParseMode.MARKDOWN)
        return
    lines = ["📋 *Permitted Users List:*"] + [f"• `{uid}`" for uid in users]
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
            await send_tracked(context, cid, f"📢 *Announcement*\n\n{text}", parse_mode=ParseMode.MARKDOWN)
            sent += 1
            await asyncio.sleep(0.1)
        except Exception:
            failed += 1
    await send_tracked(context, update.effective_chat.id, f"✅ Broadcast complete! Sent: `{sent}`, Failed: `{failed}`", parse_mode=ParseMode.MARKDOWN)


# --- OWNER EXCLUSIVE AI & PRO COMMANDS (9 COMMANDS) ---

# 1. /imagine (Owner Exclusive HD AI Text-to-Image)
async def cmd_imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    lang = get_lang(update.effective_chat)
    raw_prompt = " ".join(context.args).strip()
    if not raw_prompt:
        msg = "⚠️ AI ပုံထုတ်ချင်တဲ့ အကြောင်းအရာ ရိုက်ပါ: `/imagine Ronaldo`" if lang == "mm" else "⚠️ Usage: `/imagine <prompt>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    wait_msg = await reply_tracked(update.message, "🎨 HD AI ပုံဖန်တီးနေပါသည် ခဏစောင့်ပါ..." if lang == "mm" else "🎨 Generating HD AI artwork, please wait...")

    try:
        if any("\u1000" <= ch <= "\u109F" for ch in raw_prompt):
            base_prompt = await async_translate(raw_prompt, target="en")
        else:
            base_prompt = raw_prompt
    except Exception:
        base_prompt = raw_prompt

    enhanced_prompt = f"{base_prompt}, ultra detailed, cinematic lighting, 8k resolution, sharp focus, masterpiece, photorealistic"
    encoded = urllib.parse.quote(enhanced_prompt)
    seed = random.randint(1000, 9999999)

    endpoints = [
        f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&model=flux&seed={seed}&nologo=true&enhance=false",
        f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&model=turbo&seed={seed}&nologo=true",
        f"https://image.pollinations.ai/prompt/{urllib.parse.quote(base_prompt)}?width=800&height=800&seed={seed}&nologo=true",
    ]

    img_bytes = None
    last_err = "All endpoints busy"
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        for url in endpoints:
            try:
                async with session.get(url, timeout=35) as resp:
                    if resp.status == 200:
                        ctype = resp.headers.get("Content-Type", "")
                        data = await resp.read()
                        if "image" in ctype and len(data) > 5000:
                            img_bytes = data
                            break
                    else:
                        last_err = f"HTTP {resp.status}"
            except Exception as e:
                last_err = str(e)
            await asyncio.sleep(0.5)

    if img_bytes:
        bio = io.BytesIO(img_bytes)
        bio.seek(0)
        sent = await update.message.reply_photo(photo=bio, caption=f"🎨 *AI HD Image:* `{raw_prompt[:100]}`", parse_mode=ParseMode.MARKDOWN)
        track_bot_msg(update.effective_chat.id, sent.message_id)
        try:
            await wait_msg.delete()
        except TelegramError:
            pass
    else:
        await wait_msg.edit_text(f"⚠️ AI Image server busy ({last_err}). ခဏနေ ပြန်စမ်းကြည့်ပါ။")


# 2. /tr (Owner Exclusive Instant Global Translator)
async def cmd_tr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
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

    if target_lang in ("mm", "burmese", "myanmar"):
        target_lang = "my"

    if not text_to_tr.strip():
        msg = "⚠️ စာကို Reply ထောက်ပြီး `/tr mm` (သို့) `/tr en` ရိုက်ပါ။" if lang == "mm" else "⚠️ Reply to a message with `/tr en` or `/tr my`, or `/tr my <text>`."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    try:
        translated = await async_translate(text_to_tr, target=target_lang)
        await reply_tracked(update.message, f"🌐 *Translation ({target_lang.upper()}):*\n\n{translated}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Translation error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# 3. /tts (Owner Exclusive Text-to-Speech)
async def cmd_tts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
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


# 4. /tldr (Owner Exclusive Smart Summarizer)
async def cmd_tldr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    lang = get_lang(update.effective_chat)
    raw = ""
    if update.message.reply_to_message:
        raw = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    elif context.args:
        raw = " ".join(context.args)

    raw = clean_text(raw)
    if len(raw) < 50:
        msg = "⚠️ အနှစ်ချုပ်ချင်တဲ့ စာအရှည်ကို Reply ထောက်ပြီး `/tldr` ရိုက်ပါ။" if lang == "mm" else "⚠️ Reply to a long text/article with `/tldr` to summarize it."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    sentences = [s.strip() for s in re.split(r"(?<=[.!?။])\s+", raw) if len(s.strip()) > 12]
    if len(sentences) <= 3:
        summary_bullets = [f"• {s}" for s in sentences]
    else:
        picked = [sentences[0], sentences[len(sentences) // 2], sentences[-1]]
        summary_bullets = [f"• {s}" for s in picked]

    header = "⚡ *အဓိက အနှစ်ချုပ် (TL;DR):*\n\n" if lang == "mm" else "⚡ *Quick Summary (TL;DR):*\n\n"
    await reply_tracked(update.message, header + "\n".join(summary_bullets), parse_mode=ParseMode.MARKDOWN)


# 5. /factcheck (Owner Exclusive News Cross-Check)
async def cmd_factcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    lang = get_lang(update.effective_chat)
    claim = ""
    if update.message.reply_to_message:
        claim = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    elif context.args:
        claim = " ".join(context.args)

    claim = clean_text(claim)
    if not claim:
        msg = "⚠️ စစ်ဆေးချင်တဲ့ သတင်းစာသားကို Reply ထောက်ပြီး `/factcheck` ရိုက်ပါ။" if lang == "mm" else "⚠️ Reply to a claim/news message with `/factcheck`."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    query_phrase = " ".join(claim.split()[:10])
    is_burmese = any("\u1000" <= ch <= "\u109F" for ch in query_phrase)
    if is_burmese:
        rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query_phrase)}&hl=my&gl=MM&ceid=MM:my"
    else:
        rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query_phrase)}&hl=en-US&gl=US&ceid=US:en"

    parsed = await asyncio.to_thread(feedparser.parse, rss_url)
    matches = parsed.entries[:3]

    if matches:
        sources = "\n".join([f"• [{clean_text(m.title)}]({m.link})" for m in matches])
        if lang == "mm":
            text = f"🔍 *သတင်း အချက်အလက် တိုက်ဆိုင်စစ်ဆေးချက်*\n\n✅ သတင်းရင်းမြစ်များတွင် ဆက်စပ်သတင်း *{len(matches)} ပုဒ်* တွေ့ရှိရပါသည်:\n\n{sources}"
        else:
            text = f"🔍 *Fact-Check Cross-Reference*\n\n✅ Found *{len(matches)} matching news reports* from indexed sources:\n\n{sources}"
    else:
        if lang == "mm":
            text = "⚠️ *သတိပြုရန် (Unverified):*\nသတင်းရင်းမြစ်များတွင် ဤအကြောင်းအရာနှင့် တိုက်ရိုက်ကိုက်ညီသော သတင်း မတွေ့ရှိရသေးပါ။ သတင်းတု/ကောလာဟလ ဖြစ်နိုင်သဖြင့် သေချာစိစစ်ပါ။"
        else:
            text = "⚠️ *Unverified Claim:*\nNo matching reports found across indexed news outlets. Treat with caution."
    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


# 6. /qimg (Owner Exclusive HD Quote Card Generator)
async def cmd_qimg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    lang = get_lang(update.effective_chat)
    if not update.message.reply_to_message:
        msg = "⚠️ Quote ပုံထုတ်ချင်တဲ့ စာသားကို Reply ထောက်ပြီး `/qimg` ရိုက်ပါ။" if lang == "mm" else "⚠️ Reply to a message with `/qimg` to generate a Quote Card."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    target_msg = update.message.reply_to_message
    author_name = target_msg.from_user.full_name
    quote_text = clean_text(target_msg.text or target_msg.caption or "[Media]")

    def _render_quote_card():
        W, H = 1000, 560
        img = Image.new("RGB", (W, H), color=(11, 17, 32))
        draw = ImageDraw.Draw(img)
        for y in range(H):
            c = int(11 + (y / H) * 18)
            draw.line([(0, y), (W, y)], fill=(c, c + 6, c + 24))

        draw.rounded_rectangle([(32, 32), (W - 32, H - 32)], radius=28, fill=(22, 32, 54), outline=(0, 240, 255), width=3)
        draw.rectangle([(64, 68), (74, 128)], fill=(0, 240, 255))

        try:
            font_author = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
            font_body = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 26)
            font_footer = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
        except Exception:
            font_author = font_body = font_footer = ImageFont.load_default()

        draw.text((92, 74), author_name, fill=(0, 240, 255), font=font_author)

        words = quote_text.split()
        lines, curr = [], ""
        for w in words:
            if len(curr) + len(w) + 1 <= 54:
                curr = (curr + " " + w).strip()
            else:
                lines.append(curr)
                curr = w
        if curr:
            lines.append(curr)

        y = 165
        for line in lines[:8]:
            draw.text((68, y), line, fill=(241, 245, 249), font=font_body)
            y += 38

        draw.text((68, H - 72), "MobehoX Studio • Quote Card", fill=(148, 163, 184), font=font_footer)
        bio = io.BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        return bio

    bio = await asyncio.to_thread(_render_quote_card)
    sent = await update.message.reply_photo(photo=bio)
    track_bot_msg(update.effective_chat.id, sent.message_id)


# 7. /wiki (Owner Exclusive Wikipedia Research + Photo)
async def cmd_wiki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    lang = get_lang(update.effective_chat)
    query = " ".join(context.args).strip()
    if not query:
        msg = "⚠️ ရှာချင်တဲ့ ခေါင်းစဉ် ရိုက်ထည့်ပါ: `/wiki Myanmar`" if lang == "mm" else "⚠️ Usage: `/wiki <topic>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    wiki_lang = "my" if any("\u1000" <= ch <= "\u109F" for ch in query) else "en"
    headers = {"User-Agent": "MobehoBot/3.0 (https://t.me)"}

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            search_url = f"https://{wiki_lang}.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query)}&format=json&srlimit=1"
            async with session.get(search_url, timeout=10) as sresp:
                sdata = await sresp.json()
                hits = sdata.get("query", {}).get("search", [])
                if not hits:
                    msg = f"❌ `{query}` အတွက် Wikipedia အချက်အလက် မတွေ့ပါ။" if lang == "mm" else f"❌ No Wikipedia results found for `{query}`."
                    await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
                    return
                exact_title = hits[0]["title"]

            sum_url = f"https://{wiki_lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(exact_title.replace(' ', '_'))}"
            async with session.get(sum_url, timeout=10) as resp:
                data = await resp.json()
                title = data.get("title", exact_title)
                extract = smart_truncate(data.get("extract", "No summary available."), 750)
                page_url = data.get("content_urls", {}).get("desktop", {}).get("page", f"https://{wiki_lang}.wikipedia.org")
                thumb_url = (data.get("originalimage") or data.get("thumbnail") or {}).get("source")

                caption = f"📚 *{title}*\n\n{extract}\n\n🔗 [Wikipedia အပြည့်အစုံဖတ်ရန်]({page_url})"
                if thumb_url:
                    try:
                        sent = await update.message.reply_photo(photo=thumb_url, caption=caption, parse_mode=ParseMode.MARKDOWN)
                        track_bot_msg(update.effective_chat.id, sent.message_id)
                        return
                    except TelegramError:
                        pass
                await reply_tracked(update.message, caption, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Wiki error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# 8. /movie (Owner Exclusive Global Movie & Series HD Poster Search)
async def cmd_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    lang = get_lang(update.effective_chat)
    query = " ".join(context.args).strip()
    if not query:
        msg = "⚠️ ဇာတ်ကား သို့မဟုတ် Series နာမည် ရိုက်ပါ: `/movie Hulk 2003`" if lang == "mm" else "⚠️ Usage: `/movie <title>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        itunes_url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&media=movie&limit=1"
        try:
            async with session.get(itunes_url, timeout=10) as resp:
                if resp.status == 200:
                    text_body = await resp.text()
                    data = json.loads(text_body)
                    results = data.get("results", [])
                    if results:
                        m = results[0]
                        title = m.get("trackName", query)
                        director = m.get("artistName", "N/A")
                        release = (m.get("releaseDate") or "")[:10] or "N/A"
                        genre = m.get("primaryGenreName", "Movie")
                        advisory = m.get("contentAdvisoryRating", "NR")
                        raw_desc = m.get("longDescription") or m.get("shortDescription") or "No description available."
                        desc = smart_truncate(raw_desc, 620)
                        poster = (m.get("artworkUrl100") or "").replace("100x100bb", "1000x1000bb")

                        caption = (
                            f"🎬 *{title}*\n"
                            f"📅 Release: `{release}` | 🏷 Rated: `{advisory}`\n"
                            f"🎭 Genre: `{genre}` | 🎥 Director: `{director}`\n\n"
                            f"📝 {desc}"
                        )
                        if poster:
                            sent = await update.message.reply_photo(photo=poster, caption=caption, parse_mode=ParseMode.MARKDOWN)
                            track_bot_msg(update.effective_chat.id, sent.message_id)
                            return
                        await reply_tracked(update.message, caption, parse_mode=ParseMode.MARKDOWN)
                        return
        except Exception:
            pass

        clean_q = re.sub(r"\b(19|20)\d{2}\b", "", query).strip()
        tv_url = f"https://api.tvmaze.com/singlesearch/shows?q={urllib.parse.quote(clean_q)}"
        try:
            async with session.get(tv_url, timeout=10) as resp:
                if resp.status == 200:
                    show = await resp.json()
                    name = show.get("name", query)
                    premiered = show.get("premiered", "N/A")
                    rating = show.get("rating", {}).get("average") or "N/A"
                    genres = ", ".join(show.get("genres", [])) or "Series"
                    summary = smart_truncate(show.get("summary") or "No description.", 620)
                    img_url = (show.get("image") or {}).get("original") or (show.get("image") or {}).get("medium")
                    caption = (
                        f"📺 *{name}*\n"
                        f"⭐ Rating: `{rating}/10` | 📅 Premiered: `{premiered}`\n"
                        f"🎭 Genre: `{genres}`\n\n"
                        f"📝 {summary}"
                    )
                    if img_url:
                        sent = await update.message.reply_photo(photo=img_url, caption=caption, parse_mode=ParseMode.MARKDOWN)
                        track_bot_msg(update.effective_chat.id, sent.message_id)
                        return
                    await reply_tracked(update.message, caption, parse_mode=ParseMode.MARKDOWN)
                    return
        except Exception:
            pass

    msg = f"❌ `{query}` ဇာတ်ကား/Series ကို ရှာမတွေ့ပါ။" if lang == "mm" else f"❌ Movie/Series `{query}` not found."
    await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)


# 9. /news (Owner Exclusive Live News Hub + Banner & Article Posters)
RSS_FEEDS = {
    "mm": [
        ("BBC Burmese", "https://feeds.bbci.co.uk/burmese/rss.xml"),
        ("RFA Burmese", "https://www.rfa.org/burmese/rss2.xml"),
        ("Google News MM", "https://news.google.com/rss?hl=my&gl=MM&ceid=MM:my"),
    ],
    "world": [
        ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("NPR World", "https://feeds.npr.org/1004/rss.xml"),
    ],
}


def _generate_news_banner(title_text: str, subtitle_text: str, accent_rgb=(0, 229, 255)) -> io.BytesIO:
    W, H = 900, 340
    img = Image.new("RGB", (W, H), color=(10, 15, 28))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        r = int(10 + (y / H) * 12)
        g = int(15 + (y / H) * 20)
        b = int(28 + (y / H) * 35)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    draw.rounded_rectangle([(20, 20), (W - 20, H - 20)], radius=20, outline=accent_rgb, width=4)
    draw.rectangle([(48, 52), (62, 145)], fill=accent_rgb)

    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 44)
        font_sub = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
        font_badge = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
    except Exception:
        font_title = font_sub = font_badge = ImageFont.load_default()

    draw.text((82, 54), title_text, fill=(255, 255, 255), font=font_title)
    draw.text((82, 112), subtitle_text, fill=(148, 163, 184), font=font_sub)

    now_str = datetime.now(timezone(timedelta(hours=6, minutes=30))).strftime("%d %b %Y • %I:%M %p MMT")
    draw.text((52, H - 68), f"LIVE FEED  |  {now_str}", fill=accent_rgb, font=font_badge)

    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio


def _extract_entry_image(entry) -> str:
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        return entry.media_thumbnail[0].get("url", "")
    if hasattr(entry, "media_content") and entry.media_content:
        return entry.media_content[0].get("url", "")
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            if "image" in enc.get("type", ""):
                return enc.get("href", "")
    raw_html = entry.get("summary", "") + entry.get("description", "")
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', raw_html)
    if m:
        return m.group(1)
    return ""


def _parse_rss_sync(category: str) -> list:
    articles = []
    for source_name, feed_url in RSS_FEEDS.get(category, []):
        try:
            parsed = feedparser.parse(feed_url)
            for entry in parsed.entries[:4]:
                title = clean_text(entry.get("title", ""))
                summary = clean_text(entry.get("summary", entry.get("description", "")))
                if not summary or len(summary) < 15:
                    summary = title
                img_url = _extract_entry_image(entry)
                articles.append({
                    "source": source_name,
                    "title": title,
                    "summary": smart_truncate(summary, 950),
                    "link": entry.get("link", ""),
                    "published": entry.get("published", "Live")[:25],
                    "image": img_url,
                })
        except Exception:
            continue
    return articles[:8]


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    lang = get_lang(update.effective_chat)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇲🇲 ပြည်တွင်းသတင်း (Myanmar)", callback_data="news_cat_mm"),
            InlineKeyboardButton("🌍 ပြည်ပသတင်း (World)", callback_data="news_cat_world"),
        ]
    ])
    banner = await asyncio.to_thread(_generate_news_banner, "MOBEHO NEWS HUB", "Real-Time Myanmar & Global Headlines", (0, 255, 255))
    caption = (
        "📰 *Live သတင်းဌာန (Daily News Hub)*\n"
        "ဖတ်ရှုလိုသော သတင်းအမျိုးအစားကို အောက်ပါခလုတ်မှ ရွေးချယ်ပါ:"
        if lang == "mm" else
        "📰 *Live Daily News Hub*\nSelect a news category below:"
    )
    sent = await update.message.reply_photo(photo=banner, caption=caption, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    track_bot_msg(update.effective_chat.id, sent.message_id)


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
    header = "📜 *သိမ်းထားသော စာသားများ:*\n" if lang == "mm" else "📜 *Saved Text List:*\n"
    body = "\n".join([f"`{i+1}.` {t}" for i, t in enumerate(texts)])
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
    msg = f"🗑 ဖျက်လိုက်သော စာသား: `{removed}`" if lang == "mm" else f"🗑 Removed: `{removed}`"
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
        msg = f"🧠 Auto-Quiz အခြေအနေ: *{status}*\nဖွင့်ရန် `/quiz on`၊ ပိတ်ရန် `/quiz off` ရိုက်ပါ။" if lang == "mm" else f"🧠 Auto-Quiz Status: *{status}*\nUse `/quiz on` or `/quiz off`."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)


async def send_quiz_to_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    q_item = random.choice(MYANMAR_QUIZ_BANK)
    buttons = [
        [InlineKeyboardButton(text=f"{['က', 'ခ', 'ဂ'][i]}။ {opt}", callback_data=f"quiz_{chat_id}_{i}")]
        for i, opt in enumerate(q_item["opts"])
    ]
    markup = InlineKeyboardMarkup(buttons)
    text = (
        f"🧠 *မြန်မာဗဟုသုတ Quiz (အချိန် ၁ မိနစ်)*\n\n"
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
        f"⏰ *Quiz အချိန်စေ့သွားပါပြီ!*\n\n"
        f"❓ မေးခွန်း: {q_item['q']}\n"
        f"✅ အဖြေမှန်: *{correct_label}*\n"
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
        await reply_tracked(update.message, "🇲🇲 Bot ဘာသာစကားကို *မြန်မာဘာသာ* သို့ ပြောင်းလဲလိုက်ပါပြီ။", parse_mode=ParseMode.MARKDOWN)
    elif arg in ("en", "eng", "english"):
        bot_data_store["chat_langs"][cid] = "en"
        await save_to_jsonbin(force=True)
        await reply_tracked(update.message, "🇬🇧 Bot language switched to *English*.", parse_mode=ParseMode.MARKDOWN)
    else:
        curr = get_lang(update.effective_chat).upper()
        await reply_tracked(update.message, f"🌐 Current Language: *{curr}*\nUsage: `/lang mm` or `/lang en`", parse_mode=ParseMode.MARKDOWN)


async def cmd_afk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    uid = update.effective_user.id
    lang = get_lang(update.effective_chat)
    reason = " ".join(context.args).strip() or ("အနားယူနေပါသည်" if lang == "mm" else "Away from keyboard")
    afk_users[uid] = {"reason": reason, "since": time.time(), "name": update.effective_user.first_name}
    msg = f"💤 *{update.effective_user.first_name}* သည် AFK ဖြစ်သွားပါပြီ။\n📝 အကြောင်းပြချက်: _{reason}_" if lang == "mm" else f"💤 *{update.effective_user.first_name}* is now AFK.\n📝 Reason: _{reason}_"
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
            await reply_tracked(update.message, f"📌 *{key}:*\n{user_notes[key]}", parse_mode=ParseMode.MARKDOWN)
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
    header = "📒 *သင်၏ ကိုယ်ပိုင်မှတ်စု စာရင်း:*\n" if lang == "mm" else "📒 *Your Saved Notes:*\n"
    items = "\n".join([f"• `{k}` (ခေါ်ကြည့်ရန် `/note {k}`)" for k in user_notes.keys()])
    await reply_tracked(update.message, header + items, parse_mode=ParseMode.MARKDOWN)


# ==============================================================================
# 3. EVERYONE COMMANDS (14 LIGHTWEIGHT & FAST PUBLIC COMMANDS)
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
            f"🪪 *အကောင့် အချက်အလက် စစ်ဆေးချက်*\n\n"
            f"• အမည်: *{target.full_name}*\n"
            f"• Username: @{target.username or 'မရှိပါ'}\n"
            f"• User ID: `{target.id}`\n"
            f"• အဆင့်: {role}\n"
            f"• အကောင့်ဖွင့်ခဲ့သည့် ခန့်မှန်းနှစ်: *{year_est}*"
        )
    else:
        text = (
            f"🪪 *User Account Check*\n\n"
            f"• Name: *{target.full_name}*\n"
            f"• Username: @{target.username or 'None'}\n"
            f"• User ID: `{target.id}`\n"
            f"• Role: {role}\n"
            f"• Est. Creation Year: *{year_est}*"
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
    header = "🕒 *အရှေ့တောင်အာရှ (၁၁) နိုင်ငံ လက်ရှိအချိန်များ*\n\n" if lang == "mm" else "🕒 *Southeast Asia (11 Countries) Live Time*\n\n"
    lines = []
    for name, offset in sea_zones:
        local_dt = now_utc + timedelta(hours=offset)
        lines.append(f"• {name}: `{local_dt.strftime('%I:%M:%S %p')} ({local_dt.strftime('%d %b')})`")
    await reply_tracked(update.message, header + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_gevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    now = datetime.now(timezone.utc)
    year = now.year
    lines = []

    if lang == "mm":
        lines.append(f"📅 *မြန်မာ့အဓိက နေ့ထူးနေ့မြတ်များ ({year})*")
        lines.append("• *ဇန်နဝါရီ ၄* — လွတ်လပ်ရေးနေ့ 🇲🇲")
        lines.append("• *ဖေဖော်ဝါရီ ၁၂* — ပြည်ထောင်စုနေ့ 🇲🇲")
        lines.append("• *ဧပြီ ၁၃-၁၆* — မဟာသင်္ကြန်ပွဲတော် 💦")
        lines.append("• *ဇူလိုင် ၁၉* — အာဇာနည်နေ့ 🕯")
        lines.append("• *သီတင်းကျွတ် / တန်ဆောင်တိုင်* — မီးထွန်းပွဲတော်များ 🏮\n")
    else:
        lines.append(f"📅 *Key Observances & Live Global Holidays ({year})*\n")

    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            async with session.get("https://date.nager.at/api/v3/NextPublicHolidaysWorldwide", timeout=8) as resp:
                if resp.status == 200:
                    holidays = await resp.json()
                    lines.append("🌍 *Upcoming Global Holidays (Live):*")
                    seen = set()
                    for h in holidays:
                        key = f"{h['date']}_{h['name']}"
                        if key in seen:
                            continue
                        seen.add(key)
                        lines.append(f"• `{h['date']}` ({h['countryCode']}) — *{h['name']}*")
                        if len(seen) >= 7:
                            break
    except Exception:
        pass

    await reply_tracked(update.message, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_qtoday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    en_q = "The only way to do great work is to love what you do."
    author = "Steve Jobs"

    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            async with session.get("https://dummyjson.com/quotes/random", timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    en_q = data.get("quote", en_q)
                    author = data.get("author", author)
    except Exception:
        pass

    if lang == "mm":
        try:
            mm_q = await async_translate(en_q, target="my")
            text = f"💡 *ယနေ့အတွက် မှတ်သားဖွယ် စကားစု (Live)*\n\n_\"{mm_q}\"_\n\n_\"{en_q}\"_\n— *{author}*"
        except Exception:
            text = f"💡 *Quote of the Moment*\n\n_\"{en_q}\"_\n— *{author}*"
    else:
        text = f"💡 *Live Quote*\n\n_\"{en_q}\"_\n— *{author}*"
    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)


async def cmd_topq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    scores = bot_data_store["quiz_scores"]
    if not scores:
        msg = "🏆 Quiz ရမှတ်စာရင်း မရှိသေးပါ။ `/quiz on` ဖွင့်ပြီး ဖြေဆိုနိုင်ပါပြီ!" if lang == "mm" else "🏆 No quiz scores yet! Enable `/quiz on` to play."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return
    sorted_users = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)[:10]
    header = "🏆 *မြန်မာဗဟုသုတ Quiz ထိပ်တန်းစာရင်း (Top 10)*\n\n" if lang == "mm" else "🏆 *Quiz Leaderboard (Top 10)*\n\n"
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, (uid, info) in enumerate(sorted_users):
        badge = medals[idx] if idx < 3 else f"`{idx+1}.`"
        lines.append(f"{badge} *{info['name']}* — `{info['score']} pts`")
    await reply_tracked(update.message, header + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["💱 *Live ငွေလဲနှုန်းနှင့် Global Crypto ပေါက်ဈေး*\n"]
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        try:
            async with session.get("https://open.er-api.com/v6/latest/USD", timeout=10) as r1:
                fx = await r1.json()
                rates = fx.get("rates", {})
                mmk_ref = rates.get("MMK", 2100.0)
                thb = rates.get("THB", 34.0)
                sgd = rates.get("SGD", 1.30)
                jpy = rates.get("JPY", 155.0)
                cny = rates.get("CNY", 7.20)
                lines.append("💵 *Fiat Exchange (1 USD Base):*")
                lines.append(f"• 🇲🇲 MMK (Ref): `{mmk_ref:,.2f} MMK`")
                lines.append(f"• 🇹🇭 THB: `{thb:,.2f} THB`")
                lines.append(f"• 🇸🇬 SGD: `{sgd:,.2f} SGD`")
                lines.append(f"• 🇯🇵 JPY: `{jpy:,.2f} JPY`")
                lines.append(f"• 🇨🇳 CNY: `{cny:,.2f} CNY`\n")
        except Exception as e:
            lines.append(f"⚠️ Fiat rate error: `{e}`\n")

        try:
            symbols = '["TONUSDT","BTCUSDT","ETHUSDT","SOLUSDT"]'
            bn_url = f"https://api.binance.com/api/v3/ticker/price?symbols={urllib.parse.quote(symbols)}"
            async with session.get(bn_url, timeout=10) as r2:
                if r2.status == 200:
                    plist = await r2.json()
                    pmap = {item["symbol"]: float(item["price"]) for item in plist}
                    lines.append("🪙 *Global Crypto Live (Binance):*")
                    if "TONUSDT" in pmap:
                        lines.append(f"• 💎 TON: `${pmap['TONUSDT']:,.3f}`")
                    if "BTCUSDT" in pmap:
                        lines.append(f"• ₿ BTC: `${pmap['BTCUSDT']:,.2f}`")
                    if "ETHUSDT" in pmap:
                        lines.append(f"• Ξ ETH: `${pmap['ETHUSDT']:,.2f}`")
                    if "SOLUSDT" in pmap:
                        lines.append(f"• ◎ SOL: `${pmap['SOLUSDT']:,.2f}`")
                else:
                    raise RuntimeError(f"HTTP {r2.status}")
        except Exception:
            try:
                async with session.get("https://api.coinbase.com/v2/exchange-rates?currency=USD", timeout=10) as r3:
                    cb = await r3.json()
                    cr = cb["data"]["rates"]
                    lines.append("🪙 *Global Crypto Live (Coinbase):*")
                    lines.append(f"• 💎 TON: `${1/float(cr['TON']):,.3f}`")
                    lines.append(f"• ₿ BTC: `${1/float(cr['BTC']):,.2f}`")
                    lines.append(f"• Ξ ETH: `${1/float(cr['ETH']):,.2f}`")
            except Exception as e2:
                lines.append(f"⚠️ Crypto feed unavailable: `{e2}`")

    await reply_tracked(update.message, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    oz_usd = None
    thb_rate = 34.0

    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        try:
            async with session.get("https://open.er-api.com/v6/latest/USD", timeout=8) as r_fx:
                fx = await r_fx.json()
                thb_rate = fx.get("rates", {}).get("THB", 34.0)
        except Exception:
            pass

        try:
            async with session.get("https://api.gold-api.com/price/XAU", timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    oz_usd = float(data["price"])
        except Exception:
            pass

        if not oz_usd:
            try:
                async with session.get("https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT", timeout=8) as resp2:
                    if resp2.status == 200:
                        d2 = await resp2.json()
                        oz_usd = float(d2["price"])
            except Exception:
                pass

    if not oz_usd:
        await reply_tracked(update.message, "⚠️ ကမ္ဘာ့ရွှေဈေး ဆွဲယူ၍ မရသေးပါ။ ခဏနေ ပြန်စမ်းကြည့်ပါ။")
        return

    oz_thb = oz_usd * thb_rate
    kyatthar_usd = oz_usd * (16.606 / 31.1035)
    if lang == "mm":
        text = (
            f"🥇 *ကမ္ဘာ့ရွှေဈေးနှုန်း (Live Spot Gold XAU)*\n\n"
            f"• 1 Troy Ounce (31.1g): `${oz_usd:,.2f}` (`{oz_thb:,.0f} THB`)\n"
            f"• ၁ ကျပ်သား (16.6g) ညီမျှကမ္ဘာ့ဈေး: `${kyatthar_usd:,.2f} USD`\n"
            f"_(ပြည်တွင်းရွှေဈေးသည် ပြင်ပဒေါ်လာပေါက်ဈေးပေါ် မူတည်၍ အပြောင်းအလဲရှိနိုင်ပါသည်)_"
        )
    else:
        text = (
            f"🥇 *Live Spot Gold Price (XAU)*\n\n"
            f"• 1 Troy Ounce (31.1g): `${oz_usd:,.2f}` (`{oz_thb:,.0f} THB`)\n"
            f"• 1 Myanmar Kyatthar (16.6g) Eq: `${kyatthar_usd:,.2f} USD`"
        )
    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)


async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    city = " ".join(context.args).strip() if context.args else "Yangon"

    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        try:
            wttr_url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"
            async with session.get(wttr_url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    curr = data["current_condition"][0]
                    area = data.get("nearest_area", [{}])[0]
                    area_name = area.get("areaName", [{}])[0].get("value", city)
                    country = area.get("country", [{}])[0].get("value", "")
                    desc = curr.get("weatherDesc", [{}])[0].get("value", "Clear")
                    temp_c = curr.get("temp_C", "N/A")
                    feels_c = curr.get("FeelsLikeC", temp_c)
                    humidity = curr.get("humidity", "N/A")
                    wind = curr.get("windspeedKmph", "N/A")
                    loc_label = f"{area_name}, {country}".strip(", ")

                    if lang == "mm":
                        text = (
                            f"🌤 *မိုးလေဝသ အခြေအနေ — {loc_label}*\n\n"
                            f"• ☁️ အခြေအနေ: *{desc}*\n"
                            f"• 🌡 အပူချိန်: `{temp_c}°C` (ခံစားရမှု `{feels_c}°C`)\n"
                            f"• 💧 စိုထိုင်းဆ: `{humidity}%`\n"
                            f"• 💨 လေတိုက်နှုန်း: `{wind} km/h`"
                        )
                    else:
                        text = (
                            f"🌤 *Live Weather — {loc_label}*\n\n"
                            f"• ☁️ Condition: *{desc}*\n"
                            f"• 🌡 Temperature: `{temp_c}°C` (Feels like `{feels_c}°C`)\n"
                            f"• 💧 Humidity: `{humidity}%`\n"
                            f"• 💨 Wind Speed: `{wind} km/h`"
                        )
                    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)
                    return
        except Exception:
            pass

        try:
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=1&language=en&format=json"
            async with session.get(geo_url, timeout=10) as r1:
                gdata = await r1.json()
                if not gdata.get("results"):
                    msg = f"❌ `{city}` မြို့ကို ရှာမတွေ့ပါ။" if lang == "mm" else f"❌ City `{city}` not found."
                    await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
                    return
                loc = gdata["results"][0]
                lat, lon = loc["latitude"], loc["longitude"]
                full_name = f"{loc['name']}, {loc.get('country', '')}"

            w_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
            async with session.get(w_url, timeout=10) as r2:
                wdata = await r2.json()
                cw = wdata["current_weather"]
                text = (
                    f"🌤 *မိုးလေဝသ အခြေအနေ — {full_name}*\n\n"
                    f"• 🌡 အပူချိန်: `{cw['temperature']}°C`\n"
                    f"• 💨 လေတိုက်နှုန်း: `{cw['windspeed']} km/h`"
                )
                await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)
                return
        except Exception as e:
            await reply_tracked(update.message, f"⚠️ Weather service error: `{e}`", parse_mode=ParseMode.MARKDOWN)


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
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=12, border=3)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#0f172a", back_color="#ffffff").convert("RGB")
        bio = io.BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        return bio

    bio = await asyncio.to_thread(_make_qr)
    sent = await update.message.reply_photo(photo=bio, caption=f"📱 *QR Code:* `{data[:80]}`", parse_mode=ParseMode.MARKDOWN)
    track_bot_msg(update.effective_chat.id, sent.message_id)


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
        await reply_tracked(update.message, f"🧮 `{expr}` = *`{result:,}`*" if isinstance(result, (int, float)) else f"🧮 `{expr}` = *`{result}`*", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await reply_tracked(update.message, "❌ တွက်ချက်၍ မရပါ။ ပုစ္ဆာပုံစံ ပြန်စစ်ပါ။" if lang == "mm" else "❌ Could not evaluate expression.")


async def cmd_dict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    word = context.args[0].strip().lower() if context.args else ""
    if not word:
        msg = "⚠️ အင်္ဂလိပ်စကားလုံး ရိုက်ပါ: `/dict resilience`" if lang == "mm" else "⚠️ Usage: `/dict <word>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}"
    defn_text = ""
    pos = "noun"
    phonetic = ""
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    entry = data[0]
                    phonetic = entry.get("phonetic", "")
                    meaning = entry["meanings"][0]
                    pos = meaning.get("partOfSpeech", "")
                    defn_text = clean_text(meaning["definitions"][0]["definition"])
    except Exception:
        pass

    try:
        mm_word = await async_translate(word, target="my")
    except Exception:
        mm_word = word

    if not defn_text and mm_word.lower() == word.lower():
        await reply_tracked(update.message, f"❌ Word `{word}` not found.", parse_mode=ParseMode.MARKDOWN)
        return

    lines = [f"📖 *{word.capitalize()}* `{phonetic}`", f"🇲🇲 မြန်မာအဓိပ္ပာယ်: *{mm_word}*"]
    if defn_text:
        lines.append(f"🏷 Type: _{pos}_")
        lines.append(f"📝 Definition: {defn_text}")
    await reply_tracked(update.message, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


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
            f"📊 *Mobeho Bot စာရင်းချုပ် (Dashboard)*\n\n"
            f"• ⏱ Uptime: `{hrs}h {mins}m {secs}s`\n"
            f"• 📜 သိမ်းထားသော စာသား: `{len(bot_data_store['spam_texts'])}` ကြောင်း\n"
            f"• 🔐 Permit ပေးထားသူ: `{len(bot_data_store['permitted_users'])}` ဦး\n"
            f"• 🧠 Quiz ဖွင့်ထားသည့် Group: `{len(bot_data_store['quiz_enabled_chats'])}` ခု\n"
            f"• 🏆 Quiz ထိပ်ဆုံး: *{top_player}*\n"
            f"• 💬 ချိတ်ဆက်ထားသော Chat: `{len(bot_data_store['known_chats'])}` ခု"
        )
    else:
        text = (
            f"📊 *Mobeho Bot Dashboard*\n\n"
            f"• ⏱ Uptime: `{hrs}h {mins}m {secs}s`\n"
            f"• 📜 Saved Spam Texts: `{len(bot_data_store['spam_texts'])}`\n"
            f"• 🔐 Permitted Users: `{len(bot_data_store['permitted_users'])}`\n"
            f"• 🧠 Active Quiz Chats: `{len(bot_data_store['quiz_enabled_chats'])}`\n"
            f"• 🏆 Top Quiz Scorer: *{top_player}*\n"
            f"• 💬 Known Chats: `{len(bot_data_store['known_chats'])}`"
        )
    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)


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
        text = f"💘 *အချစ်ရာခိုင်နှုန်း တိုင်းတာချက်*\n\n*{u1}* 💞 *{u2}*\nရလဒ်: *{score}%* {bar}"
    else:
        text = f"💘 *Matchmaking Result*\n\n*{u1}* 💞 *{u2}*\nCompatibility: *{score}%* {bar}"
    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)


async def cmd_slang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat)
    term = " ".join(context.args).strip()
    if not term:
        msg = "⚠️ ရှာချင်တဲ့ ဗန်းစကား ရိုက်ပါ: `/slang goated`" if lang == "mm" else "⚠️ Usage: `/slang <term>`"
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)
        return

    url = f"https://api.urbandictionary.com/v0/define?term={urllib.parse.quote(term)}"
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                items = data.get("list", [])
                if not items:
                    await reply_tracked(update.message, f"❌ No slang definition found for `{term}`.", parse_mode=ParseMode.MARKDOWN)
                    return
                top = items[0]
                defn = smart_truncate(top.get("definition", "").replace("[", "").replace("]", ""), 650)
                ex = smart_truncate(top.get("example", "").replace("[", "").replace("]", ""), 300)
                text = f"🔥 *Slang: {term}*\n\n📖 {defn}\n\n💬 _Example:_ {ex}"
                await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Slang error: `{e}`", parse_mode=ParseMode.MARKDOWN)


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
            "👑 *Owner Exclusive Commands (13)*\n"
            "• `/apermit` — Grant user permission\n"
            "• `/rmpermit` — Revoke user permission\n"
            "• `/listpermit` — View all permitted users\n"
            "• `/broadcast` — Broadcast message to all chats\n"
            "• `/imagine <prompt>` — HD AI Text-to-Image generator\n"
            "• `/tr [lang] <text>` — Instant global translator\n"
            "• `/tts <text>` — Convert text to voice message\n"
            "• `/tldr` — Summarize long text/article\n"
            "• `/factcheck` — Cross-check news claim\n"
            "• `/qimg` — Reply to create HD Quote Card image\n"
            "• `/wiki <topic>` — Wikipedia research + image\n"
            "• `/movie <title>` — Global Movie & TV info + HD Poster\n"
            "• `/news` — Live Myanmar & World RSS news + Posters"
        )

    if is_permitted(uid):
        sections.append(
            "🔐 *Permitted Commands (13)*\n"
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
        "🌍 *Public Commands (14)*\n"
        "• `/check` — Check User ID & est. account year\n"
        "• `/time` — Live time across 11 SEA countries\n"
        "• `/gevent` — Live global holidays & Myanmar events\n"
        "• `/qtoday` — Live quote with Burmese translation\n"
        "• `/topq` — Myanmar Quiz Top 10 leaderboard\n"
        "• `/rate` — Live USD/MMK/THB & Global Crypto rates\n"
        "• `/gold` — Live Global Spot Gold price\n"
        "• `/weather <city>` — Live city weather forecast\n"
        "• `/qr <text/link>` — Generate HD QR code image\n"
        "• `/calc <expr>` — Quick calculator\n"
        "• `/dict <word>` — English-Myanmar dictionary\n"
        "• `/stats` — Bot uptime & live statistics\n"
        "• `/ship` — Matchmaking percentage\n"
        "• `/slang <term>` — Urban Dictionary slang lookup"
    )

    header = "🤖 *Mobeho Bot — Available Command List*\n\n" if lang == "en" else "🤖 *Mobeho Bot — အသုံးပြုနိုင်သော Command စာရင်း*\n\n"
    await reply_tracked(update.message, header + "\n\n".join(sections), parse_mode=ParseMode.MARKDOWN)


# ==============================================================================
# 5. INLINE CALLBACK ROUTER (QUIZ & NEWS WITH POSTERS)
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

    if data.startswith("news_"):
        if not is_owner(user.id):
            await query.answer("🔒 Owner Only Feature", show_alert=True)
            return

    if data.startswith("news_cat_"):
        cat = data.replace("news_cat_", "")
        await query.answer("📰 Live သတင်းများ ဆွဲယူနေပါသည်...")
        articles = await asyncio.to_thread(_parse_rss_sync, cat)
        if not articles:
            await query.edit_message_caption(caption="⚠️ လက်ရှိ သတင်းများ ဆွဲယူ၍ မရသေးပါ။ ခဏနေ ပြန်စမ်းကြည့်ပါ။")
            return
        cache_key = f"{chat_id}_{cat}"
        news_cache[cache_key] = articles

        header = (
            "🇲🇲 *ပြည်တွင်း Live သတင်းခေါင်းစဉ်များ*\nဖတ်လိုသော သတင်းနံပါတ်ကို နှိပ်ပါ:\n\n"
            if cat == "mm" else
            "🌍 *ပြည်ပ Live သတင်းခေါင်းစဉ်များ*\nဖတ်လိုသော သတင်းနံပါတ်ကို နှိပ်ပါ:\n\n"
        )
        lines = []
        btn_row = []
        btn_rows = []
        for idx, art in enumerate(articles):
            lines.append(f"*{idx+1}.* [{art['source']}] {art['title']}")
            btn_row.append(InlineKeyboardButton(f"📄 {idx+1}", callback_data=f"news_read_{cat}_{idx}"))
            if len(btn_row) == 4:
                btn_rows.append(btn_row)
                btn_row = []
        if btn_row:
            btn_rows.append(btn_row)
        btn_rows.append([InlineKeyboardButton("🔄 Switch Category", callback_data="news_back_main")])

        caption_text = header + "\n".join(lines)
        if len(caption_text) > 1020:
            caption_text = caption_text[:1015] + "..."

        try:
            await query.edit_message_caption(caption=caption_text, reply_markup=InlineKeyboardMarkup(btn_rows), parse_mode=ParseMode.MARKDOWN)
        except TelegramError:
            await send_tracked(context, chat_id, caption_text, reply_markup=InlineKeyboardMarkup(btn_rows), parse_mode=ParseMode.MARKDOWN)
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
            f"📰 *{art['title']}*\n"
            f"📡 Source: `{art['source']}` | 🕒 `{art['published']}`\n\n"
            f"{art['summary']}\n\n"
            f"🔗 [မူရင်းသတင်း အပြည့်အစုံဖတ်ရန်]({art['link']})"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ သတင်းစာရင်းသို့ ပြန်သွားရန်", callback_data=f"news_cat_{cat}")]])

        if art.get("image"):
            try:
                sent = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=art["image"],
                    caption=article_text[:1020],
                    reply_markup=kb,
                    parse_mode=ParseMode.MARKDOWN,
                )
                track_bot_msg(chat_id, sent.message_id)
                return
            except TelegramError:
                pass

        try:
            await query.edit_message_caption(caption=article_text[:1020], reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except TelegramError:
            await send_tracked(context, chat_id, article_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return

    if data == "news_back_main":
        await query.answer()
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🇲🇲 ပြည်တွင်းသတင်း (Myanmar)", callback_data="news_cat_mm"),
                InlineKeyboardButton("🌍 ပြည်ပသတင်း (World)", callback_data="news_cat_world"),
            ]
        ])
        try:
            await query.edit_message_caption(
                caption="📰 *Live သတင်းဌာန (Daily News Hub)*\nဖတ်ရှုလိုသော သတင်းအမျိုးအစားကို ရွေးချယ်ပါ:",
                reply_markup=kb,
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass


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
        msg = f"👋 *{user.first_name}* ပြန်လည်ရောက်ရှိလာပါပြီ (AFK ဖြုတ်ပြီး)။" if lang == "mm" else f"👋 *{user.first_name}* is back (AFK removed)."
        await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        replied_uid = update.message.reply_to_message.from_user.id
        if replied_uid in afk_users and replied_uid != user.id:
            info = afk_users[replied_uid]
            mins = max(1, int((time.time() - info["since"]) // 60))
            msg = (
                f"💤 *{info['name']}* သည် လက်ရှိ AFK ဖြစ်နေပါသည် ({mins} မိနစ်ကြာ)\n"
                f"📝 အကြောင်းပြချက်: _{info['reason']}_"
            ) if lang == "mm" else (
                f"💤 *{info['name']}* is currently AFK ({mins}m ago)\n"
                f"📝 Reason: _{info['reason']}_"
            )
            await reply_tracked(update.message, msg, parse_mode=ParseMode.MARKDOWN)

    if text.startswith("#") and len(text) > 1 and is_permitted(user.id):
        key = text[1:].split()[0].lower()
        user_notes = bot_data_store["notes"].get(str(user.id), {})
        if key in user_notes:
            await reply_tracked(update.message, f"📌 *{key}:*\n{user_notes[key]}", parse_mode=ParseMode.MARKDOWN)


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
        BotCommand("gevent", "Live global holidays & Myanmar events"),
        BotCommand("qtoday", "Live quote with Burmese translation"),
        BotCommand("topq", "Myanmar Quiz Top 10 leaderboard"),
        BotCommand("rate", "Live currency & global crypto rates"),
        BotCommand("gold", "Live Spot Gold price"),
        BotCommand("weather", "Live city weather forecast"),
        BotCommand("qr", "Generate HD QR code image"),
        BotCommand("calc", "Quick calculator"),
        BotCommand("dict", "English-Myanmar dictionary"),
        BotCommand("stats", "Bot uptime & live stats"),
        BotCommand("ship", "Matchmaking percentage"),
        BotCommand("slang", "Urban Dictionary slang lookup"),
    ]
    await app.bot.set_my_commands(public_dm_commands, scope=BotCommandScopeAllPrivateChats())

    if OWNER_ID:
        owner_dm_commands = [
            BotCommand("apermit", "[Owner] Grant user permission"),
            BotCommand("rmpermit", "[Owner] Revoke user permission"),
            BotCommand("listpermit", "[Owner] List permitted users"),
            BotCommand("broadcast", "[Owner] Broadcast to all chats"),
            BotCommand("imagine", "[Owner AI] HD Text-to-Image generator"),
            BotCommand("tr", "[Owner AI] Instant global translator"),
            BotCommand("tts", "[Owner AI] Text to voice message"),
            BotCommand("tldr", "[Owner AI] Summarize long text/article"),
            BotCommand("factcheck", "[Owner AI] Cross-check news claim"),
            BotCommand("qimg", "[Owner Pro] Create HD Quote Card image"),
            BotCommand("wiki", "[Owner Pro] Wikipedia summary + image"),
            BotCommand("movie", "[Owner Pro] Movie & TV info + HD poster"),
            BotCommand("news", "[Owner Pro] Live Myanmar & World news"),
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
    logger.info("Mobeho Bot v3.1 (Owner AI Suite) initialized cleanly.")


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

    # Owner Exclusive (13: 4 Admin + 9 AI/Pro Suite)
    application.add_handler(CommandHandler("apermit", cmd_apermit))
    application.add_handler(CommandHandler("rmpermit", cmd_rmpermit))
    application.add_handler(CommandHandler("listpermit", cmd_listpermit))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(CommandHandler("imagine", cmd_imagine))
    application.add_handler(CommandHandler("tr", cmd_tr))
    application.add_handler(CommandHandler("tts", cmd_tts))
    application.add_handler(CommandHandler("tldr", cmd_tldr))
    application.add_handler(CommandHandler("factcheck", cmd_factcheck))
    application.add_handler(CommandHandler(["qimg", "quoteimg"], cmd_qimg))
    application.add_handler(CommandHandler("wiki", cmd_wiki))
    application.add_handler(CommandHandler("movie", cmd_movie))
    application.add_handler(CommandHandler(["news", "dailynews"], cmd_news))

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

    # Everyone (14 + legacy aliases)
    application.add_handler(CommandHandler(["check", "id"], cmd_check))
    application.add_handler(CommandHandler("time", cmd_time))
    application.add_handler(CommandHandler("gevent", cmd_gevent))
    application.add_handler(CommandHandler("qtoday", cmd_qtoday))
    application.add_handler(CommandHandler("topq", cmd_topq))
    application.add_handler(CommandHandler(["rate", "currency"], cmd_rate))
    application.add_handler(CommandHandler("gold", cmd_gold))
    application.add_handler(CommandHandler("weather", cmd_weather))
    application.add_handler(CommandHandler("qr", cmd_qr))
    application.add_handler(CommandHandler("calc", cmd_calc))
    application.add_handler(CommandHandler("dict", cmd_dict))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("ship", cmd_ship))
    application.add_handler(CommandHandler(["slang", "define_slang"], cmd_slang))

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

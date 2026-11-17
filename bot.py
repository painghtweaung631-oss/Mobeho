import os
import re
import ast
import copy
import io
import json
import time
import math
import base64
import random
import asyncio
import logging
import html
import urllib.parse
from datetime import datetime, timezone, timedelta

import aiohttp
import qrcode
import edge_tts
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
from telegram.error import TelegramError, RetryAfter
from aiohttp import web

# ==============================================================================
# CONFIGURATION & ENVIRONMENT VARIABLES (STRIPPED & CLEAN)
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "8966139030").strip() or "8966139030")
JSONBIN_API_KEY = os.getenv("JSONBIN_API_KEY", "").strip()
JSONBIN_BIN_ID = os.getenv("JSONBIN_BIN_ID", "").strip()
PORT = int(os.getenv("PORT", "10000").strip() or "10000")

# Active Free-Tier AI & Search Keys (Cerebras & Image Gen removed as requested)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
SAMBANOVA_API_KEY = os.getenv("SAMBANOVA_API_KEY", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
COHERE_API_KEY = os.getenv("COHERE_API_KEY", "").strip()
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "").strip()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("MobehoBot")

START_TIME = time.time()
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

# Global Reusable HTTP ClientSession for Ultra-Fast Responses
_http_session: aiohttp.ClientSession | None = None


async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        connector = aiohttp.TCPConnector(limit=60, ttl_dns_cache=300)
        _http_session = aiohttp.ClientSession(
            connector=connector,
            headers={"User-Agent": USER_AGENT},
        )
    return _http_session


# ==============================================================================
# IN-MEMORY STATE & JSONBIN CLOUD SYNC
# ==============================================================================
DEFAULT_DATA = {
    "special_users": [],         # Co-Owners (/suser a/d)
    "permitted_users": [],       # Permitted Users (/apermit /rpermit)
    "spam_texts": [
        "မင်္ဂလာပါ! ဘာတွေလုပ်နေကြလဲဗျို့ 👋",
        "စကားလေးဘာလေး ပြောကြပါဦးနော် 💬",
        "Mobeho Bot မှ နှုတ်ခွန်းဆက်သအပ်ပါတယ် 🚀",
    ],
    "chat_langs": {},            # {chat_id_str: "mm" | "en"}
    "known_chats": [],           # [chat_id_int]
    "hourly_quote": {
        "hour_key": "",
        "en": "The only way to do great work is to love what you do.",
        "author": "Steve Jobs",
        "mm": "ကြီးကျယ်ခမ်းနားတဲ့ အလုပ်တစ်ခုကို လုပ်နိုင်ဖို့ တစ်ခုတည်းသော နည်းလမ်းကတော့ ကိုယ်လုပ်နေတဲ့ အလုပ်ကို ချစ်မြတ်နိုးဖို့ပါပဲ။",
    },
}

bot_data_store = json.loads(json.dumps(DEFAULT_DATA))
_save_lock = asyncio.Lock()
_dirty_flag = False
_dirty_revision = 0

# Runtime volatile states
active_spam_tasks = {}      # {chat_id: asyncio.Task}
bot_sent_messages = {}      # {chat_id: [msg_id, ...]}
afk_users = {}              # {user_id: {"reason": str, "until": float | None, "since": float, "name": str}}
wiki_cache = {}             # {cache_key: {"title": str, "full_text": str, "thumb": str, "url": str, "lang": str}}


async def load_from_jsonbin():
    global bot_data_store
    if not JSONBIN_API_KEY or not JSONBIN_BIN_ID:
        logger.warning("JSONBin credentials missing. Using in-memory storage only.")
        return
    url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest"
    headers = {"X-Master-Key": JSONBIN_API_KEY}
    try:
        session = await get_http_session()
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
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
    global _dirty_flag, _dirty_revision
    _dirty_flag = True
    _dirty_revision += 1

    if not JSONBIN_API_KEY or not JSONBIN_BIN_ID or not force:
        return

    async with _save_lock:
        snapshot_revision = _dirty_revision
        snapshot = copy.deepcopy(bot_data_store)
        url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
        headers = {
            "Content-Type": "application/json",
            "X-Master-Key": JSONBIN_API_KEY,
        }
        try:
            session = await get_http_session()
            async with session.put(url, json=snapshot, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status == 200:
                    if _dirty_revision == snapshot_revision:
                        _dirty_flag = False
                else:
                    logger.error(f"JSONBin save error: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"Failed to save to JSONBin: {e}")


async def periodic_sync_worker():
    while True:
        await asyncio.sleep(25)
        if _dirty_flag:
            await save_to_jsonbin(force=True)


# ==============================================================================
# 4-TIER ACCESS CONTROL & GLOBAL LANGUAGE LOCALIZATION
# ==============================================================================
def is_super(user_id: int) -> bool:
    """Tier 1: Primary Owner ONLY."""
    return user_id == OWNER_ID


def is_special(user_id: int) -> bool:
    """Tier 2: Primary Owner + Co-Owners (/suser a)."""
    return is_super(user_id) or (user_id in bot_data_store["special_users"])


def is_permitted(user_id: int) -> bool:
    """Tier 3: Primary Owner + Co-Owners + Permitted Users (/apermit)."""
    return is_special(user_id) or (user_id in bot_data_store["permitted_users"])


def get_lang(chat) -> str:
    cid = str(chat.id)
    if cid in bot_data_store["chat_langs"]:
        return bot_data_store["chat_langs"][cid]
    return "mm"


def t_msg(chat, mm_text: str, en_text: str) -> str:
    return mm_text if get_lang(chat) == "mm" else en_text


def esc(text) -> str:
    return html.escape(str(text or ""))


def track_chat(chat_id: int):
    if chat_id not in bot_data_store["known_chats"]:
        bot_data_store["known_chats"].append(chat_id)
        asyncio.create_task(save_to_jsonbin(force=False))


def track_bot_msg(chat_id: int, message_id: int):
    bot_sent_messages.setdefault(chat_id, []).append(message_id)
    if len(bot_sent_messages[chat_id]) > 250:
        bot_sent_messages[chat_id] = bot_sent_messages[chat_id][-250:]
    while len(bot_sent_messages) > 300:
        bot_sent_messages.pop(next(iter(bot_sent_messages)))


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def smart_truncate(text: str, max_len: int = 850) -> str:
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


def ai_to_html(raw: str) -> str:
    """Safely converts basic Markdown bold/code from AI output into clean Telegram HTML."""
    s = html.escape(raw.strip())
    s = re.sub(r"\*\*(.+?)\*\*", r"<b></b>", s)
    s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
    return s


async def send_tracked(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs):
    if len(text) > 4000:
        text = text[:3990] + "..."
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except RetryAfter as ra:
        await asyncio.sleep(ra.retry_after + 1)
        msg = await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except TelegramError:
        kwargs.pop("parse_mode", None)
        msg = await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    track_bot_msg(chat_id, msg.message_id)
    return msg


async def reply_tracked(message, text: str, **kwargs):
    if len(text) > 4000:
        text = text[:3990] + "..."
    try:
        msg = await message.reply_text(text=text, **kwargs)
    except RetryAfter as ra:
        await asyncio.sleep(ra.retry_after + 1)
        msg = await message.reply_text(text=text, **kwargs)
    except TelegramError:
        kwargs.pop("parse_mode", None)
        msg = await message.reply_text(text=text, **kwargs)
    track_bot_msg(message.chat_id, msg.message_id)
    return msg


# ==============================================================================
# DIRECT PINPOINT AI ENGINES (SUB-2 SECOND FAST ROUTING, FREE TIER ONLY)
# ==============================================================================
async def _call_gemini(prompt: str, model: str = "gemini-2.0-flash", system_prompt: str = "", image_bytes: bytes = None, timeout_sec: float = 12.0) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
    user_parts = [{"text": prompt}]
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        user_parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
    payload = {
        "contents": [{"role": "user", "parts": user_parts}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1400},
    }
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    session = await get_http_session()
    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
        if resp.status != 200:
            err_txt = await resp.text()
            raise RuntimeError(f"Gemini {model} HTTP {resp.status}: {err_txt[:100]}")
        data = await resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini empty response")
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts).strip()


async def _call_openai_compat(base_url: str, api_key: str, model: str, prompt: str, system_prompt: str = "", timeout_sec: float = 8.0) -> str:
    if not api_key:
        raise RuntimeError(f"API key missing for {model}")
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages, "temperature": 0.4, "max_tokens": 1400}

    session = await get_http_session()
    async with session.post(base_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
        if resp.status != 200:
            err_txt = await resp.text()
            raise RuntimeError(f"{model} HTTP {resp.status}: {err_txt[:100]}")
        data = await resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


async def _call_cohere(prompt: str, system_prompt: str = "", timeout_sec: float = 9.0) -> str:
    if not COHERE_API_KEY:
        raise RuntimeError("COHERE_API_KEY missing")
    url = "https://api.cohere.com/v2/chat"
    headers = {"Authorization": f"Bearer {COHERE_API_KEY}", "Content-Type": "application/json"}
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    payload = {"model": "command-r-plus-08-2024", "messages": messages}
    session = await get_http_session()
    async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Cohere HTTP {resp.status}")
        data = await resp.json()
        return data["message"]["content"][0]["text"].strip()


async def run_direct_ai(route: str, prompt: str, system_prompt: str = "", image_bytes: bytes = None) -> tuple[str, str]:
    """
    Pinpoint Direct AI Routing by Command Purpose (Fastest engine first, 6-12s strict timeout):
    - 'solve'   : Gemini 2.0 Flash (supports text & image) -> SambaNova Llama-3.3 -> GitHub GPT-4o-mini
    - 'tran'    : Cohere Command-R+ (best Burmese) -> Groq Llama-3.3-70B -> Gemini 2.0 Flash-Lite
    - 'grammar' : GitHub GPT-4o-mini -> Groq Llama-3.3-70B -> Gemini 2.0 Flash-Lite
    - 'gsearch' : Gemini 2.0 Flash-Lite -> Groq Llama-3.3-70B -> SambaNova Llama-3.3
    - 'fast'    : Groq Llama-3.3-70B -> Gemini 2.0 Flash-Lite
    """
    if image_bytes:
        for m in ("gemini-2.0-flash", "gemini-2.0-flash-lite"):
            try:
                ans = await _call_gemini(prompt, model=m, system_prompt=system_prompt, image_bytes=image_bytes, timeout_sec=15.0)
                if ans:
                    return ans, f"Gemini ({m})"
            except Exception as e:
                logger.warning(f"Vision fallback ({m}): {e}")
        raise RuntimeError("Vision AI unavailable. Check GEMINI_API_KEY.")

    routes = {
        "solve": [
            ("Gemini 2.0 Flash", lambda: _call_gemini(prompt, "gemini-2.0-flash", system_prompt, timeout_sec=12.0)),
            ("SambaNova Llama-3.3-70B", lambda: _call_openai_compat("https://api.sambanova.ai/v1/chat/completions", SAMBANOVA_API_KEY, "Meta-Llama-3.3-70B-Instruct", prompt, system_prompt, timeout_sec=9.0)),
            ("GitHub GPT-4o-mini", lambda: _call_openai_compat("https://models.github.ai/inference/chat/completions", GITHUB_TOKEN, "gpt-4o-mini", prompt, system_prompt, timeout_sec=9.0)),
            ("Groq Llama-3.3-70B", lambda: _call_openai_compat("https://api.groq.com/openai/v1/chat/completions", GROQ_API_KEY, "llama-3.3-70b-versatile", prompt, system_prompt, timeout_sec=7.0)),
        ],
        "tran": [
            ("Groq Llama-3.3-70B", lambda: _call_openai_compat("https://api.groq.com/openai/v1/chat/completions", GROQ_API_KEY, "llama-3.3-70b-versatile", prompt, system_prompt, timeout_sec=6.0)),
            ("Cohere Command-R+", lambda: _call_cohere(prompt, system_prompt, timeout_sec=8.0)),
            ("Gemini 2.0 Flash-Lite", lambda: _call_gemini(prompt, "gemini-2.0-flash-lite", system_prompt, timeout_sec=7.0)),
        ],
        "grammar": [
            ("GitHub GPT-4o-mini", lambda: _call_openai_compat("https://models.github.ai/inference/chat/completions", GITHUB_TOKEN, "gpt-4o-mini", prompt, system_prompt, timeout_sec=7.0)),
            ("Groq Llama-3.3-70B", lambda: _call_openai_compat("https://api.groq.com/openai/v1/chat/completions", GROQ_API_KEY, "llama-3.3-70b-versatile", prompt, system_prompt, timeout_sec=6.0)),
            ("Gemini 2.0 Flash-Lite", lambda: _call_gemini(prompt, "gemini-2.0-flash-lite", system_prompt, timeout_sec=7.0)),
        ],
        "gsearch": [
            ("Gemini 2.0 Flash-Lite", lambda: _call_gemini(prompt, "gemini-2.0-flash-lite", system_prompt, timeout_sec=7.0)),
            ("Groq Llama-3.3-70B", lambda: _call_openai_compat("https://api.groq.com/openai/v1/chat/completions", GROQ_API_KEY, "llama-3.3-70b-versatile", prompt, system_prompt, timeout_sec=6.0)),
            ("SambaNova Llama-3.3-70B", lambda: _call_openai_compat("https://api.sambanova.ai/v1/chat/completions", SAMBANOVA_API_KEY, "Meta-Llama-3.3-70B-Instruct", prompt, system_prompt, timeout_sec=8.0)),
        ],
        "fast": [
            ("Groq Llama-3.3-70B", lambda: _call_openai_compat("https://api.groq.com/openai/v1/chat/completions", GROQ_API_KEY, "llama-3.3-70b-versatile", prompt, system_prompt, timeout_sec=6.0)),
            ("Gemini 2.0 Flash-Lite", lambda: _call_gemini(prompt, "gemini-2.0-flash-lite", system_prompt, timeout_sec=6.0)),
        ],
    }

    last_err = "No AI keys configured."
    for engine_name, fn in routes.get(route, routes["fast"]):
        try:
            ans = await fn()
            if ans:
                return ans, engine_name
        except Exception as e:
            last_err = str(e)
            logger.warning(f"Direct AI [{route}] fallback ({engine_name}): {e}")
            continue

    raise RuntimeError(last_err)


# ==============================================================================
# SERPER HD GOOGLE SEARCH + TAVILY WEB SEARCH
# ==============================================================================
async def search_hd_google(query: str) -> tuple[list[dict], str]:
    """Returns (web_results, crisp_hd_image_url) using Serper Search + Serper Images + Tavily fallback."""
    results = []
    hd_image = ""
    session = await get_http_session()

    if SERPER_API_KEY:
        headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}

        async def _fetch_web():
            async with session.post("https://google.serper.dev/search", json={"q": query, "num": 4}, headers=headers, timeout=aiohttp.ClientTimeout(total=7)) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {}

        async def _fetch_images():
            async with session.post("https://google.serper.dev/images", json={"q": query, "num": 5}, headers=headers, timeout=aiohttp.ClientTimeout(total=7)) as resp:
                if resp.status == 200:
                    return await resp.json()
                return {}

        web_data, img_data = await asyncio.gather(_fetch_web(), _fetch_images(), return_exceptions=True)

        if isinstance(web_data, dict):
            if "knowledgeGraph" in web_data:
                kg = web_data["knowledgeGraph"]
                results.append({
                    "title": kg.get("title", query),
                    "snippet": kg.get("description", ""),
                    "url": kg.get("website", ""),
                })
                if kg.get("imageUrl") and str(kg["imageUrl"]).startswith("http"):
                    hd_image = kg["imageUrl"]
            for r in web_data.get("organic", [])[:4]:
                results.append({"title": r.get("title", ""), "snippet": r.get("snippet", ""), "url": r.get("link", "")})

        if isinstance(img_data, dict):
            for im in img_data.get("images", []):
                url = im.get("imageUrl", "")
                w = int(im.get("imageWidth", 0) or 0)
                if url.startswith("http") and not url.endswith(".svg") and (w >= 400 or not hd_image):
                    hd_image = url
                    break

    if not results and TAVILY_API_KEY:
        try:
            payload = {"api_key": TAVILY_API_KEY, "query": query, "search_depth": "basic", "max_results": 4, "include_images": True}
            async with session.post("https://api.tavily.com/search", json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for r in data.get("results", []):
                        results.append({"title": r.get("title", ""), "snippet": r.get("content", ""), "url": r.get("url", "")})
                    imgs = data.get("images", [])
                    if imgs and not hd_image:
                        hd_image = imgs[0] if isinstance(imgs[0], str) else imgs[0].get("url", "")
        except Exception as e:
            logger.warning(f"Tavily fallback error: {e}")

    return results, hd_image


# ==============================================================================
# ASYNC TRANSLATE HELPER (GOOGLE GTX + MYMEMORY BACKUP)
# ==============================================================================
async def async_translate(text: str, target: str = "my") -> str:
    if not text.strip():
        return ""
    target = "my" if target in ("mm", "burmese", "myanmar") else target
    snippet = text[:2500]
    session = await get_http_session()

    gtx_url = (
        f"https://translate.googleapis.com/translate_a/single"
        f"?client=gtx&sl=auto&tl={urllib.parse.quote(target)}&dt=t&q={urllib.parse.quote(snippet)}"
    )
    try:
        async with session.get(gtx_url, timeout=aiohttp.ClientTimeout(total=7)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, list) and data[0]:
                    translated = "".join(seg[0] for seg in data[0] if seg and seg[0])
                    if translated:
                        return translated
    except Exception:
        pass

    mm_url = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(snippet[:500])}&langpair=en|{urllib.parse.quote(target)}"
    async with session.get(mm_url, timeout=aiohttp.ClientTimeout(total=7)) as resp:
        if resp.status == 200:
            data = await resp.json()
            res = data.get("responseData", {}).get("translatedText", "")
            if res:
                return html.unescape(res)
    raise RuntimeError("Translation services temporarily busy.")


# ==============================================================================
# HOURLY QUOTE ENGINE (/qhour — UPDATES ONCE EVERY HOUR, SYNCED WITH /lang)
# ==============================================================================
FALLBACK_QUOTES = [
    ("The only way to do great work is to love what you do.", "Steve Jobs", "ကြီးကျယ်ခမ်းနားတဲ့ အလုပ်တစ်ခုကို လုပ်နိုင်ဖို့ တစ်ခုတည်းသော နည်းလမ်းကတော့ ကိုယ်လုပ်နေတဲ့ အလုပ်ကို ချစ်မြတ်နိုးဖို့ပါပဲ။"),
    ("Success is not final, failure is not fatal: it is the courage to continue that counts.", "Winston Churchill", "အောင်မြင်မှုဆိုတာ အဆုံးသတ်မဟုတ်သလို ကျရှုံးမှုဆိုတာလည်း သေဆုံးခြင်းမဟုတ်ပါဘူး၊ ဆက်လက်လျှောက်လှမ်းရဲတဲ့ သတ္တိကသာ အဓိကပါ။"),
    ("In the middle of every difficulty lies opportunity.", "Albert Einstein", "အခက်အခဲတိုင်းရဲ့ အလယ်ဗဟိုမှာ အခွင့်အလမ်းကောင်းတွေ ရှိနေတတ်ပါတယ်။"),
    ("Do what you can, with what you have, where you are.", "Theodore Roosevelt", "သင်ရောက်နေတဲ့ နေရာကနေ သင့်မှာရှိတဲ့ အရာနဲ့ အကောင်းဆုံး လုပ်ဆောင်ပါ။"),
]


async def refresh_hourly_quote_if_needed(force: bool = False):
    now_mmt = datetime.now(timezone(timedelta(hours=6, minutes=30)))
    current_hour_key = now_mmt.strftime("%Y-%m-%d-%H")
    hq = bot_data_store.get("hourly_quote", {})

    if not force and hq.get("hour_key") == current_hour_key and hq.get("en") and hq.get("mm"):
        return hq

    en_q, author, mm_q = random.choice(FALLBACK_QUOTES)
    try:
        session = await get_http_session()
        async with session.get("https://dummyjson.com/quotes/random", timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                data = await resp.json()
                en_q = data.get("quote", en_q)
                author = data.get("author", author)
                try:
                    mm_q, _ = await run_direct_ai("fast", f"Translate this quote naturally into Burmese. Output ONLY the Burmese translation:\n\n{en_q}")
                except Exception:
                    mm_q = await async_translate(en_q, target="my")
    except Exception:
        pass

    bot_data_store["hourly_quote"] = {
        "hour_key": current_hour_key,
        "en": en_q,
        "author": author,
        "mm": mm_q,
    }
    await save_to_jsonbin(force=False)
    return bot_data_store["hourly_quote"]


async def hourly_quote_worker():
    while True:
        try:
            await refresh_hourly_quote_if_needed(force=False)
        except Exception as e:
            logger.warning(f"Hourly quote worker error: {e}")
        await asyncio.sleep(300)


# ==============================================================================
# SCOPED MENU UPDATER HELPER (UPDATES IMMEDIATELY ON /suser OR /apermit)
# ==============================================================================
async def sync_user_dm_menu(bot, target_id: int):
    public_cmds = [
        BotCommand("cmlist", "View available commands"),
        BotCommand("wiki", "Wikipedia full article viewer"),
        BotCommand("checkpf", "Check user ID & username"),
        BotCommand("gevent", "Myanmar & Global live holidays"),
        BotCommand("qhour", "Hourly quote (auto mm/en)"),
        BotCommand("rate", "Live currency & crypto rates"),
        BotCommand("qr", "Generate HD QR code"),
        BotCommand("calc", "Math calculator"),
        BotCommand("dict", "English-Myanmar dictionary"),
        BotCommand("stats", "Bot uptime & live stats"),
    ]
    permit_cmds = [
        BotCommand("lang", "[Permit] Switch bot language (mm/en)"),
        BotCommand("selsen", "[Permit] Reply & mention loop"),
        BotCommand("yat", "[Permit] Stop active loop"),
        BotCommand("delmsg", "[Permit] Delete bot messages"),
        BotCommand("addtext", "[Permit] Add spam text"),
        BotCommand("listtext", "[Permit] List spam texts"),
        BotCommand("edittext", "[Permit] Edit spam text"),
        BotCommand("removetext", "[Permit] Remove spam text"),
        BotCommand("cleartext", "[Permit] Clear spam texts"),
    ] + public_cmds

    special_cmds = [
        BotCommand("gsearch", "[Special] Google Search + HD Photo"),
        BotCommand("tran", "[Special] Reply & translate text"),
        BotCommand("tts", "[Special] Female voice TTS"),
        BotCommand("ttsm", "[Special] Male voice TTS"),
        BotCommand("grammar", "[Special] English grammar coach"),
        BotCommand("afk", "[Special] Set timed AFK status"),
    ] + permit_cmds

    super_cmds = [
        BotCommand("suser", "[Super] Add/remove Co-Owner (a/d)"),
        BotCommand("userlist", "[Super] List Co-Owners & Permitted"),
        BotCommand("apermit", "[Super] Grant user permission"),
        BotCommand("rpermit", "[Super] Revoke user permission"),
        BotCommand("broadcast", "[Super] Broadcast to all chats"),
        BotCommand("solve", "[Super] Step-by-step problem solver"),
        BotCommand("stt", "[Super] Voice note to text"),
        BotCommand("aistatus", "[Super] Check active AI engines"),
    ] + special_cmds

    try:
        if is_super(target_id):
            await bot.set_my_commands(super_cmds, scope=BotCommandScopeChat(chat_id=target_id))
        elif is_special(target_id):
            await bot.set_my_commands(special_cmds, scope=BotCommandScopeChat(chat_id=target_id))
        elif is_permitted(target_id):
            await bot.set_my_commands(permit_cmds, scope=BotCommandScopeChat(chat_id=target_id))
        else:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=target_id))
    except TelegramError:
        pass


# ==============================================================================
# 1. SUPER OWNER COMMANDS (8 COMMANDS: PRIMARY OWNER ONLY)
# ==============================================================================
async def cmd_suser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        try:
            await update.message.delete()
        except TelegramError:
            pass

    action = context.args[0].lower() if context.args else ""
    target_id = None
    target_name = "User"

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        u = update.message.reply_to_message.from_user
        target_id = u.id
        target_name = u.full_name
    elif len(context.args) >= 2 and context.args[1].isdigit():
        target_id = int(context.args[1])
        target_name = str(target_id)

    if action not in ("a", "d", "add", "del") or not target_id:
        await send_tracked(
            context,
            update.effective_chat.id,
            t_msg(
                update.effective_chat,
                "⚠️ <b>Co-Owner စီမံခန့်ခွဲမှု</b>\n• ခန့်ရန်: Reply ထောက်ပြီး <code>/suser a</code> (သို့) <code>/suser a 123456789</code>\n• ဖြုတ်ရန်: Reply ထောက်ပြီး <code>/suser d</code> (သို့) <code>/suser d 123456789</code>",
                "⚠️ <b>Co-Owner Management</b>\n• Add: Reply with <code>/suser a</code> or <code>/suser a 123456789</code>\n• Remove: Reply with <code>/suser d</code> or <code>/suser d 123456789</code>",
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    if target_id == OWNER_ID:
        await send_tracked(context, update.effective_chat.id, "👑 Primary Owner cannot be modified.")
        return

    if action in ("a", "add"):
        if target_id not in bot_data_store["special_users"]:
            bot_data_store["special_users"].append(target_id)
            await save_to_jsonbin(force=True)
        await sync_user_dm_menu(context.bot, target_id)
        await send_tracked(
            context,
            update.effective_chat.id,
            t_msg(
                update.effective_chat,
                f"⭐ <b>Co-Owner ခန့်အပ်ပြီးပါပြီ</b>\n👤 <code>{esc(target_name)}</code> (<code>{target_id}</code>)",
                f"⭐ <b>Co-Owner Added</b>\n👤 <code>{esc(target_name)}</code> (<code>{target_id}</code>)",
            ),
            parse_mode=ParseMode.HTML,
        )
    else:
        if target_id in bot_data_store["special_users"]:
            bot_data_store["special_users"].remove(target_id)
            await save_to_jsonbin(force=True)
            await sync_user_dm_menu(context.bot, target_id)
            await send_tracked(
                context,
                update.effective_chat.id,
                t_msg(update.effective_chat, f"🗑 <b>Co-Owner ဖြုတ်သိမ်းပြီးပါပြီ:</b> <code>{target_id}</code>", f"🗑 <b>Co-Owner Removed:</b> <code>{target_id}</code>"),
                parse_mode=ParseMode.HTML,
            )
        else:
            await send_tracked(
                context,
                update.effective_chat.id,
                t_msg(update.effective_chat, f"ℹ️ <code>{target_id}</code> သည် Co-Owner စာရင်းထဲတွင် မရှိပါ။", f"ℹ️ <code>{target_id}</code> is not in Co-Owner list."),
                parse_mode=ParseMode.HTML,
            )


async def cmd_userlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        try:
            await update.message.delete()
        except TelegramError:
            pass

    susers = bot_data_store["special_users"]
    pusers = bot_data_store["permitted_users"]

    s_lines = [f"  • <code>{uid}</code>" for uid in susers] or ["  • <i>None</i>"]
    p_lines = [f"  • <code>{uid}</code>" for uid in pusers] or ["  • <i>None</i>"]

    msg = (
        f"📋 <b>Authorized Users List</b>\n\n"
        f"⭐ <b>Co-Owners (Special — {len(susers)}):</b>\n" + "\n".join(s_lines) + "\n\n"
        f"🔐 <b>Permitted Users ({len(pusers)}):</b>\n" + "\n".join(p_lines)
    )
    await send_tracked(context, update.effective_chat.id, msg, parse_mode=ParseMode.HTML)


async def cmd_apermit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        try:
            await update.message.delete()
        except TelegramError:
            pass

    target_id = None
    target_name = "User"
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        u = update.message.reply_to_message.from_user
        target_id = u.id
        target_name = u.full_name
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
        target_name = str(target_id)

    if not target_id:
        await send_tracked(
            context,
            update.effective_chat.id,
            t_msg(update.effective_chat, "⚠️ Reply ထောက်ပြီး <code>/apermit</code> (သို့) <code>/apermit 123456789</code> ရိုက်ပါ။", "⚠️ Reply to a user or send <code>/apermit 123456789</code>"),
            parse_mode=ParseMode.HTML,
        )
        return

    if target_id not in bot_data_store["permitted_users"]:
        bot_data_store["permitted_users"].append(target_id)
        await save_to_jsonbin(force=True)
    await sync_user_dm_menu(context.bot, target_id)
    await send_tracked(
        context,
        update.effective_chat.id,
        t_msg(update.effective_chat, f"✅ <b>အသုံးပြုခွင့် ပေးပြီးပါပြီ:</b> <code>{esc(target_name)}</code> (<code>{target_id}</code>)", f"✅ <b>Permission Granted:</b> <code>{esc(target_name)}</code> (<code>{target_id}</code>)"),
        parse_mode=ParseMode.HTML,
    )


async def cmd_rpermit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        try:
            await update.message.delete()
        except TelegramError:
            pass

    target_id = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])

    if not target_id:
        await send_tracked(
            context,
            update.effective_chat.id,
            t_msg(update.effective_chat, "⚠️ Reply ထောက်ပြီး <code>/rpermit</code> (သို့) <code>/rpermit 123456789</code> ရိုက်ပါ။", "⚠️ Reply to a user or send <code>/rpermit 123456789</code>"),
            parse_mode=ParseMode.HTML,
        )
        return

    if target_id in bot_data_store["permitted_users"]:
        bot_data_store["permitted_users"].remove(target_id)
        await save_to_jsonbin(force=True)
        await sync_user_dm_menu(context.bot, target_id)
        await send_tracked(
            context,
            update.effective_chat.id,
            t_msg(update.effective_chat, f"🗑 <b>အသုံးပြုခွင့် ပိတ်သိမ်းပြီးပါပြီ:</b> <code>{target_id}</code>", f"🗑 <b>Permission Revoked:</b> <code>{target_id}</code>"),
            parse_mode=ParseMode.HTML,
        )
    else:
        await send_tracked(
            context,
            update.effective_chat.id,
            t_msg(update.effective_chat, f"ℹ️ <code>{target_id}</code> သည် Permitted စာရင်းထဲတွင် မရှိပါ။", f"ℹ️ User <code>{target_id}</code> is not in permitted list."),
            parse_mode=ParseMode.HTML,
        )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
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
        await send_tracked(context, update.effective_chat.id, "⚠️ Usage: <code>/broadcast &lt;message&gt;</code> or reply to a message.", parse_mode=ParseMode.HTML)
        return

    sent, failed = 0, 0
    safe_body = esc(text)
    for cid in list(bot_data_store["known_chats"]):
        try:
            await send_tracked(context, cid, f"📢 <b>Announcement</b>\n\n{safe_body}", parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio.sleep(0.08)
        except Exception:
            failed += 1
    await send_tracked(context, update.effective_chat.id, f"✅ Broadcast complete! Sent: <code>{sent}</code>, Failed: <code>{failed}</code>", parse_mode=ParseMode.HTML)


async def _extract_replied_photo_bytes(message, context: ContextTypes.DEFAULT_TYPE) -> bytes | None:
    rep = message.reply_to_message
    if not rep or not rep.photo:
        return None
    f = await context.bot.get_file(rep.photo[-1].file_id)
    bio = io.BytesIO()
    await f.download_to_memory(bio)
    return bio.getvalue()


async def cmd_solve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    rep = update.message.reply_to_message
    if not rep:
        await reply_tracked(
            update.message,
            t_msg(update.effective_chat, "⚠️ ပုစ္ဆာစာသား သို့မဟုတ် ပုစ္ဆာဓာတ်ပုံကို Reply ထောက်ပြီး <code>/solve</code> ရိုက်ပါ။", "⚠️ Reply to a problem text or photo and send <code>/solve</code>."),
            parse_mode=ParseMode.HTML,
        )
        return

    rep_text = (rep.text or rep.caption or "").strip()
    extra_note = " ".join(context.args).strip()
    img_bytes = await _extract_replied_photo_bytes(update.message, context)

    if not rep_text and not img_bytes:
        await reply_tracked(update.message, t_msg(update.effective_chat, "⚠️ Reply ထောက်ထားသော စာထဲတွင် ပုစ္ဆာ မတွေ့ပါ။", "⚠️ No problem text or photo found in replied message."))
        return

    lang_instruction = "Burmese (မြန်မာဘာသာ)" if get_lang(update.effective_chat) == "mm" else "English"
    wait_msg = await reply_tracked(update.message, t_msg(update.effective_chat, "📐 ပုစ္ဆာကို အဆင့်ဆင့် တွက်ချက်နေပါသည်...", "📐 Solving problem step-by-step..."))
    prompt = (
        f"Solve this problem clearly step-by-step. Explain each step in {lang_instruction} using standard math notation "
        f"and highlight the final answer clearly at the end.\nProblem: {rep_text} {extra_note}".strip()
    )
    try:
        answer, engine = await run_direct_ai("solve", prompt, image_bytes=img_bytes)
        formatted = f"📐 <b>Solution ({esc(engine)})</b>\n\n{ai_to_html(answer[:3800])}"
        await wait_msg.edit_text(formatted, parse_mode=ParseMode.HTML)
    except Exception as e:
        await wait_msg.edit_text(f"⚠️ Solve Error: <code>{esc(e)}</code>", parse_mode=ParseMode.HTML)


async def cmd_stt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    rep = update.message.reply_to_message
    if not rep or not (rep.voice or rep.audio):
        await reply_tracked(
            update.message,
            t_msg(update.effective_chat, "⚠️ Voice Note သို့မဟုတ် Audio ဖိုင်ကို Reply ထောက်ပြီး <code>/stt</code> (သို့) <code>/stt my</code> / <code>/stt en</code> ရိုက်ပါ။", "⚠️ Reply to a voice/audio message with <code>/stt</code> (optional: <code>/stt my</code> or <code>/stt en</code>)."),
            parse_mode=ParseMode.HTML,
        )
        return
    if not GROQ_API_KEY:
        await reply_tracked(update.message, "⚠️ <code>GROQ_API_KEY</code> missing in Render Environment.", parse_mode=ParseMode.HTML)
        return

    lang_hint = context.args[0].lower().strip() if context.args else ""
    wait_msg = await reply_tracked(update.message, t_msg(update.effective_chat, "🎙 အသံဖိုင်ကို စာသားပြောင်းနေပါသည်...", "🎙 Transcribing audio..."))
    try:
        media_obj = rep.voice or rep.audio
        tg_file = await context.bot.get_file(media_obj.file_id)
        bio = io.BytesIO()
        await tg_file.download_to_memory(bio)
        audio_bytes = bio.getvalue()

        form = aiohttp.FormData()
        form.add_field("file", audio_bytes, filename="voice.ogg", content_type="audio/ogg")
        form.add_field("model", "whisper-large-v3-turbo")
        if lang_hint and len(lang_hint) <= 5:
            form.add_field("language", "my" if lang_hint == "mm" else lang_hint)

        session = await get_http_session()
        async with session.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            data=form,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                transcript = data.get("text", "").strip()
                if not transcript:
                    await wait_msg.edit_text(t_msg(update.effective_chat, "⚠️ အသံဖိုင်ထဲတွင် စကားသံ မတွေ့ပါ။", "⚠️ No speech detected in audio."))
                    return
                await wait_msg.edit_text(f"🎙 <b>Transcript:</b>\n\n<blockquote>{esc(transcript[:3800])}</blockquote>", parse_mode=ParseMode.HTML)
            else:
                err = await resp.text()
                await wait_msg.edit_text(f"⚠️ Whisper Error: HTTP {resp.status} - <code>{esc(err[:100])}</code>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await wait_msg.edit_text(f"⚠️ STT Error: <code>{esc(e)}</code>", parse_mode=ParseMode.HTML)


async def cmd_aistatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    providers = [
        ("Google Gemini (2.0 Flash / Lite)", bool(GEMINI_API_KEY)),
        ("Groq (Llama 3.3 70B & Whisper)", bool(GROQ_API_KEY)),
        ("SambaNova (Llama 3.3 70B)", bool(SAMBANOVA_API_KEY)),
        ("GitHub Models (GPT-4o-mini)", bool(GITHUB_TOKEN)),
        ("Cohere (Command-R+ Burmese)", bool(COHERE_API_KEY)),
        ("Serper Google HD Search", bool(SERPER_API_KEY)),
        ("Tavily Web Search AI", bool(TAVILY_API_KEY)),
        ("Microsoft Edge-TTS (Built-in)", True),
    ]
    lines = ["🩺 <b>Mobeho Active AI Engines</b>\n"]
    active_cnt = 0
    for name, ok in providers:
        if ok:
            active_cnt += 1
        lines.append(f"{'🟢' if ok else '🔴'} <b>{esc(name)}</b> — <code>{'READY' if ok else 'No Key'}</code>")
    lines.append(f"\n⚡ Active: <b>{active_cnt}/{len(providers)}</b>")
    await reply_tracked(update.message, "\n".join(lines), parse_mode=ParseMode.HTML)


# ==============================================================================
# 2. SPECIAL COMMANDS (6 COMMANDS: SUPER OWNER + CO-OWNERS /suser)
# ==============================================================================
async def cmd_gsearch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    query = " ".join(context.args).strip()
    if not query:
        await reply_tracked(
            update.message,
            t_msg(update.effective_chat, "⚠️ ရှာဖွေလိုသော အကြောင်းအရာ ရိုက်ပါ: <code>/gsearch iPhone 16 Pro Max</code>", "⚠️ Enter search query: <code>/gsearch iPhone 16 Pro Max</code>"),
            parse_mode=ParseMode.HTML,
        )
        return

    wait_msg = await reply_tracked(update.message, f"🔎 <code>{esc(query)}</code> ...", parse_mode=ParseMode.HTML)
    web_hits, hd_image = await search_hd_google(query)

    snippets = "\n".join([f"- {h['title']}: {h['snippet']} ({h['url']})" for h in web_hits]) or "No direct snippets."
    lang_hint = "Burmese (မြန်မာဘာသာ)" if get_lang(update.effective_chat) == "mm" else "English"
    prompt = (
        f"Create a clean, structured Google Knowledge Card for: '{query}'.\n"
        f"Search Data:\n{snippets}\n\n"
        f"Write in {lang_hint} with:\n"
        f"• 3-4 crisp key facts / specifications\n"
        f"• Concise accurate summary (strictly under 580 characters total)."
    )

    try:
        card_body, _ = await run_direct_ai("gsearch", prompt)
    except Exception:
        card_body = web_hits[0]["snippet"] if web_hits else f"Results for {query}"

    links_str = "\n".join([f"🔗 <a href='{esc(h['url'])}'>{esc(clean_text(h['title'])[:42])}</a>" for h in web_hits[:2] if h.get("url")])
    caption_html = f"🌐 <b>Google Search — {esc(query)}</b>\n\n{ai_to_html(card_body)}"
    if links_str:
        caption_html += f"\n\n{links_str}"

    if hd_image:
        try:
            sent = await update.message.reply_photo(photo=hd_image, caption=caption_html[:1020], parse_mode=ParseMode.HTML)
            track_bot_msg(update.effective_chat.id, sent.message_id)
            try:
                await wait_msg.delete()
            except TelegramError:
                pass
            return
        except TelegramError:
            pass

    await wait_msg.edit_text(caption_html[:3900], parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_tran(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    target_lang = "my" if get_lang(update.effective_chat) == "mm" else "en"
    text_to_tr = ""

    if update.message.reply_to_message:
        text_to_tr = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
        if context.args:
            target_lang = context.args[0].lower().strip("()")
    elif context.args:
        if len(context.args) >= 2 and len(context.args[0].strip("()")) <= 6:
            target_lang = context.args[0].lower().strip("()")
            text_to_tr = " ".join(context.args[1:])
        else:
            text_to_tr = " ".join(context.args)

    if not text_to_tr.strip():
        await reply_tracked(
            update.message,
            t_msg(update.effective_chat, "⚠️ စာကို Reply ထောက်ပြီး <code>/tran mm</code> (သို့) <code>/tran en</code> ရိုက်ပါ။", "⚠️ Reply to a message with <code>/tran en</code> or <code>/tran mm</code>."),
            parse_mode=ParseMode.HTML,
        )
        return

    dest = "Burmese (မြန်မာစကားပြေ သဘာဝကျကျ)" if target_lang in ("mm", "my", "burmese", "myanmar") else target_lang
    try:
        translated, _ = await run_direct_ai("tran", f"Translate naturally into {dest}. Output ONLY the translated text:\n\n{text_to_tr}")
        await reply_tracked(update.message, f"🌐 <b>Translation ({esc(target_lang.upper())}):</b>\n\n<blockquote>{esc(translated)}</blockquote>", parse_mode=ParseMode.HTML)
    except Exception:
        try:
            translated = await async_translate(text_to_tr, target=target_lang)
            await reply_tracked(update.message, f"🌐 <b>Translation ({esc(target_lang.upper())}):</b>\n\n<blockquote>{esc(translated)}</blockquote>", parse_mode=ParseMode.HTML)
        except Exception as e2:
            await reply_tracked(update.message, f"⚠️ Translation Error: <code>{esc(e2)}</code>", parse_mode=ParseMode.HTML)


async def _edge_tts_generate(text: str, gender: str = "female") -> io.BytesIO:
    is_burmese = any("က" <= ch <= "႟" for ch in text)
    if is_burmese:
        voice = "my-MM-NilarNeural" if gender == "female" else "my-MM-ThihaNeural"
    else:
        voice = "en-US-JennyNeural" if gender == "female" else "en-US-GuyNeural"

    try:
        communicate = edge_tts.Communicate(text[:650], voice)
        bio = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                bio.write(chunk["data"])
        if bio.tell() > 500:
            bio.seek(0)
            return bio
    except Exception as e:
        logger.warning(f"Edge-TTS fallback to gTTS: {e}")

    def _gtts_sync():
        lang_code = "my" if is_burmese else "en"
        tts = gTTS(text=text[:500], lang=lang_code)
        b = io.BytesIO()
        tts.write_to_fp(b)
        b.seek(0)
        return b

    return await asyncio.to_thread(_gtts_sync)


async def cmd_tts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    text = " ".join(context.args).strip()
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    if not text:
        await reply_tracked(update.message, "⚠️ Usage: <code>/tts မင်္ဂလာပါ</code> or reply to text.", parse_mode=ParseMode.HTML)
        return
    try:
        bio = await _edge_tts_generate(text, gender="female")
        sent = await update.message.reply_voice(voice=bio)
        track_bot_msg(update.effective_chat.id, sent.message_id)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ TTS Error: <code>{esc(e)}</code>", parse_mode=ParseMode.HTML)


async def cmd_ttsm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    text = " ".join(context.args).strip()
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    if not text:
        await reply_tracked(update.message, "⚠️ Usage: <code>/ttsm မင်္ဂလာပါ</code> or reply to text.", parse_mode=ParseMode.HTML)
        return
    try:
        bio = await _edge_tts_generate(text, gender="male")
        sent = await update.message.reply_voice(voice=bio)
        track_bot_msg(update.effective_chat.id, sent.message_id)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ TTS Error: <code>{esc(e)}</code>", parse_mode=ParseMode.HTML)


async def cmd_grammar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    sentence = " ".join(context.args).strip()
    if not sentence and update.message.reply_to_message:
        sentence = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    if not sentence:
        await reply_tracked(update.message, "⚠️ Usage: <code>/grammar She go to school yesterday</code> or reply to text.", parse_mode=ParseMode.HTML)
        return

    explain_lang = "Burmese (မြန်မာလို ရှင်းပြချက်)" if get_lang(update.effective_chat) == "mm" else "English"
    prompt = (
        f"Check and correct the English grammar of this text. Provide:\n"
        f"1) ✅ Corrected Sentence\n"
        f"2) 💡 Natural/Native Version\n"
        f"3) 📝 Brief explanation in {explain_lang}.\n\nText: {sentence}"
    )
    try:
        res, engine = await run_direct_ai("grammar", prompt)
        await reply_tracked(update.message, f"✍️ <b>Grammar Check ({esc(engine)})</b>\n\n{ai_to_html(res[:3800])}", parse_mode=ParseMode.HTML)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Grammar Error: <code>{esc(e)}</code>", parse_mode=ParseMode.HTML)


def _parse_afk_duration(raw: str) -> tuple[float | None, str]:
    """Parses strings like '3 Mins', '10 hours', '1d 2h' into (until_timestamp, clean_label)."""
    if not raw.strip():
        return None, ""
    pattern = re.compile(r"(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)", re.I)
    matches = pattern.findall(raw)
    if not matches:
        return None, raw.strip()
    total_seconds = 0.0
    for val_str, unit in matches:
        val = float(val_str)
        u = unit.lower()
        if u.startswith("s"):
            total_seconds += val
        elif u.startswith("m"):
            total_seconds += val * 60
        elif u.startswith("h"):
            total_seconds += val * 3600
        elif u.startswith("d"):
            total_seconds += val * 86400
    if total_seconds <= 0:
        return None, raw.strip()
    return time.time() + total_seconds, raw.strip()


def _format_remaining(seconds_left: float, lang: str) -> str:
    sec = max(1, int(seconds_left))
    hrs, rem = divmod(sec, 3600)
    mins, secs = divmod(rem, 60)
    if lang == "mm":
        parts = []
        if hrs:
            parts.append(f"{hrs} နာရီ")
        if mins:
            parts.append(f"{mins} မိနစ်")
        if secs and not hrs:
            parts.append(f"{secs} စက္ကန့်")
        return " ".join(parts)
    parts = []
    if hrs:
        parts.append(f"{hrs}h")
    if mins:
        parts.append(f"{mins}m")
    if secs and not hrs:
        parts.append(f"{secs}s")
    return " ".join(parts)


async def cmd_afk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    uid = update.effective_user.id
    raw_arg = " ".join(context.args).strip()
    until_ts, label = _parse_afk_duration(raw_arg)
    reason = label or t_msg(update.effective_chat, "အနားယူနေပါသည်", "Away")

    afk_users[uid] = {
        "reason": reason,
        "until": until_ts,
        "since": time.time(),
        "name": update.effective_user.first_name,
    }
    msg = t_msg(
        update.effective_chat,
        f"💤 <b>{esc(update.effective_user.first_name)}</b> သည် AFK ဖြစ်သွားပါပြီ။\n⏱ ကြာချိန်/အကြောင်းပြချက်: <i>{esc(reason)}</i>",
        f"💤 <b>{esc(update.effective_user.first_name)}</b> is now AFK.\n⏱ Duration/Reason: <i>{esc(reason)}</i>",
    )
    await reply_tracked(update.message, msg, parse_mode=ParseMode.HTML)


# ==============================================================================
# 3. PERMITTED COMMANDS (9 COMMANDS: SUPER + SPECIAL + PERMITTED)
# ==============================================================================
async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    cid = str(update.effective_chat.id)
    arg = context.args[0].lower() if context.args else ""

    if arg in ("mm", "my", "myanmar", "burmese"):
        new_lang = "mm"
    elif arg in ("en", "eng", "english"):
        new_lang = "en"
    else:
        # Automatic instant toggle when sending /lang alone
        curr = get_lang(update.effective_chat)
        new_lang = "en" if curr == "mm" else "mm"

    bot_data_store["chat_langs"][cid] = new_lang
    await save_to_jsonbin(force=True)

    if new_lang == "mm":
        await reply_tracked(update.message, "🇲🇲 Bot စနစ်တစ်ခုလုံးကို <b>မြန်မာဘာသာ</b> သို့ ပြောင်းလဲလိုက်ပါပြီ။", parse_mode=ParseMode.HTML)
    else:
        await reply_tracked(update.message, "🇬🇧 Entire Bot system switched to <b>English</b>.", parse_mode=ParseMode.HTML)


async def _spam_worker(context: ContextTypes.DEFAULT_TYPE, chat_id: int, target_user):
    mention = f"<a href='tg://user?id={target_user.id}'>{esc(target_user.first_name)}</a>"
    idx = 0
    try:
        while True:
            texts = bot_data_store["spam_texts"]
            if not texts:
                break
            line = esc(texts[idx % len(texts)])
            idx += 1
            try:
                await send_tracked(context, chat_id, f"{mention} {line}", parse_mode=ParseMode.HTML)
            except RetryAfter as ra:
                await asyncio.sleep(ra.retry_after + 1)
                continue
            except TelegramError:
                break
            await asyncio.sleep(2.5)
    except asyncio.CancelledError:
        pass


async def cmd_selsen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await reply_tracked(update.message, t_msg(update.effective_chat, "⚠️ Reply ထောက်ပြီး <code>/selsen</code> ရိုက်ပါ။", "⚠️ Reply to a user and send <code>/selsen</code>."), parse_mode=ParseMode.HTML)
        return
    if not bot_data_store["spam_texts"]:
        await reply_tracked(update.message, t_msg(update.effective_chat, "⚠️ စာသားစာရင်း အလွတ်ဖြစ်နေပါတယ်။ <code>/addtext</code> နဲ့ အရင်ထည့်ပါ။", "⚠️ Text list is empty. Add text with <code>/addtext</code>."), parse_mode=ParseMode.HTML)
        return
    if chat_id in active_spam_tasks and not active_spam_tasks[chat_id].done():
        active_spam_tasks[chat_id].cancel()
    target_user = update.message.reply_to_message.from_user
    active_spam_tasks[chat_id] = asyncio.create_task(_spam_worker(context, chat_id, target_user))


async def cmd_yat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    if chat_id in active_spam_tasks and not active_spam_tasks[chat_id].done():
        active_spam_tasks[chat_id].cancel()
        del active_spam_tasks[chat_id]
        await reply_tracked(update.message, t_msg(update.effective_chat, "🛑 ရပ်တန့်လိုက်ပါပြီ။", "🛑 Stopped."))
    else:
        await reply_tracked(update.message, t_msg(update.effective_chat, "ℹ️ လက်ရှိ ပို့နေသော စာမရှိပါ။", "ℹ️ No active loop running."))


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
    confirm = await context.bot.send_message(chat_id=chat_id, text=f"🧹 Deleted <code>{deleted}</code> bot messages.", parse_mode=ParseMode.HTML)
    await asyncio.sleep(3)
    try:
        await confirm.delete()
    except TelegramError:
        pass


async def cmd_addtext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    text = " ".join(context.args).strip()
    if not text:
        await reply_tracked(update.message, t_msg(update.effective_chat, "⚠️ ထည့်ချင်တဲ့ စာသား ရိုက်ပါ: <code>/addtext မင်္ဂလာပါ</code>", "⚠️ Usage: <code>/addtext Hello</code>"), parse_mode=ParseMode.HTML)
        return
    bot_data_store["spam_texts"].append(text)
    await save_to_jsonbin(force=True)
    await reply_tracked(
        update.message,
        t_msg(update.effective_chat, f"✅ စာသားအသစ် ထည့်ပြီးပါပြီ (စုစုပေါင်း: <code>{len(bot_data_store['spam_texts'])}</code> ကြောင်း)", f"✅ Added! Total lines: <code>{len(bot_data_store['spam_texts'])}</code>"),
        parse_mode=ParseMode.HTML,
    )


async def cmd_listtext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    texts = bot_data_store["spam_texts"]
    if not texts:
        await reply_tracked(update.message, t_msg(update.effective_chat, "📭 သိမ်းထားသော စာသား မရှိပါ။", "📭 No saved texts."))
        return
    body = "\n".join([f"<code>{i+1}.</code> {esc(t)}" for i, t in enumerate(texts)])
    header = t_msg(update.effective_chat, "📜 <b>သိမ်းထားသော စာသားများ:</b>\n", "📜 <b>Saved Text List:</b>\n")
    await reply_tracked(update.message, header + body, parse_mode=ParseMode.HTML)


async def cmd_edittext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await reply_tracked(update.message, t_msg(update.effective_chat, "⚠️ အသုံးပြုပုံ: <code>/edittext &lt;နံပါတ်&gt; &lt;စာသားအသစ်&gt;</code>", "⚠️ Usage: <code>/edittext &lt;num&gt; &lt;new text&gt;</code>"), parse_mode=ParseMode.HTML)
        return
    idx = int(context.args[0]) - 1
    new_text = " ".join(context.args[1:]).strip()
    if idx < 0 or idx >= len(bot_data_store["spam_texts"]):
        await reply_tracked(update.message, t_msg(update.effective_chat, "❌ နံပါတ် မှားယွင်းနေပါသည်။", "❌ Invalid index."))
        return
    bot_data_store["spam_texts"][idx] = new_text
    await save_to_jsonbin(force=True)
    await reply_tracked(update.message, t_msg(update.effective_chat, f"✏️ နံပါတ် <code>{idx+1}</code> ကို ပြင်ဆင်ပြီးပါပြီ။", f"✏️ Updated item <code>{idx+1}</code>."), parse_mode=ParseMode.HTML)


async def cmd_removetext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await reply_tracked(update.message, t_msg(update.effective_chat, "⚠️ အသုံးပြုပုံ: <code>/removetext &lt;နံပါတ်&gt;</code>", "⚠️ Usage: <code>/removetext &lt;num&gt;</code>"), parse_mode=ParseMode.HTML)
        return
    idx = int(context.args[0]) - 1
    if idx < 0 or idx >= len(bot_data_store["spam_texts"]):
        await reply_tracked(update.message, t_msg(update.effective_chat, "❌ နံပါတ် မှားယွင်းနေပါသည်။", "❌ Invalid index."))
        return
    removed = bot_data_store["spam_texts"].pop(idx)
    await save_to_jsonbin(force=True)
    await reply_tracked(update.message, t_msg(update.effective_chat, f"🗑 ဖျက်လိုက်သော စာသား: <code>{esc(removed)}</code>", f"🗑 Removed: <code>{esc(removed)}</code>"), parse_mode=ParseMode.HTML)


async def cmd_cleartext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    bot_data_store["spam_texts"] = []
    await save_to_jsonbin(force=True)
    await reply_tracked(update.message, t_msg(update.effective_chat, "🗑 စာသားအားလုံးကို ဖျက်သိမ်းပြီးပါပြီ။", "🗑 Cleared all saved texts."))


# ==============================================================================
# 4. PUBLIC / EVERYONE COMMANDS (10 COMMANDS)
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


async def cmd_checkpf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target = update.message.reply_to_message.from_user
    else:
        target = update.effective_user

    year_est = estimate_account_year(target.id)
    if is_super(target.id):
        role = "👑 Super Owner"
    elif is_special(target.id):
        role = "⭐ Co-Owner"
    elif is_permitted(target.id):
        role = "🔐 Permitted"
    else:
        role = "👤 Member"

    uname = f"@{esc(target.username)}" if target.username else t_msg(update.effective_chat, "မရှိပါ", "None")
    text = t_msg(
        update.effective_chat,
        f"🪪 <b>ပရိုဖိုင် အချက်အလက် (Profile Check)</b>\n\n"
        f"• အမည်: <b>{esc(target.full_name)}</b>\n"
        f"• Username: {uname}\n"
        f"• User ID: <code>{target.id}</code>\n"
        f"• အဆင့်: {role}\n"
        f"• ဖွင့်ခဲ့သည့် ခန့်မှန်းနှစ်: <b>{year_est}</b>",
        f"🪪 <b>Profile Check</b>\n\n"
        f"• Name: <b>{esc(target.full_name)}</b>\n"
        f"• Username: {uname}\n"
        f"• User ID: <code>{target.id}</code>\n"
        f"• Role: {role}\n"
        f"• Est. Created: <b>{year_est}</b>",
    )
    await reply_tracked(update.message, text, parse_mode=ParseMode.HTML)


def _build_wiki_card_ui(key: str, page_idx: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    entry = wiki_cache[key]
    full_text = entry["full_text"]
    page_size = 750
    chunks = [full_text[i: i + page_size] for i in range(0, len(full_text), page_size)] or [full_text]
    total_pages = len(chunks)
    page_idx = max(0, min(page_idx, total_pages - 1))

    body = esc(chunks[page_idx])
    caption = (
        f"📚 <b>WIKIPEDIA — {esc(entry['title'])}</b>\n"
        f"<i>Free Encyclopedia • Page {page_idx + 1}/{total_pages}</i>\n\n"
        f"<blockquote>{body}</blockquote>"
    )

    nav_row = []
    if page_idx > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"wiki_p_{key}_{page_idx - 1}"))
    if page_idx < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"wiki_p_{key}_{page_idx + 1}"))

    rows = []
    if nav_row:
        rows.append(nav_row)
    rows.append([InlineKeyboardButton("🌐 Open Wikipedia Page", url=entry["url"])])
    return caption, InlineKeyboardMarkup(rows)


async def cmd_wiki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()
    if not query:
        await reply_tracked(
            update.message,
            t_msg(update.effective_chat, "⚠️ Wikipedia ရှာရန်: <code>/wiki Myanmar</code>", "⚠️ Usage: <code>/wiki Myanmar</code>"),
            parse_mode=ParseMode.HTML,
        )
        return

    wiki_lang = "my" if any("က" <= ch <= "႟" for ch in query) else "en"
    session = await get_http_session()
    try:
        search_url = f"https://{wiki_lang}.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query)}&format=json&srlimit=1"
        async with session.get(search_url, timeout=aiohttp.ClientTimeout(total=8)) as sresp:
            sdata = await sresp.json()
            hits = sdata.get("query", {}).get("search", [])
            if not hits:
                await reply_tracked(update.message, f"❌ <code>{esc(query)}</code> not found on Wikipedia.", parse_mode=ParseMode.HTML)
                return
            exact_title = hits[0]["title"]

        # Fetch full extract + HD pageimage via MediaWiki API for complete Wikipedia UI reading
        ext_url = (
            f"https://{wiki_lang}.wikipedia.org/w/api.php?action=query&prop=extracts|pageimages|info"
            f"&explaintext=1&exchars=3200&piprop=original|thumbnail&pithumbsize=1000&inprop=url"
            f"&titles={urllib.parse.quote(exact_title)}&format=json"
        )
        async with session.get(ext_url, timeout=aiohttp.ClientTimeout(total=9)) as eresp:
            edata = await eresp.json()
            pages = edata.get("query", {}).get("pages", {})
            page_obj = next(iter(pages.values()))
            title = page_obj.get("title", exact_title)
            full_text = clean_text(page_obj.get("extract", "")) or "No article content available."
            page_url = page_obj.get("fullurl", f"https://{wiki_lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}")
            thumb_url = (page_obj.get("original") or page_obj.get("thumbnail") or {}).get("source", "")

            cache_key = f"{update.effective_chat.id}_{int(time.time()) % 100000}"
            wiki_cache[cache_key] = {
                "title": title,
                "full_text": full_text,
                "thumb": thumb_url,
                "url": page_url,
                "lang": wiki_lang,
            }
            while len(wiki_cache) > 80:
                wiki_cache.pop(next(iter(wiki_cache)))

            caption, kb = _build_wiki_card_ui(cache_key, page_idx=0)
            if thumb_url:
                try:
                    sent = await update.message.reply_photo(photo=thumb_url, caption=caption[:1020], reply_markup=kb, parse_mode=ParseMode.HTML)
                    track_bot_msg(update.effective_chat.id, sent.message_id)
                    return
                except TelegramError:
                    pass
            await reply_tracked(update.message, caption, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Wiki Error: <code>{esc(e)}</code>", parse_mode=ParseMode.HTML)


async def cmd_gevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    year = datetime.now(timezone.utc).year
    lines = [
        f"📅 <b>မြန်မာ့အဓိက နေ့ထူးနေ့မြတ်များ ({year})</b>",
        "• <b>ဇန်နဝါရီ ၄</b> — လွတ်လပ်ရေးနေ့ 🇲🇲",
        "• <b>ဖေဖော်ဝါရီ ၁၂</b> — ပြည်ထောင်စုနေ့ 🇲🇲",
        "• <b>ဧပြီ ၁၃-၁၆</b> — မဟာသင်္ကြန်ပွဲတော် 💦",
        "• <b>ဇူလိုင် ၁၉</b> — အာဇာနည်နေ့ 🕯",
        "• <b>သီတင်းကျွတ် / တန်ဆောင်တိုင်</b> — မီးထွန်းပွဲတော်များ 🏮\n",
    ]
    try:
        session = await get_http_session()
        async with session.get("https://date.nager.at/api/v3/NextPublicHolidaysWorldwide", timeout=aiohttp.ClientTimeout(total=7)) as resp:
            if resp.status == 200:
                holidays = await resp.json()
                lines.append("🌍 <b>Upcoming Global Holidays (Live):</b>")
                seen = set()
                for h in holidays:
                    key = f"{h['date']}_{h['name']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    lines.append(f"• <code>{esc(h['date'])}</code> ({esc(h['countryCode'])}) — <b>{esc(h['name'])}</b>")
                    if len(seen) >= 7:
                        break
    except Exception:
        pass
    await reply_tracked(update.message, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_qhour(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hq = await refresh_hourly_quote_if_needed(force=False)
    lang = get_lang(update.effective_chat)
    now_mmt = datetime.now(timezone(timedelta(hours=6, minutes=30)))
    hour_label = now_mmt.strftime("%I:00 %p")

    if lang == "mm":
        text = (
            f"🕰 <b>နာရီစဉ် မှတ်သားဖွယ် စကားစု ({hour_label})</b>\n\n"
            f"<blockquote>“{esc(hq['mm'])}”</blockquote>\n"
            f"— <b>{esc(hq['author'])}</b>"
        )
    else:
        text = (
            f"🕰 <b>Hourly Quote ({hour_label})</b>\n\n"
            f"<blockquote>“{esc(hq['en'])}”</blockquote>\n"
            f"— <b>{esc(hq['author'])}</b>"
        )
    await reply_tracked(update.message, text, parse_mode=ParseMode.HTML)


async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [t_msg(update.effective_chat, "💱 <b>Live ငွေလဲနှုန်းနှင့် Crypto ပေါက်ဈေး</b>\n", "💱 <b>Live Exchange & Crypto Rates</b>\n")]
    session = await get_http_session()
    try:
        async with session.get("https://open.er-api.com/v6/latest/USD", timeout=aiohttp.ClientTimeout(total=7)) as r1:
            fx = await r1.json()
            rates = fx.get("rates", {})
            lines.append("💵 <b>Fiat (1 USD Base):</b>")
            lines.append(f"• 🇲🇲 MMK: <code>{rates.get('MMK', 2100.0):,.2f} MMK</code>")
            lines.append(f"• 🇹🇭 THB: <code>{rates.get('THB', 34.0):,.2f} THB</code>")
            lines.append(f"• 🇸🇬 SGD: <code>{rates.get('SGD', 1.30):,.2f} SGD</code>")
            lines.append(f"• 🇯🇵 JPY: <code>{rates.get('JPY', 155.0):,.2f} JPY</code>")
            lines.append(f"• 🇨🇳 CNY: <code>{rates.get('CNY', 7.20):,.2f} CNY</code>\n")
    except Exception as e:
        lines.append(f"⚠️ Fiat rate error: <code>{esc(e)}</code>\n")

    try:
        symbols = '["TONUSDT","BTCUSDT","ETHUSDT","SOLUSDT"]'
        bn_url = f"https://api.binance.com/api/v3/ticker/price?symbols={urllib.parse.quote(symbols)}"
        async with session.get(bn_url, timeout=aiohttp.ClientTimeout(total=7)) as r2:
            if r2.status == 200:
                plist = await r2.json()
                pmap = {item["symbol"]: float(item["price"]) for item in plist}
                lines.append("🪙 <b>Crypto Live (Binance):</b>")
                if "TONUSDT" in pmap:
                    lines.append(f"• 💎 TON: <code>${pmap['TONUSDT']:,.3f}</code>")
                if "BTCUSDT" in pmap:
                    lines.append(f"• ₿ BTC: <code>${pmap['BTCUSDT']:,.2f}</code>")
                if "ETHUSDT" in pmap:
                    lines.append(f"• Ξ ETH: <code>${pmap['ETHUSDT']:,.2f}</code>")
                if "SOLUSDT" in pmap:
                    lines.append(f"• ◎ SOL: <code>${pmap['SOLUSDT']:,.2f}</code>")
            else:
                raise RuntimeError(f"HTTP {r2.status}")
    except Exception:
        try:
            async with session.get("https://api.coinbase.com/v2/exchange-rates?currency=USD", timeout=aiohttp.ClientTimeout(total=7)) as r3:
                cb = await r3.json()
                cr = cb["data"]["rates"]
                lines.append("🪙 <b>Crypto Live (Coinbase):</b>")
                lines.append(f"• 💎 TON: <code>${1/float(cr['TON']):,.3f}</code>")
                lines.append(f"• ₿ BTC: <code>${1/float(cr['BTC']):,.2f}</code>")
                lines.append(f"• Ξ ETH: <code>${1/float(cr['ETH']):,.2f}</code>")
        except Exception as e2:
            lines.append(f"⚠️ Crypto feed unavailable: <code>{esc(e2)}</code>")
    await reply_tracked(update.message, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = " ".join(context.args).strip()
    if not data and update.message.reply_to_message:
        data = update.message.reply_to_message.text or ""
    if not data:
        await reply_tracked(update.message, t_msg(update.effective_chat, "⚠️ QR ပြောင်းရန်: <code>/qr https://t.me</code>", "⚠️ Usage: <code>/qr https://t.me</code>"), parse_mode=ParseMode.HTML)
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
    sent = await update.message.reply_photo(photo=bio, caption=f"📱 <b>QR Code:</b> <code>{esc(data[:80])}</code>", parse_mode=ParseMode.HTML)
    track_bot_msg(update.effective_chat.id, sent.message_id)


def _safe_calculate(expression: str):
    expression = expression.replace("^", "**")
    if len(expression) > 120:
        raise ValueError("expression too long")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 40:
        raise ValueError("expression too complex")

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            operand = visit(node.operand)
            value = operand if isinstance(node.op, ast.UAdd) else -operand
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                value = left * right
            elif isinstance(node.op, ast.Div):
                value = left / right
            elif isinstance(node.op, ast.FloorDiv):
                value = left // right
            elif isinstance(node.op, ast.Mod):
                value = left % right
            else:
                if abs(right) > 100:
                    raise ValueError("exponent too large")
                value = left ** right
        else:
            raise ValueError("unsupported expression")

        if isinstance(value, complex) or not isinstance(value, (int, float)):
            raise ValueError("invalid result")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite result")
        if abs(value) > 10 ** 100:
            raise ValueError("result too large")
        return value

    return visit(tree)


async def cmd_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expr = " ".join(context.args).strip()
    if not expr:
        await reply_tracked(update.message, "⚠️ Usage: <code>/calc (2500*4)+1500</code>", parse_mode=ParseMode.HTML)
        return
    if not re.fullmatch(r"[0-9+\-*/().%^ ]+", expr):
        await reply_tracked(update.message, t_msg(update.effective_chat, "❌ ဂဏန်းနှင့် သင်္ချာသင်္ကေတများသာ ရိုက်ထည့်ပါ။", "❌ Numbers and math operators only."))
        return
    try:
        result = _safe_calculate(expr)
        shown = f"{result:,}" if isinstance(result, int) else f"{result:,.10g}"
        await reply_tracked(update.message, f"🧮 <code>{esc(expr)}</code> = <b><code>{esc(shown)}</code></b>", parse_mode=ParseMode.HTML)
    except Exception:
        await reply_tracked(update.message, t_msg(update.effective_chat, "❌ တွက်ချက်၍ မရပါ။ ပုစ္ဆာပုံစံ ပြန်စစ်ပါ။", "❌ Invalid calculation."))


async def cmd_dict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    word = context.args[0].strip().lower() if context.args else ""
    if not word:
        await reply_tracked(update.message, "⚠️ Usage: <code>/dict resilience</code>", parse_mode=ParseMode.HTML)
        return
    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}"
    defn_text, pos, phonetic = "", "noun", ""
    session = await get_http_session()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=7)) as resp:
            if resp.status == 200:
                entry = (await resp.json())[0]
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
        await reply_tracked(update.message, f"❌ Word <code>{esc(word)}</code> not found.", parse_mode=ParseMode.HTML)
        return
    lines = [f"📖 <b>{esc(word.capitalize())}</b> <code>{esc(phonetic)}</code>", f"🇲🇲 မြန်မာအဓိပ္ပာယ်: <b>{esc(mm_word)}</b>"]
    if defn_text:
        lines.append(f"🏷 Type: <i>{esc(pos)}</i>")
        lines.append(f"📝 Definition: {esc(defn_text)}")
    await reply_tracked(update.message, "\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime_sec = int(time.time() - START_TIME)
    hrs, rem = divmod(uptime_sec, 3600)
    mins, secs = divmod(rem, 60)
    lang_mode = get_lang(update.effective_chat).upper()
    text = t_msg(
        update.effective_chat,
        f"📊 <b>Mobeho Bot စာရင်းချုပ် (Dashboard)</b>\n\n"
        f"• ⏱ Uptime: <code>{hrs}h {mins}m {secs}s</code>\n"
        f"• 🌐 Language Mode: <code>{lang_mode}</code>\n"
        f"• ⭐ Co-Owners: <code>{len(bot_data_store['special_users'])}</code> ဦး\n"
        f"• 🔐 Permitted Users: <code>{len(bot_data_store['permitted_users'])}</code> ဦး\n"
        f"• 💬 ချိတ်ဆက်ထားသော Chat: <code>{len(bot_data_store['known_chats'])}</code> ခု",
        f"📊 <b>Mobeho Bot Dashboard</b>\n\n"
        f"• ⏱ Uptime: <code>{hrs}h {mins}m {secs}s</code>\n"
        f"• 🌐 Language Mode: <code>{lang_mode}</code>\n"
        f"• ⭐ Co-Owners: <code>{len(bot_data_store['special_users'])}</code>\n"
        f"• 🔐 Permitted Users: <code>{len(bot_data_store['permitted_users'])}</code>\n"
        f"• 💬 Known Chats: <code>{len(bot_data_store['known_chats'])}</code>",
    )
    await reply_tracked(update.message, text, parse_mode=ParseMode.HTML)


async def cmd_cmlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = get_lang(update.effective_chat)
    sections = []

    if is_super(uid):
        sections.append(
            "👑 <b>1. Super Owner Commands (8)</b>\n"
            "• <code>/suser a</code> | <code>/suser d</code> — Co-Owner ခန့်/ဖြုတ်\n"
            "• <code>/userlist</code> — Co-Owners & Permitted စာရင်းကြည့်ရန်\n"
            "• <code>/apermit</code> | <code>/rpermit</code> — အသုံးပြုခွင့် ပေး/ပိတ်\n"
            "• <code>/broadcast</code> — Chat အားလုံးသို့ ကြေညာစာပို့ရန်\n"
            "• <code>/solve</code> — Reply ထောက်ပြီး ပုစ္ဆာတွက်ရန်\n"
            "• <code>/stt</code> — Reply ထောက်ပြီး အသံမှ စာပြောင်းရန်\n"
            "• <code>/aistatus</code> — ချိတ်ဆက်ထားသော AI များ စစ်ရန်"
            if lang == "mm" else
            "👑 <b>1. Super Owner Commands (8)</b>\n"
            "• <code>/suser a</code> | <code>/suser d</code> — Add/remove Co-Owner\n"
            "• <code>/userlist</code> — View Co-Owners & Permitted list\n"
            "• <code>/apermit</code> | <code>/rpermit</code> — Grant/revoke permission\n"
            "• <code>/broadcast</code> — Broadcast to all chats\n"
            "• <code>/solve</code> — Reply to solve problem step-by-step\n"
            "• <code>/stt</code> — Reply to transcribe voice/audio\n"
            "• <code>/aistatus</code> — Check active AI engines"
        )

    if is_special(uid):
        sections.append(
            "⭐ <b>2. Special Commands (6)</b>\n"
            "• <code>/gsearch &lt;query&gt;</code> — Google Search + HD ပုံရှာရန်\n"
            "• <code>/tran &lt;lang&gt;</code> — Reply ထောက်ပြီး ဘာသာပြန်ရန်\n"
            "• <code>/tts</code> | <code>/ttsm</code> — မိန်းကလေး/ယောက်ျားလေး အသံပြောင်း\n"
            "• <code>/grammar</code> — English Grammar စစ်ဆေးပြင်ဆင်ရန်\n"
            "• <code>/afk &lt;time&gt;</code> — အချိန်သတ်မှတ်ပြီး AFK ထားရန်"
            if lang == "mm" else
            "⭐ <b>2. Special Commands (6)</b>\n"
            "• <code>/gsearch &lt;query&gt;</code> — Google Search + HD photo\n"
            "• <code>/tran &lt;lang&gt;</code> — Reply & translate text\n"
            "• <code>/tts</code> | <code>/ttsm</code> — Female / Male voice TTS\n"
            "• <code>/grammar</code> — English grammar check\n"
            "• <code>/afk &lt;time&gt;</code> — Set timed AFK status"
        )

    if is_permitted(uid):
        sections.append(
            "🔐 <b>3. Permitted Commands (9)</b>\n"
            "• <code>/lang</code> — Bot ဘာသာစကား (မြန်မာ/English) ပြောင်းရန်\n"
            "• <code>/selsen</code> | <code>/yat</code> | <code>/delmsg</code> — စာပို့/ရပ်/ဖျက်\n"
            "• <code>/addtext</code> | <code>/listtext</code> | <code>/edittext</code>\n"
            "• <code>/removetext</code> | <code>/cleartext</code> — စာသားစာရင်း စီမံရန်"
            if lang == "mm" else
            "🔐 <b>3. Permitted Commands (9)</b>\n"
            "• <code>/lang</code> — Toggle bot language (mm/en)\n"
            "• <code>/selsen</code> | <code>/yat</code> | <code>/delmsg</code> — Loop & cleanup\n"
            "• <code>/addtext</code> | <code>/listtext</code> | <code>/edittext</code>\n"
            "• <code>/removetext</code> | <code>/cleartext</code> — Manage text list"
        )

    sections.append(
        "🌍 <b>4. Public Commands (10)</b>\n"
        "• <code>/cmlist</code> — Command စာရင်းကြည့်ရန်\n"
        "• <code>/wiki &lt;topic&gt;</code> — Wikipedia စာမျက်နှာ အပြည့်အစုံဖတ်ရန်\n"
        "• <code>/checkpf</code> — Reply ထောက်ပြီး ID/Username စစ်ရန်\n"
        "• <code>/gevent</code> — နေ့ထူးနေ့မြတ်များ ကြည့်ရန်\n"
        "• <code>/qhour</code> — တစ်နာရီတစ်ကြိမ် Quote ကြည့်ရန်\n"
        "• <code>/rate</code> — ငွေလဲနှုန်းနှင့် Crypto ဈေးကြည့်ရန်\n"
        "• <code>/qr &lt;text/link&gt;</code> — QR Code ပုံထုတ်ရန်\n"
        "• <code>/calc &lt;expr&gt;</code> — ဂဏန်းတွက်စက်\n"
        "• <code>/dict &lt;word&gt;</code> — English-Myanmar အဘိဓာန်\n"
        "• <code>/stats</code> — Bot စာရင်းချုပ် ကြည့်ရန်"
        if lang == "mm" else
        "🌍 <b>4. Public Commands (10)</b>\n"
        "• <code>/cmlist</code> — View command list\n"
        "• <code>/wiki &lt;topic&gt;</code> — Full Wikipedia article reader\n"
        "• <code>/checkpf</code> — Reply to check ID & username\n"
        "• <code>/gevent</code> — Myanmar & Global holidays\n"
        "• <code>/qhour</code> — Hourly quote (auto mm/en)\n"
        "• <code>/rate</code> — Live exchange & crypto rates\n"
        "• <code>/qr &lt;text/link&gt;</code> — Generate QR code\n"
        "• <code>/calc &lt;expr&gt;</code> — Math calculator\n"
        "• <code>/dict &lt;word&gt;</code> — English-Myanmar dictionary\n"
        "• <code>/stats</code> — Bot uptime & stats"
    )

    header = "🤖 <b>Mobeho Bot — Command Menu (33)</b>\n\n"
    await reply_tracked(update.message, header + "\n\n".join(sections), parse_mode=ParseMode.HTML)


# ==============================================================================
# 5. INLINE CALLBACK ROUTER (WIKIPEDIA UI PAGINATION)
# ==============================================================================
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("wiki_p_"):
        rest = data[len("wiki_p_"):]
        key, page_str = rest.rsplit("_", 1)
        if key not in wiki_cache:
            await query.answer("⚠️ Wikipedia session expired. Search again.", show_alert=True)
            return
        await query.answer()
        caption, kb = _build_wiki_card_ui(key, page_idx=int(page_str))
        try:
            if query.message.photo:
                await query.edit_message_caption(caption=caption[:1020], reply_markup=kb, parse_mode=ParseMode.HTML)
            else:
                await query.edit_message_text(text=caption[:3900], reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except TelegramError:
            pass


# ==============================================================================
# 6. GENERAL MESSAGE LISTENER (TIMED AFK & CHAT TRACKING)
# ==============================================================================
async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        track_chat(update.effective_chat.id)


async def general_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    chat = update.effective_chat
    user = update.effective_user
    track_chat(chat.id)
    text = (update.message.text or "").strip()
    if not text:
        return

    now = time.time()
    lang = get_lang(chat)

    # Auto-expire AFK if duration passed
    for uid in list(afk_users.keys()):
        until_ts = afk_users[uid].get("until")
        if until_ts and now >= until_ts:
            del afk_users[uid]

    # Remove sender's AFK when they speak
    if user.id in afk_users and not text.startswith("/afk"):
        del afk_users[user.id]
        msg = t_msg(chat, f"👋 <b>{esc(user.first_name)}</b> ပြန်လည်ရောက်ရှိလာပါပြီ (AFK ဖြုတ်ပြီး)။", f"👋 <b>{esc(user.first_name)}</b> is back (AFK removed).")
        await reply_tracked(update.message, msg, parse_mode=ParseMode.HTML)

    # Notify if replying to an AFK user
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        replied_uid = update.message.reply_to_message.from_user.id
        if replied_uid in afk_users and replied_uid != user.id:
            info = afk_users[replied_uid]
            until_ts = info.get("until")
            if until_ts and until_ts > now:
                rem_str = _format_remaining(until_ts - now, lang)
                msg = t_msg(
                    chat,
                    f"💤 <b>{esc(info['name'])}</b> သည် AFK ဖြစ်နေပါသည်\n⏳ ကျန်ရှိချိန်: <code>{esc(rem_str)}</code> ({esc(info['reason'])})",
                    f"💤 <b>{esc(info['name'])}</b> is currently AFK\n⏳ Remaining: <code>{esc(rem_str)}</code> ({esc(info['reason'])})",
                )
            else:
                mins = max(1, int((now - info["since"]) // 60))
                msg = t_msg(
                    chat,
                    f"💤 <b>{esc(info['name'])}</b> သည် AFK ဖြစ်နေပါသည် ({mins} မိနစ်ကြာ)\n📝 အကြောင်းပြချက်: <i>{esc(info['reason'])}</i>",
                    f"💤 <b>{esc(info['name'])}</b> is AFK ({mins}m ago)\n📝 Reason: <i>{esc(info['reason'])}</i>",
                )
            await reply_tracked(update.message, msg, parse_mode=ParseMode.HTML)


# ==============================================================================
# 7. STARTUP & RENDER WEB HEALTH SERVER
# ==============================================================================
async def post_init_setup(app: Application):
    await start_health_server()
    await load_from_jsonbin()
    await refresh_hourly_quote_if_needed(force=False)

    # Hide command menu in groups; set scoped DM menus
    await app.bot.set_my_commands([], scope=BotCommandScopeAllGroupChats())

    public_dm = [
        BotCommand("cmlist", "View available commands"),
        BotCommand("wiki", "Wikipedia full article viewer"),
        BotCommand("checkpf", "Check user ID & username"),
        BotCommand("gevent", "Myanmar & Global live holidays"),
        BotCommand("qhour", "Hourly quote (auto mm/en)"),
        BotCommand("rate", "Live currency & crypto rates"),
        BotCommand("qr", "Generate HD QR code"),
        BotCommand("calc", "Math calculator"),
        BotCommand("dict", "English-Myanmar dictionary"),
        BotCommand("stats", "Bot uptime & live stats"),
    ]
    await app.bot.set_my_commands(public_dm, scope=BotCommandScopeAllPrivateChats())

    for uid in set(bot_data_store["permitted_users"] + bot_data_store["special_users"] + ([OWNER_ID] if OWNER_ID else [])):
        await sync_user_dm_menu(app.bot, uid)

    asyncio.create_task(periodic_sync_worker())
    asyncio.create_task(hourly_quote_worker())
    logger.info("Mobeho Bot (33 Commands • Direct Pinpoint AI • Clean HTML UI) ready.")


async def post_shutdown_cleanup(app: Application):
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()


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

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init_setup)
        .post_shutdown(post_shutdown_cleanup)
        .build()
    )

    # 1. Super Owner Commands (8)
    application.add_handler(CommandHandler("suser", cmd_suser))
    application.add_handler(CommandHandler("userlist", cmd_userlist))
    application.add_handler(CommandHandler("apermit", cmd_apermit))
    application.add_handler(CommandHandler("rpermit", cmd_rpermit))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(CommandHandler("solve", cmd_solve))
    application.add_handler(CommandHandler("stt", cmd_stt))
    application.add_handler(CommandHandler("aistatus", cmd_aistatus))

    # 2. Special Commands (6)
    application.add_handler(CommandHandler("gsearch", cmd_gsearch))
    application.add_handler(CommandHandler("tran", cmd_tran))
    application.add_handler(CommandHandler("tts", cmd_tts))
    application.add_handler(CommandHandler("ttsm", cmd_ttsm))
    application.add_handler(CommandHandler("grammar", cmd_grammar))
    application.add_handler(CommandHandler("afk", cmd_afk))

    # 3. Permitted Commands (9)
    application.add_handler(CommandHandler("lang", cmd_lang))
    application.add_handler(CommandHandler("selsen", cmd_selsen))
    application.add_handler(CommandHandler("yat", cmd_yat))
    application.add_handler(CommandHandler("delmsg", cmd_delmsg))
    application.add_handler(CommandHandler("addtext", cmd_addtext))
    application.add_handler(CommandHandler("listtext", cmd_listtext))
    application.add_handler(CommandHandler("edittext", cmd_edittext))
    application.add_handler(CommandHandler("removetext", cmd_removetext))
    application.add_handler(CommandHandler("cleartext", cmd_cleartext))

    # 4. Public Commands (10)
    application.add_handler(CommandHandler("cmlist", cmd_cmlist))
    application.add_handler(CommandHandler("wiki", cmd_wiki))
    application.add_handler(CommandHandler("checkpf", cmd_checkpf))
    application.add_handler(CommandHandler("gevent", cmd_gevent))
    application.add_handler(CommandHandler("qhour", cmd_qhour))
    application.add_handler(CommandHandler("rate", cmd_rate))
    application.add_handler(CommandHandler("qr", cmd_qr))
    application.add_handler(CommandHandler("calc", cmd_calc))
    application.add_handler(CommandHandler("dict", cmd_dict))
    application.add_handler(CommandHandler("stats", cmd_stats))

    # Callbacks, Join Events & General Listener
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, general_message_handler))

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

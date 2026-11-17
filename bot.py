import os
import re
import io
import json
import time
import math
import base64
import hashlib
import random
import asyncio
import logging
import html
import urllib.parse
from datetime import datetime, timezone, timedelta

import aiohttp
import feedparser
import qrcode
import edge_tts
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
OWNER_ID = int(os.getenv("OWNER_ID", "8966139030"))
JSONBIN_API_KEY = os.getenv("JSONBIN_API_KEY", "")
JSONBIN_BIN_ID = os.getenv("JSONBIN_BIN_ID", "")
PORT = int(os.getenv("PORT", "10000"))

# Multi-AI & Search Provider Keys (Configure available keys in Render Environment)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")
SAMBANOVA_API_KEY = os.getenv("SAMBANOVA_API_KEY", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
COHERE_API_KEY = os.getenv("COHERE_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")
HF_TOKEN = os.getenv("HF_TOKEN", "")

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
    "special_users": [],         # Co-Owners / Special Admins (/suser a/d)
    "permitted_users": [],       # Permitted Users (/apermit /rmpermit)
    "spam_texts": [
        "မင်္ဂလာပါ! ဘာတွေလုပ်နေကြလဲဗျို့ 👋",
        "စကားလေးဘာလေး ပြောကြပါဦးနော် 💬",
        "Mobeho Bot မှ နှုတ်ခွန်းဆက်သအပ်ပါတယ် 🚀",
    ],
    "quiz_scores": {},           # {user_id_str: {"name": str, "score": int}}
    "quiz_enabled_chats": [],    # [chat_id_int]
    "quiz_seen_hashes": {},      # {chat_id_str: [hash_str, ...]} -> prevents duplicate questions
    "aichat_enabled_chats": [],  # [chat_id_int] -> /aichat on/off
    "chat_langs": {},            # {chat_id_str: "mm" | "en"}
    "notes": {},                 # {user_id_str: {note_key: note_content}}
    "known_chats": [],           # [chat_id_int]
}

bot_data_store = json.loads(json.dumps(DEFAULT_DATA))
_save_lock = asyncio.Lock()
_dirty_flag = False

# Runtime volatile states
active_spam_tasks = {}      # {chat_id: asyncio.Task}
bot_sent_messages = {}      # {chat_id: [msg_id, ...]}
afk_users = {}              # {user_id: {"reason": str, "since": float, "name": str}}
active_quizzes = {}         # {chat_id: {"q": str, "opts": list, "ans": int, "answered": set, "winners": list, "msg_id": int}}
news_cache = {}             # {cache_key: [article_dict, ...]}
aichat_history = {}         # {chat_id: [{"role": "user"|"assistant", "content": str}, ...]}
pomodoro_tasks = {}         # {(chat_id, user_id): asyncio.Task}


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
# 4-TIER ACCESS CONTROL & LOCALIZATION HELPERS
# ==============================================================================
def is_super(user_id: int) -> bool:
    """Tier 1: Primary Owner ONLY (OWNER_ID)."""
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
    if chat.type == ChatType.PRIVATE:
        return "en"
    return "mm"


def track_chat(chat_id: int):
    if chat_id not in bot_data_store["known_chats"]:
        bot_data_store["known_chats"].append(chat_id)
        asyncio.create_task(save_to_jsonbin(force=False))


def track_bot_msg(chat_id: int, message_id: int):
    bot_sent_messages.setdefault(chat_id, []).append(message_id)
    if len(bot_sent_messages[chat_id]) > 250:
        bot_sent_messages[chat_id] = bot_sent_messages[chat_id][-250:]


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
    if len(text) > 4000:
        text = text[:3990] + "..."
    try:
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
    except TelegramError:
        kwargs.pop("parse_mode", None)
        msg = await message.reply_text(text=text, **kwargs)
    track_bot_msg(message.chat_id, msg.message_id)
    return msg


# ==============================================================================
# MULTI-AI ORCHESTRATOR ENGINE (AUTO-ROTATION & ZERO-DOWNTIME FALLBACK)
# ==============================================================================
async def _call_gemini(prompt: str, model: str = "gemini-2.0-flash", system_prompt: str = "", image_bytes: bytes = None, history: list = None) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
    contents = []
    if history:
        for h in history[-8:]:
            role = "user" if h["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": h["content"]}]})

    user_parts = [{"text": prompt}]
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        user_parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
    contents.append({"role": "user", "parts": user_parts})

    payload = {"contents": contents}
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        async with session.post(url, json=payload, timeout=35) as resp:
            if resp.status != 200:
                err_txt = await resp.text()
                raise RuntimeError(f"Gemini {model} HTTP {resp.status}: {err_txt[:120]}")
            data = await resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                raise RuntimeError("Gemini empty response")
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts).strip()


async def _call_openai_compat(base_url: str, api_key: str, model: str, prompt: str, system_prompt: str = "", history: list = None) -> str:
    if not api_key:
        raise RuntimeError(f"API key missing for {model}")
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history:
        for h in history[-8:]:
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": prompt})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    payload = {"model": model, "messages": messages, "temperature": 0.6, "max_tokens": 1800}

    async with aiohttp.ClientSession() as session:
        async with session.post(base_url, json=payload, headers=headers, timeout=30) as resp:
            if resp.status != 200:
                err_txt = await resp.text()
                raise RuntimeError(f"{model} HTTP {resp.status}: {err_txt[:120]}")
            data = await resp.json()
            return data["choices"][0]["message"]["content"].strip()


async def _call_cohere(prompt: str, system_prompt: str = "") -> str:
    if not COHERE_API_KEY:
        raise RuntimeError("COHERE_API_KEY missing")
    url = "https://api.cohere.com/v2/chat"
    headers = {"Authorization": f"Bearer {COHERE_API_KEY}", "Content-Type": "application/json"}
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    payload = {"model": "command-r-plus-08-2024", "messages": messages}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers, timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Cohere HTTP {resp.status}")
            data = await resp.json()
            return data["message"]["content"][0]["text"].strip()


async def run_ai_router(prompt: str, tier: str = "super", system_prompt: str = "", image_bytes: bytes = None, history: list = None) -> tuple[str, str]:
    """
    Routes AI calls by privilege tier with automatic multi-provider fallback:
    - 'super'    : Gemini 2.0 Flash -> Gemini 2.0 Flash-Lite -> GitHub GPT-4o -> Groq Llama 3.3 -> Cerebras -> SambaNova -> Cohere
    - 'think'    : DeepSeek-R1 (SambaNova/GitHub/Groq) -> Gemini 2.0 Flash -> Gemini 2.0 Flash-Lite
    - 'fast'     : Cerebras Llama 3.3 -> Groq Llama 3.3 -> Gemini 2.0 Flash-Lite
    - 'standard' : Gemini 2.0 Flash-Lite -> Groq Llama 3.3 -> Cerebras -> Cohere (protects Super Owner's 2.5 Pro quota)
    """
    if image_bytes:
        models = ["gemini-2.0-flash", "gemini-2.0-flash-lite"] if tier == "super" else ["gemini-2.0-flash-lite"]
        for m in models:
            try:
                ans = await _call_gemini(prompt, model=m, system_prompt=system_prompt, image_bytes=image_bytes, history=history)
                if ans:
                    return ans, m
            except Exception as e:
                logger.warning(f"Vision fallback ({m}): {e}")
        raise RuntimeError("Vision AI unavailable or GEMINI_API_KEY missing.")

    if tier == "super":
        chain = [
            ("Gemini 2.0 Flash", lambda: _call_gemini(prompt, "gemini-2.0-flash", system_prompt, None, history)),
            ("Gemini 2.0 Flash-Lite", lambda: _call_gemini(prompt, "gemini-2.0-flash-lite", system_prompt, None, history)),
            ("GitHub GPT-4o", lambda: _call_openai_compat("https://models.github.ai/inference/chat/completions", GITHUB_TOKEN, "gpt-4o", prompt, system_prompt, history)),
            ("Groq Llama-3.1", lambda: _call_openai_compat("https://api.groq.com/openai/v1/chat/completions", GROQ_API_KEY, "llama-3.1-8b-instant", prompt, system_prompt, history)),
            ("Cerebras Llama-3.3-70B", lambda: _call_openai_compat("https://api.cerebras.ai/v1/chat/completions", CEREBRAS_API_KEY, "llama-3.3-70b", prompt, system_prompt, history)),
            ("SambaNova Llama-3.3-70B", lambda: _call_openai_compat("https://api.sambanova.ai/v1/chat/completions", SAMBANOVA_API_KEY, "Meta-Llama-3.3-70B-Instruct", prompt, system_prompt, history)),
            ("Cohere Command-R+", lambda: _call_cohere(prompt, system_prompt)),
        ]
    elif tier == "think":
        chain = [
            ("SambaNova DeepSeek-R1", lambda: _call_openai_compat("https://api.sambanova.ai/v1/chat/completions", SAMBANOVA_API_KEY, "DeepSeek-R1", prompt, system_prompt, history)),
            ("GitHub DeepSeek-R1", lambda: _call_openai_compat("https://models.github.ai/inference/chat/completions", GITHUB_TOKEN, "DeepSeek-R1", prompt, system_prompt, history)),
            ("Groq DeepSeek-R1-70B", lambda: _call_openai_compat("https://api.groq.com/openai/v1/chat/completions", GROQ_API_KEY, "qwen-qwq-32b", prompt, system_prompt, history)),
            ("Gemini 2.0 Flash", lambda: _call_gemini(prompt, "gemini-2.0-flash", system_prompt, None, history)),
            ("Gemini 2.0 Flash-Lite", lambda: _call_gemini(prompt, "gemini-2.0-flash-lite", system_prompt, None, history)),
        ]
    elif tier == "fast":
        chain = [
            ("Cerebras Llama-3.3-70B", lambda: _call_openai_compat("https://api.cerebras.ai/v1/chat/completions", CEREBRAS_API_KEY, "llama-3.3-70b", prompt, system_prompt, history)),
            ("Groq Llama-3.1", lambda: _call_openai_compat("https://api.groq.com/openai/v1/chat/completions", GROQ_API_KEY, "llama-3.1-8b-instant", prompt, system_prompt, history)),
            ("Gemini 2.0 Flash-Lite", lambda: _call_gemini(prompt, "gemini-2.0-flash-lite", system_prompt, None, history)),
        ]
    else:
        chain = [
            ("Gemini 2.0 Flash-Lite", lambda: _call_gemini(prompt, "gemini-2.0-flash-lite", system_prompt, None, history)),
            ("Groq Llama-3.1", lambda: _call_openai_compat("https://api.groq.com/openai/v1/chat/completions", GROQ_API_KEY, "llama-3.1-8b-instant", prompt, system_prompt, history)),
            ("Cerebras Llama-3.3-70B", lambda: _call_openai_compat("https://api.cerebras.ai/v1/chat/completions", CEREBRAS_API_KEY, "llama-3.3-70b", prompt, system_prompt, history)),
            ("Cohere Command-R+", lambda: _call_cohere(prompt, system_prompt)),
        ]

    for engine_name, fn in chain:
        try:
            ans = await fn()
            if ans:
                ans = re.sub(r"<think>.*?</think>", "", ans, flags=re.DOTALL).strip()
                return ans, engine_name
        except Exception as e:
            logger.warning(f"AI chain fallback ({engine_name}): {e}")
            continue

    raise RuntimeError("No AI API keys configured or all providers busy. Use /aistatus to check keys.")


# ==============================================================================
# LIVE WEB SEARCH ENGINE (TAVILY + SERPER + DUCKDUCKGO FALLBACK)
# ==============================================================================
async def live_web_search(query: str, max_results: int = 4) -> list[dict]:
    results = []
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        if TAVILY_API_KEY:
            try:
                payload = {"api_key": TAVILY_API_KEY, "query": query, "search_depth": "basic", "max_results": max_results}
                async with session.post("https://api.tavily.com/search", json=payload, timeout=12) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for r in data.get("results", []):
                            results.append({"title": r.get("title", ""), "snippet": r.get("content", ""), "url": r.get("url", "")})
                        if results:
                            return results
            except Exception as e:
                logger.warning(f"Tavily search error: {e}")

        if SERPER_API_KEY:
            try:
                headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
                async with session.post("https://google.serper.dev/search", json={"q": query, "num": max_results}, headers=headers, timeout=12) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if "knowledgeGraph" in data:
                            kg = data["knowledgeGraph"]
                            results.append({
                                "title": kg.get("title", query),
                                "snippet": kg.get("description", ""),
                                "url": kg.get("website", ""),
                                "image": kg.get("imageUrl", ""),
                            })
                        for r in data.get("organic", [])[:max_results]:
                            results.append({"title": r.get("title", ""), "snippet": r.get("snippet", ""), "url": r.get("link", "")})
                        if results:
                            return results
            except Exception as e:
                logger.warning(f"Serper search error: {e}")

        try:
            ddg_url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1&skip_disambig=1"
            async with session.get(ddg_url, timeout=10) as resp:
                if resp.status == 200:
                    data = json.loads(await resp.text())
                    if data.get("AbstractText"):
                        results.append({
                            "title": data.get("Heading", query),
                            "snippet": data.get("AbstractText", ""),
                            "url": data.get("AbstractURL", ""),
                            "image": f"https://duckduckgo.com{data['Image']}" if data.get("Image") else "",
                        })
                    for topic in data.get("RelatedTopics", [])[:max_results]:
                        if isinstance(topic, dict) and topic.get("Text"):
                            results.append({"title": topic.get("Text", "")[:60], "snippet": topic.get("Text", ""), "url": topic.get("FirstURL", "")})
        except Exception:
            pass

    return results


# ==============================================================================
# ASYNC TRANSLATE HELPER (GTX + MYMEMORY FALLBACK)
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
    except Exception:
        pass

    mm_url = f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(snippet[:500])}&langpair=en|{urllib.parse.quote(target)}"
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        async with session.get(mm_url, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                res = data.get("responseData", {}).get("translatedText", "")
                if res:
                    return html.unescape(res)
    raise RuntimeError("Translation services temporarily busy.")


# ==============================================================================
# LIVE UNLIMITED QUIZ ENGINE (OPENTRIVIADB + THE TRIVIA API + AI GENERATOR)
# ==============================================================================
FALLBACK_MYANMAR_QUIZ = [
    {"q": "မြန်မာနိုင်ငံ၏ အမြင့်ဆုံးတောင်ထွဋ်မှာ အဘယ်နည်း?", "opts": ["ခါကာဘိုရာဇီတောင်", "ပုပ္ပားတောင်", "ဇွဲကပင်တောင်"], "ans": 0},
    {"q": "ပုဂံခေတ်ကို စတင်တည်ထောင်ခဲ့သော မင်းမှာ မည်သူနည်း?", "opts": ["ကျန်စစ်သားမင်း", "အနော်ရထာမင်း", "ဘုရင့်နောင်မင်း"], "ans": 1},
    {"q": "ကမ္ဘာပေါ်တွင် အကြီးဆုံး သမုဒ္ဒရာမှာ အဘယ်နည်း?", "opts": ["အတ္တလန္တိတ်သမုဒ္ဒရာ", "အိန္ဒိယသမုဒ္ဒရာ", "ပစိဖိတ်သမုဒ္ဒရာ"], "ans": 2},
    {"q": "မြန်မာနိုင်ငံ၏ အရှည်ဆုံးမြစ်မှာ မည်သည့်မြစ်နည်း?", "opts": ["သံလွင်မြစ်", "ဧရာဝတီမြစ်", "ချင်းတွင်းမြစ်"], "ans": 1},
    {"q": "နေအဖွဲ့အစည်းတွင် နေနှင့် အနီးဆုံးဂြိုဟ်မှာ အဘယ်နည်း?", "opts": ["ဗုဒ္ဓဟူးဂြိုဟ် (Mercury)", "သောကြာဂြိုဟ် (Venus)", "အင်္ဂါဂြိုဟ် (Mars)"], "ans": 0},
    {"q": "အာဆီယံ (ASEAN) အဖွဲ့ဝင် နိုင်ငံပေါင်း မည်မျှရှိသနည်း?", "opts": ["၉ နိုင်ငံ", "၁၀ နိုင်ငံ", "၁၁ နိုင်ငံ"], "ans": 1},
    {"q": "ရေ၏ ဓာတုသင်္ကေတမှာ အဘယ်နည်း?", "opts": ["CO2", "H2O", "O2"], "ans": 1},
    {"q": "မြန်မာနိုင်ငံ လွတ်လပ်ရေးရရှိခဲ့သော ခုနှစ်မှာ အဘယ်နည်း?", "opts": ["၁၉၄၇ ခုနှစ်", "၁၉၄၈ ခုနှစ်", "၁၉၅၀ ခုနှစ်"], "ans": 1},
]


def _q_hash(text: str) -> str:
    return hashlib.md5(text.strip().lower().encode("utf-8")).hexdigest()[:12]


async def fetch_live_unique_quiz(chat_id: int) -> dict:
    """Fetches a fresh non-repeating trivia question from Live APIs or AI and translates to Burmese."""
    cid_str = str(chat_id)
    seen = set(bot_data_store["quiz_seen_hashes"].get(cid_str, []))

    # Strategy 1: AI Live Trivia Generator (Myanmar + World GK, never repeats)
    if GEMINI_API_KEY or GROQ_API_KEY:
        try:
            topics = [
                "မြန်မာ့သမိုင်းနှင့် ပထဝီ",
                "သိပ္ပံနှင့် နည်းပညာ",
                "ကမ္ဘာ့ဗဟုသုတ",
                "အားကစားနှင့် ဂီတ",
                "နက္ခတ္တဗေဒနှင့် သဘာဝပတ်ဝန်းကျင်",
                "စာပေနှင့် ယဉ်ကျေးမှု",
            ]
            chosen_topic = random.choice(topics)
            prompt = (
                f"Create 1 unique multiple-choice general knowledge trivia question in Burmese (မြန်မာဘာသာ) about '{chosen_topic}'. "
                f"Return ONLY valid JSON in this exact format with 3 options and 0-based answer index:\n"
                f'{{"q": "မေးခွန်း?", "opts": ["ရွေးချယ်စရာ ၁", "ရွေးချယ်စရာ ၂", "ရွေးချယ်စရာ ၃"], "ans": 0}}'
            )
            raw_json, _ = await run_ai_router(prompt, tier="fast")
            m = re.search(r"\{.*\}", raw_json, re.DOTALL)
            if m:
                parsed = json.loads(m.group(0))
                if isinstance(parsed.get("opts"), list) and len(parsed["opts"]) == 3 and parsed["q"]:
                    h = _q_hash(parsed["q"])
                    if h not in seen:
                        bot_data_store["quiz_seen_hashes"].setdefault(cid_str, []).append(h)
                        if len(bot_data_store["quiz_seen_hashes"][cid_str]) > 500:
                            bot_data_store["quiz_seen_hashes"][cid_str] = bot_data_store["quiz_seen_hashes"][cid_str][-500:]
                        await save_to_jsonbin(force=False)
                        return {"q": parsed["q"], "opts": parsed["opts"], "ans": int(parsed["ans"]) % 3, "source": "AI Live"}
        except Exception as e:
            logger.warning(f"AI Quiz generator fallback: {e}")

    # Strategy 2: OpenTriviaDB Live API + Auto Burmese Translation
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            async with session.get("https://opentdb.com/api.php?amount=5&type=multiple", timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get("results", []):
                        en_q = html.unescape(item["question"])
                        h = _q_hash(en_q)
                        if h in seen:
                            continue
                        correct = html.unescape(item["correct_answer"])
                        wrongs = [html.unescape(w) for w in item["incorrect_answers"][:2]]
                        opts_en = [correct] + wrongs
                        random.shuffle(opts_en)
                        ans_idx = opts_en.index(correct)

                        mm_q = await async_translate(en_q, target="my")
                        mm_opts = []
                        for o in opts_en:
                            tr_o = await async_translate(o, target="my")
                            mm_opts.append(f"{tr_o} ({o})" if tr_o.lower() != o.lower() else o)

                        bot_data_store["quiz_seen_hashes"].setdefault(cid_str, []).append(h)
                        await save_to_jsonbin(force=False)
                        return {"q": mm_q, "opts": mm_opts, "ans": ans_idx, "source": "OpenTriviaDB Live"}
    except Exception as e:
        logger.warning(f"OpenTriviaDB fallback: {e}")

    # Strategy 3: The Trivia API Live
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            async with session.get("https://the-trivia-api.com/v2/questions?limit=5", timeout=10) as resp:
                if resp.status == 200:
                    items = await resp.json()
                    for item in items:
                        en_q = item["question"]["text"]
                        h = _q_hash(en_q)
                        if h in seen:
                            continue
                        correct = item["correctAnswer"]
                        wrongs = item["incorrectAnswers"][:2]
                        opts_en = [correct] + wrongs
                        random.shuffle(opts_en)
                        ans_idx = opts_en.index(correct)

                        mm_q = await async_translate(en_q, target="my")
                        mm_opts = [await async_translate(o, target="my") for o in opts_en]
                        bot_data_store["quiz_seen_hashes"].setdefault(cid_str, []).append(h)
                        await save_to_jsonbin(force=False)
                        return {"q": mm_q, "opts": mm_opts, "ans": ans_idx, "source": "TriviaAPI Live"}
    except Exception:
        pass

    q_item = random.choice(FALLBACK_MYANMAR_QUIZ)
    return {**q_item, "source": "Built-in Bank"}


# ==============================================================================
# 1. SUPER COMMANDS (14 COMMANDS: PRIMARY OWNER ONLY - 8966139030)
# ==============================================================================
async def cmd_suser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Super Owner ONLY: /suser a (add Co-Owner) or /suser d (delete Co-Owner)."""
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

    if update.message.reply_to_message:
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
            "⚠️ *Co-Owner (Special Admin) စီမံခန့်ခွဲမှု:*\n• ခန့်ရန်: Reply ထောက်ပြီး `/suser a` (သို့) `/suser a 123456789`\n• ဖြုတ်ရန်: Reply ထောက်ပြီး `/suser d` (သို့) `/suser d 123456789`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if target_id == OWNER_ID:
        await send_tracked(context, update.effective_chat.id, "👑 Primary Owner ကို ပြင်ဆင်၍ မရပါ။")
        return

    if action in ("a", "add"):
        if target_id not in bot_data_store["special_users"]:
            bot_data_store["special_users"].append(target_id)
            await save_to_jsonbin(force=True)
        await send_tracked(
            context,
            update.effective_chat.id,
            f"⭐ *Co-Owner (Special Admin) ခန့်အပ်ပြီးပါပြီ:*\n👤 `{target_name}` (`{target_id}`)\n_Special AI/Pro Tools များ အသုံးပြုခွင့် ရရှိသွားပါပြီ။_",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        if target_id in bot_data_store["special_users"]:
            bot_data_store["special_users"].remove(target_id)
            await save_to_jsonbin(force=True)
            await send_tracked(context, update.effective_chat.id, f"🗑 *Co-Owner ဖြုတ်သိမ်းပြီးပါပြီ:* `{target_id}`", parse_mode=ParseMode.MARKDOWN)
        else:
            await send_tracked(context, update.effective_chat.id, f"ℹ️ `{target_id}` သည် Co-Owner စာရင်းထဲတွင် မရှိပါ။", parse_mode=ParseMode.MARKDOWN)


async def cmd_listsuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        try:
            await update.message.delete()
        except TelegramError:
            pass
    users = bot_data_store["special_users"]
    if not users:
        await send_tracked(context, update.effective_chat.id, "⭐ *Co-Owner (Special Admins):* None", parse_mode=ParseMode.MARKDOWN)
        return
    lines = ["⭐ *Co-Owner (Special Admins) List:*"] + [f"• `{uid}`" for uid in users]
    await send_tracked(context, update.effective_chat.id, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


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
    if update.message.reply_to_message:
        u = update.message.reply_to_message.from_user
        target_id = u.id
        target_name = u.full_name
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
        target_name = str(target_id)

    if not target_id:
        await send_tracked(context, update.effective_chat.id, "⚠️ Reply to a user or provide ID: `/apermit 123456789`", parse_mode=ParseMode.MARKDOWN)
        return

    if target_id not in bot_data_store["permitted_users"]:
        bot_data_store["permitted_users"].append(target_id)
        await save_to_jsonbin(force=True)
    await send_tracked(context, update.effective_chat.id, f"✅ *Permission Granted:* `{target_name}` (`{target_id}`)", parse_mode=ParseMode.MARKDOWN)


async def cmd_rmpermit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
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
        await send_tracked(context, update.effective_chat.id, "⚠️ Reply to a user or provide ID: `/rmpermit 123456789`", parse_mode=ParseMode.MARKDOWN)
        return

    if target_id in bot_data_store["permitted_users"]:
        bot_data_store["permitted_users"].remove(target_id)
        await save_to_jsonbin(force=True)
        await send_tracked(context, update.effective_chat.id, f"🗑 *Permission Revoked:* `{target_id}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await send_tracked(context, update.effective_chat.id, f"ℹ️ User `{target_id}` is not in permitted list.", parse_mode=ParseMode.MARKDOWN)


async def cmd_listpermit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
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


async def _extract_replied_photo_bytes(message, context: ContextTypes.DEFAULT_TYPE) -> bytes | None:
    rep = message.reply_to_message
    if not rep:
        return None
    if rep.photo:
        f = await context.bot.get_file(rep.photo[-1].file_id)
        bio = io.BytesIO()
        await f.download_to_memory(bio)
        return bio.getvalue()
    return None


# Super AI 1: /ask (Gemini 2.0 Flash + Full Multi-AI Fallback + Vision)
async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    query = " ".join(context.args).strip()
    rep_text = ""
    if update.message.reply_to_message:
        rep_text = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    img_bytes = await _extract_replied_photo_bytes(update.message, context)

    if not query and not rep_text and not img_bytes:
        await reply_tracked(update.message, "⚠️ မေးခွန်းရိုက်ပါ (သို့) ပုံ/စာကို Reply ထောက်ပြီး `/ask <မေးခွန်း>` ရိုက်ပါ။", parse_mode=ParseMode.MARKDOWN)
        return

    full_prompt = f"Context:\n{rep_text}\n\nQuestion: {query}".strip() if rep_text else (query or "Describe and analyze this image in detail.")
    wait_msg = await reply_tracked(update.message, "🧠 *Gemini 2.0 Flash VIP* စဉ်းစားဖြေကြားနေပါသည်...", parse_mode=ParseMode.MARKDOWN)
    sys_prompt = "You are Mobeho VIP AI Assistant. Answer clearly, accurately, and naturally in the user's language (Burmese or English)."

    answer = ""
    engine = "AI"
    try:
        answer, engine = await run_ai_router(full_prompt, tier="super", system_prompt=sys_prompt, image_bytes=img_bytes)
        await wait_msg.edit_text(f"👑 *{engine}*\n\n{answer[:3900]}", parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        await wait_msg.edit_text(f"👑 {engine}\n\n{answer[:3900]}")
    except Exception as e:
        await wait_msg.edit_text(f"⚠️ AI Error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# Super AI 2: /aiweb (Real-Time Web Search + Gemini 2.0 Flash)
async def cmd_aiweb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    query = " ".join(context.args).strip()
    if not query:
        await reply_tracked(update.message, "⚠️ အင်တာနက်ပေါ်က နောက်ဆုံးရအချက်အလက် ရှာမေးရန်: `/aiweb <မေးခွန်း>`", parse_mode=ParseMode.MARKDOWN)
        return

    wait_msg = await reply_tracked(update.message, "🌐 Real-Time Web Search + Gemini 2.0 Flash ရှာဖွေနေပါသည်...")
    try:
        web_hits = await live_web_search(query, max_results=4)
        context_block = "\n".join([f"- {h['title']}: {h['snippet']} ({h['url']})" for h in web_hits]) or "No direct web snippets."
        prompt = (
            f"User Question: {query}\n\n"
            f"Live Web Search Results:\n{context_block}\n\n"
            f"Synthesize a clear, accurate, up-to-date answer in Burmese (or English if asked), citing key facts and sources."
        )
        answer, engine = await run_ai_router(prompt, tier="super")
        sources = "\n".join([f"• [{clean_text(h['title'])[:45]}]({h['url']})" for h in web_hits[:3] if h.get("url")])
        final_txt = f"🌐 *Live Web AI ({engine})*\n\n{answer[:3300]}"
        if sources:
            final_txt += f"\n\n🔗 *Sources:*\n{sources}"
        await wait_msg.edit_text(final_txt, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception as e:
        await wait_msg.edit_text(f"⚠️ Web AI Error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# Super AI 3: /think (DeepSeek-R1 671B / Gemini 2.0 Flash Deep Reasoning)
async def cmd_think(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    query = " ".join(context.args).strip()
    if not query and update.message.reply_to_message:
        query = update.message.reply_to_message.text or ""
    if not query:
        await reply_tracked(update.message, "⚠️ နက်နက်နဲနဲ ခွဲခြမ်းစိတ်ဖြာမေးမြန်းရန်: `/think <ပုစ္ဆာ/မေးခွန်း>`", parse_mode=ParseMode.MARKDOWN)
        return

    wait_msg = await reply_tracked(update.message, "🔬 *Deep Reasoning Engine* နက်ရှိုင်းစွာ တွက်ချက်စဉ်းစားနေပါသည်...", parse_mode=ParseMode.MARKDOWN)
    answer = ""
    engine = "Deep Think"
    try:
        answer, engine = await run_ai_router(query, tier="think")
        await wait_msg.edit_text(f"🔬 *Deep Think ({engine})*\n\n{answer[:3900]}", parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        await wait_msg.edit_text(f"🔬 Deep Think ({engine})\n\n{answer[:3900]}")
    except Exception as e:
        await wait_msg.edit_text(f"⚠️ Deep Think Error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# Super AI 4: /fast (Cerebras / Groq Instant Sub-Second AI)
async def cmd_fast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    query = " ".join(context.args).strip()
    if not query:
        await reply_tracked(update.message, "⚠️ အမြန်ဆုံး AI အဖြေထုတ်ရန်: `/fast <မေးခွန်း>`", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        answer, engine = await run_ai_router(query, tier="fast")
        await reply_tracked(update.message, f"⚡ *{engine}*\n\n{answer[:3900]}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Fast AI Error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# Super AI 5: /solve (Step-by-Step Math & Science Problem Solver with Vision)
async def cmd_solve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    query = " ".join(context.args).strip()
    rep_text = update.message.reply_to_message.text if (update.message.reply_to_message and update.message.reply_to_message.text) else ""
    img_bytes = await _extract_replied_photo_bytes(update.message, context)

    if not query and not rep_text and not img_bytes:
        await reply_tracked(update.message, "⚠️ ပုစ္ဆာစာသားရိုက်ပါ (သို့) ပုစ္ဆာဓာတ်ပုံကို Reply ထောက်ပြီး `/solve` ရိုက်ပါ။", parse_mode=ParseMode.MARKDOWN)
        return

    wait_msg = await reply_tracked(update.message, "📐 ပုစ္ဆာကို Step-by-Step တွက်ချက်နေပါသည်...")
    prompt = (
        f"Solve this math/science problem step-by-step clearly. Explain each step in Burmese (with standard math notation) "
        f"and highlight the final answer at the end.\nProblem: {rep_text} {query}".strip()
    )
    answer = ""
    engine = "Solver"
    try:
        answer, engine = await run_ai_router(prompt, tier="super", image_bytes=img_bytes)
        await wait_msg.edit_text(f"📐 *Step-by-Step Solution ({engine})*\n\n{answer[:3900]}", parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        await wait_msg.edit_text(f"📐 Solution ({engine})\n\n{answer[:3900]}")
    except Exception as e:
        await wait_msg.edit_text(f"⚠️ Solve Error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# Super AI 6: /imagine (Dual-Engine HD AI Art: Pollinations Flux + Hugging Face)
async def cmd_imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    raw_prompt = " ".join(context.args).strip()
    if not raw_prompt:
        await reply_tracked(update.message, "⚠️ AI ပုံထုတ်ချင်တဲ့ အကြောင်းအရာ ရိုက်ပါ: `/imghd Cyberpunk Yangon at night`", parse_mode=ParseMode.MARKDOWN)
        return

    wait_msg = await reply_tracked(update.message, "🎨 Dual-Engine HD AI ပုံဖန်တီးနေပါသည် ခဏစောင့်ပါ...")
    try:
        if any("\u1000" <= ch <= "\u109F" for ch in raw_prompt):
            base_prompt = await async_translate(raw_prompt, target="en")
        else:
            base_prompt = raw_prompt
    except Exception:
        base_prompt = raw_prompt

    enhanced_prompt = f"{base_prompt}, ultra detailed, cinematic lighting, 8k resolution, sharp focus, masterpiece"
    encoded = urllib.parse.quote(enhanced_prompt)
    seed = random.randint(1000, 9999999)

    img_bytes = None
    used_engine = "Pollinations Flux"

    if HF_TOKEN:
        hf_url = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(hf_url, headers={"Authorization": f"Bearer {HF_TOKEN}"}, json={"inputs": enhanced_prompt}, timeout=35) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) > 5000:
                            img_bytes = data
                            used_engine = "HuggingFace FLUX.1"
        except Exception:
            pass

    if not img_bytes:
        endpoints = [
            f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&model=flux&seed={seed}&nologo=true",
            f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&model=turbo&seed={seed}&nologo=true",
        ]
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            for url in endpoints:
                try:
                    async with session.get(url, timeout=35) as resp:
                        if resp.status == 200 and "image" in resp.headers.get("Content-Type", ""):
                            data = await resp.read()
                            if len(data) > 5000:
                                img_bytes = data
                                break
                except Exception:
                    continue

    if img_bytes:
        bio = io.BytesIO(img_bytes)
        bio.seek(0)
        sent = await update.message.reply_photo(
            photo=bio,
            caption=f"🎨 *AI HD Image ({used_engine})*\nPrompt: `{raw_prompt[:120]}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        track_bot_msg(update.effective_chat.id, sent.message_id)
        try:
            await wait_msg.delete()
        except TelegramError:
            pass
    else:
        await wait_msg.edit_text("⚠️ AI Image server များ ခဏအလုပ်များနေပါသည်။ ခဏနေ ပြန်စမ်းကြည့်ပါ။")


# Super AI 7: /stt (Groq Whisper Large-v3 Voice-to-Text)
async def cmd_stt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    rep = update.message.reply_to_message
    if not rep or not (rep.voice or rep.audio):
        await reply_tracked(update.message, "⚠️ Voice Note သို့မဟုတ် Audio ဖိုင်ကို Reply ထောက်ပြီး `/stt` ရိုက်ပါ။", parse_mode=ParseMode.MARKDOWN)
        return
    if not GROQ_API_KEY:
        await reply_tracked(update.message, "⚠️ `GROQ_API_KEY` ထည့်ထားမှ Voice-to-Text (`/stt`) အလုပ်လုပ်ပါမည်။", parse_mode=ParseMode.MARKDOWN)
        return

    wait_msg = await reply_tracked(update.message, "🎙 *Groq Whisper AI* အသံဖိုင်ကို စာသားပြောင်းနေပါသည်...", parse_mode=ParseMode.MARKDOWN)
    try:
        media_obj = rep.voice or rep.audio
        tg_file = await context.bot.get_file(media_obj.file_id)
        bio = io.BytesIO()
        await tg_file.download_to_memory(bio)
        audio_bytes = bio.getvalue()

        form = aiohttp.FormData()
        form.add_field("file", audio_bytes, filename="voice.ogg", content_type="audio/ogg")
        form.add_field("model", "whisper-large-v3-turbo")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                data=form,
                timeout=30,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    transcript = data.get("text", "").strip()
                    await wait_msg.edit_text(f"🎙 *Voice Transcript:*\n\n{transcript}")
                else:
                    err = await resp.text()
                    await wait_msg.edit_text(f"⚠️ Whisper Error: HTTP {resp.status} - {err[:100]}")
    except Exception as e:
        await wait_msg.edit_text(f"⚠️ STT Error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# Super AI 8: /aistatus (Live AI Provider Health & Key Dashboard)
async def cmd_aistatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    providers = [
        ("Google Gemini (2.5 Pro / Flash)", bool(GEMINI_API_KEY)),
        ("Groq (Llama 3.3 70B & Whisper)", bool(GROQ_API_KEY)),
        ("Cerebras (Ultra-Fast Llama 3.3)", bool(CEREBRAS_API_KEY)),
        ("SambaNova (DeepSeek-R1 671B)", bool(SAMBANOVA_API_KEY)),
        ("GitHub Models (GPT-4o / R1)", bool(GITHUB_TOKEN)),
        ("Cohere (Command R+ Burmese)", bool(COHERE_API_KEY)),
        ("Tavily Web Search AI", bool(TAVILY_API_KEY)),
        ("Serper Google Search", bool(SERPER_API_KEY)),
        ("Hugging Face FLUX.1", bool(HF_TOKEN)),
        ("Microsoft Edge-TTS (Built-in)", True),
        ("Pollinations Flux Art (Built-in)", True),
    ]
    lines = ["🩺 *Mobeho Multi-AI Engine Status Dashboard*\n"]
    active_cnt = 0
    for name, ok in providers:
        if ok:
            active_cnt += 1
        lines.append(f"{'🟢' if ok else '⚪️'} *{name}* — `{'READY' if ok else 'No Key'}`")
    lines.append(f"\n⚡ Active Engines: *{active_cnt}/{len(providers)}*")
    await reply_tracked(update.message, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ==============================================================================
# 2. SPECIAL COMMANDS (11 COMMANDS: SUPER OWNER + CO-OWNERS /suser)
# ==============================================================================
# Special 1: /aichat on/off (Group AI Chat Toggle)
async def cmd_aichat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    arg = context.args[0].lower() if context.args else ""

    if arg == "on":
        if chat_id not in bot_data_store["aichat_enabled_chats"]:
            bot_data_store["aichat_enabled_chats"].append(chat_id)
            await save_to_jsonbin(force=True)
        await reply_tracked(
            update.message,
            "🤖 *Group AI Chat ဖွင့်လိုက်ပါပြီ!*\n"
            "• Group ထဲရှိ အဖွဲ့ဝင်များ Bot ၏ စာကို Reply ထောက်၍ဖြစ်စေ၊ `@mention` ခေါ်၍ဖြစ်စေ၊ `ai <မေးခွန်း>` ဟု ရိုက်၍ဖြစ်စေ စကားပြောမေးမြန်းနိုင်ပါပြီ။",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif arg == "off":
        if chat_id in bot_data_store["aichat_enabled_chats"]:
            bot_data_store["aichat_enabled_chats"].remove(chat_id)
            await save_to_jsonbin(force=True)
        await reply_tracked(update.message, "🛑 *Group AI Chat စနစ်ကို ပိတ်လိုက်ပါပြီ။*", parse_mode=ParseMode.MARKDOWN)
    else:
        st = "ON 🟢" if chat_id in bot_data_store["aichat_enabled_chats"] else "OFF 🔴"
        await reply_tracked(update.message, f"🤖 Group AI Chat Status: *{st}*\nUsage: `/aichat on` or `/aichat off`", parse_mode=ParseMode.MARKDOWN)


# Special 2: /gsearch <anything> (Google Knowledge Card + HD Photo + Live Search)
async def cmd_gsearch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    query = " ".join(context.args).strip()
    if not query:
        await reply_tracked(update.message, "⚠️ Google မှာ ရှာသလို အချက်အလက်နဲ့ ပုံရှာရန်: `/gsearch iPhone 16 Pro Max`", parse_mode=ParseMode.MARKDOWN)
        return

    wait_msg = await reply_tracked(update.message, f"🔎 `{query}` ကို Google Knowledge Card ရှာဖွေနေပါသည်...", parse_mode=ParseMode.MARKDOWN)
    tier = "super" if is_super(update.effective_user.id) else "standard"

    web_hits = await live_web_search(query, max_results=4)
    image_url = ""
    for h in web_hits:
        if h.get("image"):
            image_url = h["image"]
            break

    if not image_url:
        try:
            wiki_img_url = f"https://en.wikipedia.org/w/api.php?action=query&generator=search&gsrsearch={urllib.parse.quote(query)}&gsrlimit=1&prop=pageimages&pithumbsize=900&format=json"
            async with aiohttp.ClientSession(headers={"User-Agent": "MobehoBot/4.1"}) as session:
                async with session.get(wiki_img_url, timeout=8) as r:
                    wdata = await r.json()
                    pages = wdata.get("query", {}).get("pages", {})
                    for p in pages.values():
                        if "thumbnail" in p:
                            image_url = p["thumbnail"]["source"]
                            break
        except Exception:
            pass

    snippets = "\n".join([f"- {h['title']}: {h['snippet']} ({h['url']})" for h in web_hits]) or "No web snippets."
    lang_hint = "Burmese (မြန်မာဘာသာ)" if any("\u1000" <= ch <= "\u109F" for ch in query) else "Burmese with English technical terms"
    prompt = (
        f"Create a structured Google Knowledge Panel card for: '{query}'.\n"
        f"Web Search Data:\n{snippets}\n\n"
        f"Format cleanly in {lang_hint} with:\n"
        f"1) Exact Title & Category\n"
        f"2) Key Specs / Quick Facts (3-4 crisp bullet points)\n"
        f"3) Clear, accurate summary matching the query exactly (under 650 characters)."
    )

    try:
        card_body, _ = await run_ai_router(prompt, tier=tier)
    except Exception:
        card_body = web_hits[0]["snippet"] if web_hits else f"Search results for {query}"

    links_str = "\n".join([f"🔗 [{clean_text(h['title'])[:40]}]({h['url']})" for h in web_hits[:2] if h.get("url")])
    final_caption = f"🌐 *Google Search — {query}*\n\n{card_body}"
    if links_str:
        final_caption += f"\n\n{links_str}"

    if image_url:
        try:
            sent = await update.message.reply_photo(photo=image_url, caption=final_caption[:1020], parse_mode=ParseMode.MARKDOWN)
            track_bot_msg(update.effective_chat.id, sent.message_id)
            await wait_msg.delete()
            return
        except TelegramError:
            pass

    await wait_msg.edit_text(final_caption[:3900], parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


# Special 3: /tr (Natural AI Translator)
async def cmd_tr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
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

    if not text_to_tr.strip():
        await reply_tracked(update.message, "⚠️ စာကို Reply ထောက်ပြီး `/tr mm` (သို့) `/tr en` ရိုက်ပါ။", parse_mode=ParseMode.MARKDOWN)
        return

    tier = "super" if is_super(update.effective_user.id) else "standard"
    dest = "Burmese (မြန်မာစကားပြေ သဘာဝကျကျ)" if target_lang in ("mm", "my", "burmese", "myanmar") else target_lang
    try:
        translated, _ = await run_ai_router(f"Translate naturally into {dest}. Output ONLY the translation:\n\n{text_to_tr}", tier=tier)
        await reply_tracked(update.message, f"🌐 *AI Translation ({target_lang.upper()}):*\n\n{translated}", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        translated = await async_translate(text_to_tr, target=target_lang)
        await reply_tracked(update.message, f"🌐 *Translation ({target_lang.upper()}):*\n\n{translated}", parse_mode=ParseMode.MARKDOWN)


# Special 4 & 5: /tts (Female Nilar / Jenny) & /ttsm (Male Thiha / Guy) via Microsoft Edge-TTS
async def _edge_tts_generate(text: str, gender: str = "female") -> io.BytesIO:
    is_burmese = any("\u1000" <= ch <= "\u109F" for ch in text)
    if is_burmese:
        voice = "my-MM-NilarNeural" if gender == "female" else "my-MM-ThihaNeural"
    else:
        voice = "en-US-JennyNeural" if gender == "female" else "en-US-GuyNeural"

    communicate = edge_tts.Communicate(text[:600], voice)
    bio = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            bio.write(chunk["data"])
    bio.seek(0)
    return bio


async def cmd_tts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    text = " ".join(context.args).strip()
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""
    if not text:
        await reply_tracked(update.message, "⚠️ အသံပြောင်းချင်တဲ့ စာသား ရိုက်ပါ: `/tts မင်္ဂလာပါ ခင်ဗျာ`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        bio = await _edge_tts_generate(text, gender="female")
        sent = await update.message.reply_voice(voice=bio)
        track_bot_msg(update.effective_chat.id, sent.message_id)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Edge-TTS Error: `{e}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_ttsm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    text = " ".join(context.args).strip()
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""
    if not text:
        await reply_tracked(update.message, "⚠️ ယောက်ျားလေးအသံဖြင့် ပြောင်းရန်: `/ttsm မင်္ဂလာပါ`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        bio = await _edge_tts_generate(text, gender="male")
        sent = await update.message.reply_voice(voice=bio)
        track_bot_msg(update.effective_chat.id, sent.message_id)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Edge-TTS Error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# Special 6: /tldr (AI Bullet Summarizer)
async def cmd_tldr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    raw = ""
    if update.message.reply_to_message:
        raw = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    elif context.args:
        raw = " ".join(context.args)

    raw = clean_text(raw)
    if len(raw) < 40:
        await reply_tracked(update.message, "⚠️ အနှစ်ချုပ်ချင်တဲ့ စာအရှည်ကို Reply ထောက်ပြီး `/tldr` ရိုက်ပါ။", parse_mode=ParseMode.MARKDOWN)
        return

    tier = "super" if is_super(update.effective_user.id) else "standard"
    try:
        summary, _ = await run_ai_router(f"Summarize the following text into 3 to 5 clear bullet points in the same language (Burmese or English):\n\n{raw[:3500]}", tier=tier)
        await reply_tracked(update.message, f"⚡ *အဓိက အနှစ်ချုပ် (AI TL;DR):*\n\n{summary}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ TL;DR Error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# Special 7: /factcheck (Web Search + AI Claim Verifier)
async def cmd_factcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    claim = ""
    if update.message.reply_to_message:
        claim = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    elif context.args:
        claim = " ".join(context.args)

    claim = clean_text(claim)
    if not claim:
        await reply_tracked(update.message, "⚠️ စစ်ဆေးချင်တဲ့ သတင်းစာသားကို Reply ထောက်ပြီး `/factcheck` ရိုက်ပါ။", parse_mode=ParseMode.MARKDOWN)
        return

    wait_msg = await reply_tracked(update.message, "🔍 သတင်းရင်းမြစ်များနှင့် တိုက်ဆိုင်စစ်ဆေးနေပါသည်...")
    web_hits = await live_web_search(claim[:120], max_results=4)
    evidence = "\n".join([f"- {h['title']}: {h['snippet']} ({h['url']})" for h in web_hits]) or "No direct indexed reports found."
    tier = "super" if is_super(update.effective_user.id) else "standard"

    prompt = (
        f"Fact-check this claim using the live search evidence below. Respond in Burmese clearly with Verdict (မှန်ကန် / မမှန်ကန် / အတည်မပြုနိုင်သေး) and brief explanation:\n\n"
        f"Claim: {claim}\n\nEvidence:\n{evidence}"
    )
    try:
        verdict, _ = await run_ai_router(prompt, tier=tier)
        await wait_msg.edit_text(f"🔍 *Fact-Check စစ်ဆေးချက်*\n\n{verdict[:3800]}", parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception as e:
        await wait_msg.edit_text(f"⚠️ FactCheck Error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# Special 8: /grammar (English Grammar & Writing Coach)
async def cmd_grammar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    sentence = " ".join(context.args).strip()
    if not sentence and update.message.reply_to_message:
        sentence = update.message.reply_to_message.text or ""
    if not sentence:
        await reply_tracked(update.message, "⚠️ English စာကြောင်း စစ်ဆေးပြင်ဆင်ရန်: `/grammar She go to school yesterday`", parse_mode=ParseMode.MARKDOWN)
        return

    tier = "super" if is_super(update.effective_user.id) else "standard"
    prompt = (
        f"Check and correct the English grammar of this text. Show:\n"
        f"1) ✅ Corrected Sentence\n"
        f"2) 💡 Natural/Native Version\n"
        f"3) 📝 Brief explanation of mistakes in Burmese (မြန်မာလို ရှင်းပြချက်).\n\nText: {sentence}"
    )
    try:
        res, _ = await run_ai_router(prompt, tier=tier)
        await reply_tracked(update.message, f"✍️ *Grammar & Writing Check*\n\n{res[:3800]}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Grammar Error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# Special 9: /flashcard (AI Study Flashcards Generator)
async def cmd_flashcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    topic = " ".join(context.args).strip()
    if not topic:
        await reply_tracked(update.message, "⚠️ စာမေးပွဲအတွက် Flashcard ထုတ်ရန်: `/flashcard Photosynthesis`", parse_mode=ParseMode.MARKDOWN)
        return

    tier = "super" if is_super(update.effective_user.id) else "standard"
    prompt = (
        f"Create 5 concise study flashcards (Q & A pairs) on the topic: '{topic}'. "
        f"Write in Burmese (with key English terms in parentheses) so a student can memorize easily."
    )
    try:
        cards, _ = await run_ai_router(prompt, tier=tier)
        await reply_tracked(update.message, f"🗂 *Study Flashcards — {topic}*\n\n{cards[:3800]}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Flashcard Error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# Special 10: /qimg (HD Quote Card Generator)
async def cmd_qimg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    if not update.message.reply_to_message:
        await reply_tracked(update.message, "⚠️ Quote ပုံထုတ်ချင်တဲ့ စာသားကို Reply ထောက်ပြီး `/qimg` ရိုက်ပါ။", parse_mode=ParseMode.MARKDOWN)
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


# Special 11: /news (Live Myanmar & World RSS News Hub + Posters)
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
    return m.group(1) if m else ""


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
                articles.append({
                    "source": source_name,
                    "title": title,
                    "summary": smart_truncate(summary, 950),
                    "link": entry.get("link", ""),
                    "published": entry.get("published", "Live")[:25],
                    "image": _extract_entry_image(entry),
                })
        except Exception:
            continue
    return articles[:8]


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_special(update.effective_user.id):
        return
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇲🇲 ပြည်တွင်းသတင်း (Myanmar)", callback_data="news_cat_mm"),
            InlineKeyboardButton("🌍 ပြည်ပသတင်း (World)", callback_data="news_cat_world"),
        ]
    ])
    banner = await asyncio.to_thread(_generate_news_banner, "MOBEHO NEWS HUB", "Real-Time Myanmar & Global Headlines", (0, 255, 255))
    caption = "📰 *Live သတင်းဌာန (Daily News Hub)*\nဖတ်ရှုလိုသော သတင်းအမျိုးအစားကို အောက်ပါခလုတ်မှ ရွေးချယ်ပါ:"
    sent = await update.message.reply_photo(photo=banner, caption=caption, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    track_bot_msg(update.effective_chat.id, sent.message_id)


# ==============================================================================
# 3. PERMITTED COMMANDS (13 COMMANDS: SUPER + SPECIAL + PERMITTED)
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
    if not update.message.reply_to_message:
        await reply_tracked(update.message, "⚠️ Reply ထောက်ပြီး `/selsen` လို့ ရိုက်ပါ။", parse_mode=ParseMode.MARKDOWN)
        return
    if not bot_data_store["spam_texts"]:
        await reply_tracked(update.message, "⚠️ စာသားစာရင်း အလွတ်ဖြစ်နေပါတယ်။ `/addtext` နဲ့ အရင်ထည့်ပါ။", parse_mode=ParseMode.MARKDOWN)
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
        await reply_tracked(update.message, "🛑 ရပ်တန့်လိုက်ပါပြီ။")
    else:
        await reply_tracked(update.message, "ℹ️ လက်ရှိ ပို့နေသော စာမရှိပါ။")


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
    text = " ".join(context.args).strip()
    if not text:
        await reply_tracked(update.message, "⚠️ ထည့်ချင်တဲ့ စာသား ရိုက်ထည့်ပါ: `/addtext မင်္ဂလာပါ`", parse_mode=ParseMode.MARKDOWN)
        return
    bot_data_store["spam_texts"].append(text)
    await save_to_jsonbin(force=True)
    await reply_tracked(update.message, f"✅ စာသားအသစ် ထည့်ပြီးပါပြီ (စုစုပေါင်း: `{len(bot_data_store['spam_texts'])}` ကြောင်း)", parse_mode=ParseMode.MARKDOWN)


async def cmd_listtext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    texts = bot_data_store["spam_texts"]
    if not texts:
        await reply_tracked(update.message, "📭 သိမ်းထားသော စာသား မရှိပါ။")
        return
    body = "\n".join([f"`{i+1}.` {t}" for i, t in enumerate(texts)])
    await reply_tracked(update.message, "📜 *သိမ်းထားသော စာသားများ:*\n" + body, parse_mode=ParseMode.MARKDOWN)


async def cmd_edittext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await reply_tracked(update.message, "⚠️ အသုံးပြုပုံ: `/edittext <နံပါတ်> <စာသားအသစ်>`", parse_mode=ParseMode.MARKDOWN)
        return
    idx = int(context.args[0]) - 1
    new_text = " ".join(context.args[1:]).strip()
    if idx < 0 or idx >= len(bot_data_store["spam_texts"]):
        await reply_tracked(update.message, "❌ နံပါတ် မှားယွင်းနေပါသည်။")
        return
    bot_data_store["spam_texts"][idx] = new_text
    await save_to_jsonbin(force=True)
    await reply_tracked(update.message, f"✏️ နံပါတ် `{idx+1}` ကို ပြင်ဆင်ပြီးပါပြီ။", parse_mode=ParseMode.MARKDOWN)


async def cmd_removetext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await reply_tracked(update.message, "⚠️ အသုံးပြုပုံ: `/removetext <နံပါတ်>`", parse_mode=ParseMode.MARKDOWN)
        return
    idx = int(context.args[0]) - 1
    if idx < 0 or idx >= len(bot_data_store["spam_texts"]):
        await reply_tracked(update.message, "❌ နံပါတ် မှားယွင်းနေပါသည်။")
        return
    removed = bot_data_store["spam_texts"].pop(idx)
    await save_to_jsonbin(force=True)
    await reply_tracked(update.message, f"🗑 ဖျက်လိုက်သော စာသား: `{removed}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_cleartext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    bot_data_store["spam_texts"] = []
    await save_to_jsonbin(force=True)
    await reply_tracked(update.message, "🗑 စာသားအားလုံးကို ဖျက်သိမ်းပြီးပါပြီ။")


async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    arg = context.args[0].lower() if context.args else ""

    if arg == "on":
        if chat_id not in bot_data_store["quiz_enabled_chats"]:
            bot_data_store["quiz_enabled_chats"].append(chat_id)
            await save_to_jsonbin(force=True)
        await reply_tracked(update.message, "✅ *Live Unlimited Quiz* စနစ်ကို ဖွင့်လိုက်ပါပြီ! (မေးခွန်းလုံးဝ မထပ်ဘဲ ၅ မိနစ်တစ်ခါ မေးပေးပါမည်)")
        await send_quiz_to_chat(context, chat_id)
    elif arg == "off":
        if chat_id in bot_data_store["quiz_enabled_chats"]:
            bot_data_store["quiz_enabled_chats"].remove(chat_id)
            await save_to_jsonbin(force=True)
        await reply_tracked(update.message, "🛑 Live Quiz စနစ်ကို ပိတ်လိုက်ပါပြီ။")
    else:
        status = "ON 🟢" if chat_id in bot_data_store["quiz_enabled_chats"] else "OFF 🔴"
        await reply_tracked(update.message, f"🧠 Live Quiz အခြေအနေ: *{status}*\nဖွင့်ရန် `/quiz on`၊ ပိတ်ရန် `/quiz off` ရိုက်ပါ။", parse_mode=ParseMode.MARKDOWN)


async def send_quiz_to_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    q_item = await fetch_live_unique_quiz(chat_id)
    buttons = [
        [InlineKeyboardButton(text=f"{['က', 'ခ', 'ဂ'][i]}။ {opt}", callback_data=f"quiz_{chat_id}_{i}")]
        for i, opt in enumerate(q_item["opts"])
    ]
    markup = InlineKeyboardMarkup(buttons)
    text = (
        f"🧠 *Live ဗဟုသုတ Quiz ({q_item.get('source', 'Live')}) — ၁ မိနစ်*\n\n"
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
    reason = " ".join(context.args).strip() or "အနားယူနေပါသည်"
    afk_users[uid] = {"reason": reason, "since": time.time(), "name": update.effective_user.first_name}
    await reply_tracked(update.message, f"💤 *{update.effective_user.first_name}* သည် AFK ဖြစ်သွားပါပြီ။\n📝 အကြောင်းပြချက်: _{reason}_", parse_mode=ParseMode.MARKDOWN)


async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    uid = str(update.effective_user.id)
    user_notes = bot_data_store["notes"].setdefault(uid, {})
    if not context.args:
        await reply_tracked(update.message, "⚠️ အသုံးပြုပုံ:\n• သိမ်းရန်: `/note <ခေါင်းစဉ်> <စာသား>`\n• ကြည့်ရန်: `/note <ခေါင်းစဉ်>`\n• ဖျက်ရန်: `/note del <ခေါင်းစဉ်>`", parse_mode=ParseMode.MARKDOWN)
        return
    if context.args[0].lower() == "del" and len(context.args) >= 2:
        key = context.args[1].lower()
        if key in user_notes:
            del user_notes[key]
            await save_to_jsonbin(force=True)
            await reply_tracked(update.message, f"🗑 မှတ်စု `{key}` ကို ဖျက်ပြီးပါပြီ။", parse_mode=ParseMode.MARKDOWN)
        else:
            await reply_tracked(update.message, f"❌ မှတ်စု `{key}` မတွေ့ပါ။", parse_mode=ParseMode.MARKDOWN)
        return
    key = context.args[0].lower()
    if len(context.args) == 1:
        if key in user_notes:
            await reply_tracked(update.message, f"📌 *{key}:*\n{user_notes[key]}", parse_mode=ParseMode.MARKDOWN)
        else:
            await reply_tracked(update.message, f"❌ မှတ်စု `{key}` မတွေ့ပါ။", parse_mode=ParseMode.MARKDOWN)
        return
    user_notes[key] = " ".join(context.args[1:])
    await save_to_jsonbin(force=True)
    await reply_tracked(update.message, f"✅ မှတ်စု `{key}` ကို သိမ်းဆည်းပြီးပါပြီ။", parse_mode=ParseMode.MARKDOWN)


async def cmd_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_permitted(update.effective_user.id):
        return
    uid = str(update.effective_user.id)
    user_notes = bot_data_store["notes"].get(uid, {})
    if not user_notes:
        await reply_tracked(update.message, "📭 သိမ်းထားသော မှတ်စု မရှိသေးပါ။")
        return
    items = "\n".join([f"• `{k}` (ခေါ်ကြည့်ရန် `/note {k}`)" for k in user_notes.keys()])
    await reply_tracked(update.message, "📒 *သင်၏ ကိုယ်ပိုင်မှတ်စု စာရင်း:*\n" + items, parse_mode=ParseMode.MARKDOWN)


# ==============================================================================
# 4. PUBLIC / EVERYONE COMMANDS (19 LIGHTWEIGHT & STUDY COMMANDS)
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
    year_est = estimate_account_year(target.id)
    if is_super(target.id):
        role = "👑 Super Owner"
    elif is_special(target.id):
        role = "⭐ Co-Owner (Special)"
    elif is_permitted(target.id):
        role = "🔐 Permitted"
    else:
        role = "👤 Member"

    text = (
        f"🪪 *အကောင့် အချက်အလက် စစ်ဆေးချက်*\n\n"
        f"• အမည်: *{target.full_name}*\n"
        f"• Username: @{target.username or 'မရှိပါ'}\n"
        f"• User ID: `{target.id}`\n"
        f"• အဆင့်: {role}\n"
        f"• အကောင့်ဖွင့်ခဲ့သည့် ခန့်မှန်းနှစ်: *{year_est}*"
    )
    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)


async def cmd_wiki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()
    if not query:
        await reply_tracked(update.message, "⚠️ ရှာချင်တဲ့ ခေါင်းစဉ် ရိုက်ထည့်ပါ: `/wiki Myanmar`", parse_mode=ParseMode.MARKDOWN)
        return

    wiki_lang = "my" if any("\u1000" <= ch <= "\u109F" for ch in query) else "en"
    headers = {"User-Agent": "MobehoBot/4.1 (https://t.me)"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            search_url = f"https://{wiki_lang}.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(query)}&format=json&srlimit=1"
            async with session.get(search_url, timeout=10) as sresp:
                sdata = await sresp.json()
                hits = sdata.get("query", {}).get("search", [])
                if not hits:
                    await reply_tracked(update.message, f"❌ `{query}` အတွက် Wikipedia အချက်အလက် မတွေ့ပါ။", parse_mode=ParseMode.MARKDOWN)
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


async def cmd_movie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip()
    if not query:
        await reply_tracked(update.message, "⚠️ ဇာတ်ကား သို့မဟုတ် Series နာမည် ရိုက်ပါ: `/movie Inception`", parse_mode=ParseMode.MARKDOWN)
        return

    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        itunes_url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&media=movie&limit=1"
        try:
            async with session.get(itunes_url, timeout=10) as resp:
                if resp.status == 200:
                    data = json.loads(await resp.text())
                    results = data.get("results", [])
                    if results:
                        m = results[0]
                        title = m.get("trackName", query)
                        director = m.get("artistName", "N/A")
                        release = (m.get("releaseDate") or "")[:10] or "N/A"
                        genre = m.get("primaryGenreName", "Movie")
                        advisory = m.get("contentAdvisoryRating", "NR")
                        desc = smart_truncate(m.get("longDescription") or m.get("shortDescription") or "No description.", 620)
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

    await reply_tracked(update.message, f"❌ `{query}` ဇာတ်ကား/Series ကို ရှာမတွေ့ပါ။", parse_mode=ParseMode.MARKDOWN)


async def cmd_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    lines = ["🕒 *အရှေ့တောင်အာရှ (၁၁) နိုင်ငံ လက်ရှိအချိန်များ*\n"]
    for name, offset in sea_zones:
        local_dt = now_utc + timedelta(hours=offset)
        lines.append(f"• {name}: `{local_dt.strftime('%I:%M:%S %p')} ({local_dt.strftime('%d %b')})`")
    await reply_tracked(update.message, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_gevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    year = datetime.now(timezone.utc).year
    lines = [
        f"📅 *မြန်မာ့အဓိက နေ့ထူးနေ့မြတ်များ ({year})*",
        "• *ဇန်နဝါရီ ၄* — လွတ်လပ်ရေးနေ့ 🇲🇲",
        "• *ဖေဖော်ဝါရီ ၁၂* — ပြည်ထောင်စုနေ့ 🇲🇲",
        "• *ဧပြီ ၁၃-၁၆* — မဟာသင်္ကြန်ပွဲတော် 💦",
        "• *ဇူလိုင် ၁၉* — အာဇာနည်နေ့ 🕯",
        "• *သီတင်းကျွတ် / တန်ဆောင်တိုင်* — မီးထွန်းပွဲတော်များ 🏮\n",
    ]
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
    try:
        mm_q = await async_translate(en_q, target="my")
        text = f"💡 *ယနေ့အတွက် မှတ်သားဖွယ် စကားစု (Live)*\n\n_\"{mm_q}\"_\n\n_\"{en_q}\"_\n— *{author}*"
    except Exception:
        text = f"💡 *Quote of the Moment*\n\n_\"{en_q}\"_\n— *{author}*"
    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)


async def cmd_topq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scores = bot_data_store["quiz_scores"]
    if not scores:
        await reply_tracked(update.message, "🏆 Quiz ရမှတ်စာရင်း မရှိသေးပါ။ `/quiz on` ဖွင့်ပြီး ဖြေဆိုနိုင်ပါပြီ!", parse_mode=ParseMode.MARKDOWN)
        return
    sorted_users = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)[:10]
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 *Live Quiz ထိပ်တန်းစာရင်း (Top 10)*\n"]
    for idx, (uid, info) in enumerate(sorted_users):
        badge = medals[idx] if idx < 3 else f"`{idx+1}.`"
        lines.append(f"{badge} *{info['name']}* — `{info['score']} pts`")
    await reply_tracked(update.message, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["💱 *Live ငွေလဲနှုန်းနှင့် Global Crypto ပေါက်ဈေး*\n"]
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        try:
            async with session.get("https://open.er-api.com/v6/latest/USD", timeout=10) as r1:
                fx = await r1.json()
                rates = fx.get("rates", {})
                lines.append("💵 *Fiat Exchange (1 USD Base):*")
                lines.append(f"• 🇲🇲 MMK (Ref): `{rates.get('MMK', 2100.0):,.2f} MMK`")
                lines.append(f"• 🇹🇭 THB: `{rates.get('THB', 34.0):,.2f} THB`")
                lines.append(f"• 🇸🇬 SGD: `{rates.get('SGD', 1.30):,.2f} SGD`")
                lines.append(f"• 🇯🇵 JPY: `{rates.get('JPY', 155.0):,.2f} JPY`")
                lines.append(f"• 🇨🇳 CNY: `{rates.get('CNY', 7.20):,.2f} CNY`\n")
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
    oz_usd = None
    thb_rate = 34.0
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        try:
            async with session.get("https://open.er-api.com/v6/latest/USD", timeout=8) as r_fx:
                thb_rate = (await r_fx.json()).get("rates", {}).get("THB", 34.0)
        except Exception:
            pass
        try:
            async with session.get("https://api.gold-api.com/price/XAU", timeout=8) as resp:
                if resp.status == 200:
                    oz_usd = float((await resp.json())["price"])
        except Exception:
            pass
        if not oz_usd:
            try:
                async with session.get("https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT", timeout=8) as resp2:
                    if resp2.status == 200:
                        oz_usd = float((await resp2.json())["price"])
            except Exception:
                pass
    if not oz_usd:
        await reply_tracked(update.message, "⚠️ ကမ္ဘာ့ရွှေဈေး ဆွဲယူ၍ မရသေးပါ။ ခဏနေ ပြန်စမ်းကြည့်ပါ။")
        return
    oz_thb = oz_usd * thb_rate
    kyatthar_usd = oz_usd * (16.606 / 31.1035)
    text = (
        f"🥇 *ကမ္ဘာ့ရွှေဈေးနှုန်း (Live Spot Gold XAU)*\n\n"
        f"• 1 Troy Ounce (31.1g): `${oz_usd:,.2f}` (`{oz_thb:,.0f} THB`)\n"
        f"• ၁ ကျပ်သား (16.6g) ညီမျှကမ္ဘာ့ဈေး: `${kyatthar_usd:,.2f} USD`"
    )
    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)


async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                    text = (
                        f"🌤 *မိုးလေဝသ အခြေအနေ — {loc_label}*\n\n"
                        f"• ☁️ အခြေအနေ: *{desc}*\n"
                        f"• 🌡 အပူချိန်: `{temp_c}°C` (ခံစားရမှု `{feels_c}°C`)\n"
                        f"• 💧 စိုထိုင်းဆ: `{humidity}%`\n"
                        f"• 💨 လေတိုက်နှုန်း: `{wind} km/h`"
                    )
                    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)
                    return
        except Exception:
            pass
    await reply_tracked(update.message, f"⚠️ `{city}` မိုးလေဝသ အချက်အလက် ရှာမတွေ့ပါ။", parse_mode=ParseMode.MARKDOWN)


async def cmd_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = " ".join(context.args).strip()
    if not data and update.message.reply_to_message:
        data = update.message.reply_to_message.text or ""
    if not data:
        await reply_tracked(update.message, "⚠️ QR ပြောင်းချင်တဲ့ Link သို့မဟုတ် စာသား ရိုက်ထည့်ပါ: `/qr https://t.me`", parse_mode=ParseMode.MARKDOWN)
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
    expr = "".join(context.args).strip()
    if not expr:
        await reply_tracked(update.message, "⚠️ တွက်ချက်လိုသော ပုစ္ဆာရိုက်ပါ: `/calc (2500*4)+1500`", parse_mode=ParseMode.MARKDOWN)
        return
    if not re.match(r"^[0-9+\-*/().%^ ]+$", expr):
        await reply_tracked(update.message, "❌ ဂဏန်းနှင့် သင်္ချာသင်္ကေတများသာ ရိုက်ထည့်ပါ။")
        return
    try:
        result = eval(expr.replace("^", "**"), {"__builtins__": None}, {})
        await reply_tracked(update.message, f"🧮 `{expr}` = *`{result:,}`*" if isinstance(result, (int, float)) else f"🧮 `{expr}` = *`{result}`*", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await reply_tracked(update.message, "❌ တွက်ချက်၍ မရပါ။ ပုစ္ဆာပုံစံ ပြန်စစ်ပါ။")


async def cmd_dict(update: Update, context: ContextTypes.DEFAULT_TYPE):
    word = context.args[0].strip().lower() if context.args else ""
    if not word:
        await reply_tracked(update.message, "⚠️ အင်္ဂလိပ်စကားလုံး ရိုက်ပါ: `/dict resilience`", parse_mode=ParseMode.MARKDOWN)
        return
    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}"
    defn_text, pos, phonetic = "", "noun", ""
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(url, timeout=10) as resp:
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
        await reply_tracked(update.message, f"❌ Word `{word}` not found.", parse_mode=ParseMode.MARKDOWN)
        return
    lines = [f"📖 *{word.capitalize()}* `{phonetic}`", f"🇲🇲 မြန်မာအဓိပ္ပာယ်: *{mm_word}*"]
    if defn_text:
        lines.append(f"🏷 Type: _{pos}_")
        lines.append(f"📝 Definition: {defn_text}")
    await reply_tracked(update.message, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# Study Tool 1 (Public): /pomodoro <minutes>
async def cmd_pomodoro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mins = 25
    if context.args and context.args[0].isdigit():
        mins = max(1, min(120, int(context.args[0])))
    chat_id = update.effective_chat.id
    user = update.effective_user
    key = (chat_id, user.id)

    if key in pomodoro_tasks and not pomodoro_tasks[key].done():
        pomodoro_tasks[key].cancel()

    await reply_tracked(
        update.message,
        f"⏱ *Pomodoro စာကျက်ချိန် စတင်ပါပြီ!*\n👤 {user.first_name} — `{mins}` မိနစ်ပြည့်ရင် Bot က ပြန်လာသတိပေးပါမယ်။",
        parse_mode=ParseMode.MARKDOWN,
    )

    async def _timer():
        try:
            await asyncio.sleep(mins * 60)
            mention = f"<a href='tg://user?id={user.id}'>{html.escape(user.first_name)}</a>"
            await send_tracked(
                context,
                chat_id,
                f"🔔 {mention} <b>Pomodoro {mins} မိနစ် ပြည့်ပါပြီ!</b> ၅ မိနစ်လောက် အနားယူလိုက်ပါဦး ☕️",
                parse_mode=ParseMode.HTML,
            )
        except asyncio.CancelledError:
            pass

    pomodoro_tasks[key] = asyncio.create_task(_timer())


# Study Tool 2 (Public): /periodic <symbol or atomic_num>
PERIODIC_DB = {
    "h": ("1", "Hydrogen (ဟိုက်ဒရိုဂျင်)", "1.008", "Nonmetal"),
    "he": ("2", "Helium (ဟီလီယမ်)", "4.0026", "Noble Gas"),
    "c": ("6", "Carbon (ကာဗွန်)", "12.011", "Nonmetal"),
    "n": ("7", "Nitrogen (နိုက်ထရိုဂျင်)", "14.007", "Nonmetal"),
    "o": ("8", "Oxygen (အောက်ဆီဂျင်)", "15.999", "Nonmetal"),
    "na": ("11", "Sodium (ဆိုဒီယမ်)", "22.990", "Alkali Metal"),
    "mg": ("12", "Magnesium (မဂ္ဂနီဆီယမ်)", "24.305", "Alkaline Earth"),
    "al": ("13", "Aluminium (အလူမီနီယမ်)", "26.982", "Post-Transition Metal"),
    "si": ("14", "Silicon (ဆီလီကွန်)", "28.085", "Metalloid"),
    "cl": ("17", "Chlorine (ကလိုရင်း)", "35.45", "Halogen"),
    "k": ("19", "Potassium (ပိုတက်ဆီယမ်)", "39.098", "Alkali Metal"),
    "ca": ("20", "Calcium (ကယ်လ်စီယမ်)", "40.078", "Alkaline Earth"),
    "fe": ("26", "Iron (သံ)", "55.845", "Transition Metal"),
    "cu": ("29", "Copper (ကြေးနီ)", "63.546", "Transition Metal"),
    "zn": ("30", "Zinc (သွပ်)", "65.38", "Transition Metal"),
    "ag": ("47", "Silver (ငွေ)", "107.87", "Transition Metal"),
    "au": ("79", "Gold (ရွှေ)", "196.97", "Transition Metal"),
    "hg": ("80", "Mercury (ပြဒါး)", "200.59", "Transition Metal"),
    "pb": ("82", "Lead (ခဲ)", "207.2", "Post-Transition Metal"),
    "u": ("92", "Uranium (ယူရေနီယမ်)", "238.03", "Actinide"),
}


async def cmd_periodic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = context.args[0].strip().lower() if context.args else ""
    if not q:
        await reply_tracked(update.message, "⚠️ ဓာတုဒြပ်စင် သင်္ကေတ သို့မဟုတ် နံပါတ် ရိုက်ပါ: `/periodic Au`", parse_mode=ParseMode.MARKDOWN)
        return
    found = PERIODIC_DB.get(q)
    if not found:
        for sym, info in PERIODIC_DB.items():
            if info[0] == q or q in info[1].lower():
                found = (info[0], f"{info[1]} [{sym.upper()}]", info[2], info[3])
                break
    if not found:
        await reply_tracked(update.message, f"❌ `{q}` ဒြပ်စင် မတွေ့ပါ (ဥပမာ: `H`, `O`, `Fe`, `Au`, `79`)။", parse_mode=ParseMode.MARKDOWN)
        return
    await reply_tracked(
        update.message,
        f"⚛️ *Periodic Table Element*\n\n"
        f"• 🔢 Atomic Number: `{found[0]}`\n"
        f"• 🧪 Name: *{found[1]}*\n"
        f"• ⚖️ Atomic Mass: `{found[2]} u`\n"
        f"• 🏷 Category: _{found[3]}_",
        parse_mode=ParseMode.MARKDOWN,
    )


# Study Tool 3 (Public): /unit <value> <from> <to>
async def cmd_unit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await reply_tracked(update.message, "⚠️ ယူနစ်ပြောင်းရန်: `/unit 10 km mile` (သို့) `/unit 37 c f`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        val = float(context.args[0])
    except ValueError:
        await reply_tracked(update.message, "❌ ဂဏန်းတန်ဖိုး မှားယွင်းနေပါသည်။")
        return
    u1, u2 = context.args[1].lower(), context.args[2].lower()

    conversions = {
        ("km", "mile"): val * 0.621371,
        ("mile", "km"): val * 1.60934,
        ("m", "ft"): val * 3.28084,
        ("ft", "m"): val * 0.3048,
        ("cm", "inch"): val * 0.393701,
        ("inch", "cm"): val * 2.54,
        ("kg", "lb"): val * 2.20462,
        ("lb", "kg"): val * 0.453592,
        ("c", "f"): (val * 9 / 5) + 32,
        ("f", "c"): (val - 32) * 5 / 9,
        ("c", "k"): val + 273.15,
        ("k", "c"): val - 273.15,
    }
    res = conversions.get((u1, u2))
    if res is None:
        await reply_tracked(update.message, "⚠️ Supported units: `km/mile`, `m/ft`, `cm/inch`, `kg/lb`, `c/f/k`", parse_mode=ParseMode.MARKDOWN)
        return
    await reply_tracked(update.message, f"📏 *Unit Conversion:* `{val:g} {u1.upper()}` = *`{res:,.4f} {u2.upper()}`*", parse_mode=ParseMode.MARKDOWN)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime_sec = int(time.time() - START_TIME)
    hrs, rem = divmod(uptime_sec, 3600)
    mins, secs = divmod(rem, 60)
    top_player = "None"
    if bot_data_store["quiz_scores"]:
        best = max(bot_data_store["quiz_scores"].values(), key=lambda x: x["score"])
        top_player = f"{best['name']} ({best['score']} pts)"
    text = (
        f"📊 *Mobeho Bot စာရင်းချုပ် (Dashboard)*\n\n"
        f"• ⏱ Uptime: `{hrs}h {mins}m {secs}s`\n"
        f"• ⭐ Co-Owners (Special): `{len(bot_data_store['special_users'])}` ဦး\n"
        f"• 🔐 Permitted Users: `{len(bot_data_store['permitted_users'])}` ဦး\n"
        f"• 🤖 AI Chat Active Groups: `{len(bot_data_store['aichat_enabled_chats'])}` ခု\n"
        f"• 🧠 Live Quiz Active Groups: `{len(bot_data_store['quiz_enabled_chats'])}` ခု\n"
        f"• 🏆 Quiz ထိပ်ဆုံး: *{top_player}*\n"
        f"• 💬 ချိတ်ဆက်ထားသော Chat: `{len(bot_data_store['known_chats'])}` ခု"
    )
    await reply_tracked(update.message, text, parse_mode=ParseMode.MARKDOWN)


async def cmd_ship(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u1 = update.effective_user.first_name
    if update.message.reply_to_message:
        u2 = update.message.reply_to_message.from_user.first_name
    elif context.args:
        u2 = " ".join(context.args)
    else:
        u2 = "Mystery Soulmate"
    score = random.randint(15, 100)
    bar = "❤️" * (score // 20) + "🤍" * (5 - (score // 20))
    await reply_tracked(update.message, f"💘 *အချစ်ရာခိုင်နှုန်း တိုင်းတာချက်*\n\n*{u1}* 💞 *{u2}*\nရလဒ်: *{score}%* {bar}", parse_mode=ParseMode.MARKDOWN)


async def cmd_slang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    term = " ".join(context.args).strip()
    if not term:
        await reply_tracked(update.message, "⚠️ ရှာချင်တဲ့ ဗန်းစကား ရိုက်ပါ: `/slang goated`", parse_mode=ParseMode.MARKDOWN)
        return
    url = f"https://api.urbandictionary.com/v0/define?term={urllib.parse.quote(term)}"
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(url, timeout=10) as resp:
                items = (await resp.json()).get("list", [])
                if not items:
                    await reply_tracked(update.message, f"❌ No slang definition found for `{term}`.", parse_mode=ParseMode.MARKDOWN)
                    return
                top = items[0]
                defn = smart_truncate(top.get("definition", "").replace("[", "").replace("]", ""), 650)
                ex = smart_truncate(top.get("example", "").replace("[", "").replace("]", ""), 300)
                await reply_tracked(update.message, f"🔥 *Slang: {term}*\n\n📖 {defn}\n\n💬 _Example:_ {ex}", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await reply_tracked(update.message, f"⚠️ Slang error: `{e}`", parse_mode=ParseMode.MARKDOWN)


# ==============================================================================
# 5. BOT DM ONLY COMMAND (/cmlist)
# ==============================================================================
async def cmd_cmlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    uid = update.effective_user.id
    sections = []

    if is_super(uid):
        sections.append(
            "👑 *1. Super Commands (14 — Primary Owner Only)*\n"
            "• `/suser a` / `/suser d` — Add or remove Co-Owner\n"
            "• `/listsuser` — List all Co-Owners\n"
            "• `/apermit` / `/rmpermit` / `/listpermit` — Manage permitted users\n"
            "• `/broadcast` — Broadcast announcement to all chats\n"
            "• `/ask <q>` — Gemini 2.0 Flash VIP + Multi-AI + Vision\n"
            "• `/aiweb <q>` — Real-Time Web Search + Gemini 2.0 Flash\n"
            "• `/think <q>` — DeepSeek-R1 671B Deep Reasoning\n"
            "• `/fast <q>` — Cerebras / Groq Ultra-Fast AI\n"
            "• `/solve` — Step-by-Step Math/Science Solver (Text/Photo)\n"
            "• `/imghd <prompt>` — Dual-Engine HD AI Art (Flux/SDXL)\n"
            "• `/stt` — Voice Note to Text (Groq Whisper)\n"
            "• `/aistatus` — Check all AI API Keys status"
        )

    if is_special(uid):
        sections.append(
            "⭐ *2. Special Commands (11 — Co-Owner & Super)*\n"
            "• `/aichat on|off` — Toggle Group AI Chat (Gemini 2.0 Flash-Lite)\n"
            "• `/gsearch <query>` — Google Knowledge Card + HD Photo\n"
            "• `/tr [lang] <text>` — Natural AI Translator\n"
            "• `/tts <text>` — Burmese/English Female Voice (Nilar)\n"
            "• `/ttsm <text>` — Burmese/English Male Voice (Thiha)\n"
            "• `/tldr` — AI Bullet Summarizer\n"
            "• `/factcheck` — Live Web + AI Fact-Checker\n"
            "• `/grammar <text>` — English Grammar & Writing Coach\n"
            "• `/flashcard <topic>` — Study Flashcards Generator\n"
            "• `/qimg` — Reply to create HD Quote Card\n"
            "• `/news` — Live Myanmar & World News + Posters"
        )

    if is_permitted(uid):
        sections.append(
            "🔐 *3. Permitted Commands (13)*\n"
            "• `/quiz on|off` — Toggle 5-min Live Unlimited Quiz\n"
            "• `/selsen` / `/yat` / `/delmsg` — Loop & message cleanup\n"
            "• `/addtext` / `/listtext` / `/edittext` / `/removetext` / `/cleartext`\n"
            "• `/lang mm|en` — Switch bot language\n"
            "• `/afk [reason]` — Set AFK auto-reply status\n"
            "• `/note` / `/notes` — Personal notes"
        )

    sections.append(
        "🌍 *4. Public / Everyone Commands (19)*\n"
        "• `/cmlist` — View this command menu (DM only)\n"
        "• `/wiki <topic>` — Wikipedia research + photo\n"
        "• `/movie <title>` — Movie & Series info + HD poster\n"
        "• `/check` — Check User ID & est. account year\n"
        "• `/time` — Live time across 11 SEA countries\n"
        "• `/gevent` — Myanmar & Global live holidays\n"
        "• `/qtoday` — Live quote + Burmese translation\n"
        "• `/topq` — Live Quiz Top 10 leaderboard\n"
        "• `/rate` — Live USD/MMK/THB & Crypto rates\n"
        "• `/gold` — Live Global Spot Gold price\n"
        "• `/weather <city>` — Live city weather forecast\n"
        "• `/qr <text/link>` — Generate HD QR code\n"
        "• `/calc <expr>` — Math calculator\n"
        "• `/dict <word>` — English-Myanmar dictionary\n"
        "• `/pomodoro <mins>` — Study timer with mention alert\n"
        "• `/periodic <sym>` — Chemistry Periodic Table\n"
        "• `/unit <val> <from> <to>` — Unit converter\n"
        "• `/stats` — Bot uptime & live statistics\n"
        "• `/ship` & `/slang` — Fun & slang dictionary"
    )

    await reply_tracked(update.message, "🤖 *Mobeho Bot v4.1 — Command Menu (57 Commands)*\n\n" + "\n\n".join(sections), parse_mode=ParseMode.MARKDOWN)


# ==============================================================================
# 6. INLINE CALLBACK ROUTER (QUIZ & NEWS)
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
        if not is_special(user.id):
            await query.answer("🔒 Co-Owner / Owner Only Feature", show_alert=True)
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
        lines, btn_row, btn_rows = [], [], []
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
        articles = news_cache.get(f"{chat_id}_{cat}", [])
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
                sent = await context.bot.send_photo(chat_id=chat_id, photo=art["image"], caption=article_text[:1020], reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
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
# 7. GENERAL MESSAGE LISTENER (AFK, #NOTE & GROUP /AICHAT CONVERSATIONS)
# ==============================================================================
async def general_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    chat = update.effective_chat
    user = update.effective_user
    track_chat(chat.id)
    text = (update.message.text or "").strip()
    if not text:
        return

    # 1. AFK Auto-Removal & Mention Notification
    if user.id in afk_users and not text.startswith("/afk"):
        del afk_users[user.id]
        await reply_tracked(update.message, f"👋 *{user.first_name}* ပြန်လည်ရောက်ရှိလာပါပြီ (AFK ဖြုတ်ပြီး)။", parse_mode=ParseMode.MARKDOWN)

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        replied_uid = update.message.reply_to_message.from_user.id
        if replied_uid in afk_users and replied_uid != user.id:
            info = afk_users[replied_uid]
            mins = max(1, int((time.time() - info["since"]) // 60))
            await reply_tracked(
                update.message,
                f"💤 *{info['name']}* သည် လက်ရှိ AFK ဖြစ်နေပါသည် ({mins} မိနစ်ကြာ)\n📝 အကြောင်းပြချက်: _{info['reason']}_",
                parse_mode=ParseMode.MARKDOWN,
            )

    # 2. #note Shortcut
    if text.startswith("#") and len(text) > 1 and is_permitted(user.id):
        key = text[1:].split()[0].lower()
        user_notes = bot_data_store["notes"].get(str(user.id), {})
        if key in user_notes:
            await reply_tracked(update.message, f"📌 *{key}:*\n{user_notes[key]}", parse_mode=ParseMode.MARKDOWN)
            return

    # 3. Group AI Chat (/aichat on) Smart Trigger
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and chat.id in bot_data_store["aichat_enabled_chats"]:
        bot_username = (context.bot.username or "").lower()
        is_reply_to_bot = (
            update.message.reply_to_message
            and update.message.reply_to_message.from_user
            and update.message.reply_to_message.from_user.id == context.bot.id
        )
        is_mentioned = bot_username and f"@{bot_username}" in text.lower()
        is_ai_prefix = text.lower().startswith("ai ")

        if is_reply_to_bot or is_mentioned or is_ai_prefix:
            clean_q = text
            if bot_username:
                clean_q = re.sub(rf"@{re.escape(bot_username)}", "", clean_q, flags=re.IGNORECASE).strip()
            if clean_q.lower().startswith("ai "):
                clean_q = clean_q[3:].strip()
            if not clean_q:
                return

            tier = "super" if is_super(user.id) else "standard"
            hist = aichat_history.setdefault(chat.id, [])
            sys_prompt = (
                "You are Mobeho AI inside a Telegram group chat. Reply helpfully, naturally, and concisely "
                "in Burmese or English matching the user's message."
            )
            try:
                reply_txt, _ = await run_ai_router(clean_q, tier=tier, system_prompt=sys_prompt, history=hist)
                hist.append({"role": "user", "content": f"{user.first_name}: {clean_q}"})
                hist.append({"role": "assistant", "content": reply_txt})
                if len(hist) > 12:
                    aichat_history[chat.id] = hist[-12:]
                await reply_tracked(update.message, reply_txt, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.warning(f"Group AI Chat error: {e}")


# ==============================================================================
# 8. SCOPED COMMAND MENU SETUP (HIDDEN IN GROUPS, ROLE-BASED IN BOT DM)
# ==============================================================================
async def post_init_setup(app: Application):
    await start_health_server()
    await load_from_jsonbin()

    await app.bot.set_my_commands([], scope=BotCommandScopeAllGroupChats())

    public_dm_commands = [
        BotCommand("cmlist", "View available commands (DM only)"),
        BotCommand("wiki", "Wikipedia summary + photo"),
        BotCommand("movie", "Movie & Series info + HD poster"),
        BotCommand("check", "Check User ID & account year"),
        BotCommand("time", "Live time across 11 SEA countries"),
        BotCommand("gevent", "Live holidays & Myanmar events"),
        BotCommand("qtoday", "Live quote + Burmese translation"),
        BotCommand("topq", "Live Quiz Top 10 leaderboard"),
        BotCommand("rate", "Live currency & crypto rates"),
        BotCommand("gold", "Live Spot Gold price"),
        BotCommand("weather", "Live city weather forecast"),
        BotCommand("qr", "Generate HD QR code"),
        BotCommand("calc", "Math calculator"),
        BotCommand("dict", "English-Myanmar dictionary"),
        BotCommand("pomodoro", "Study timer with mention alert"),
        BotCommand("periodic", "Chemistry Periodic Table"),
        BotCommand("unit", "Physics/Math unit converter"),
        BotCommand("stats", "Bot uptime & live stats"),
        BotCommand("ship", "Matchmaking percentage"),
        BotCommand("slang", "Slang dictionary"),
    ]
    await app.bot.set_my_commands(public_dm_commands, scope=BotCommandScopeAllPrivateChats())

    special_dm_commands = [
        BotCommand("aichat", "[Special] Toggle Group AI Chat on/off"),
        BotCommand("gsearch", "[Special] Google Knowledge Card + Photo"),
        BotCommand("tr", "[Special] Natural AI Translator"),
        BotCommand("tts", "[Special] Burmese/English Female Voice"),
        BotCommand("ttsm", "[Special] Burmese/English Male Voice"),
        BotCommand("tldr", "[Special] AI Bullet Summarizer"),
        BotCommand("factcheck", "[Special] Web + AI Fact-Checker"),
        BotCommand("grammar", "[Special] English Grammar Coach"),
        BotCommand("flashcard", "[Special] Study Flashcards"),
        BotCommand("qimg", "[Special] Create HD Quote Card"),
        BotCommand("news", "[Special] Live Myanmar & World News"),
        BotCommand("quiz", "[Permit] Toggle Live Unlimited Quiz"),
        BotCommand("selsen", "[Permit] Reply & mention loop"),
        BotCommand("yat", "[Permit] Stop active loop"),
        BotCommand("delmsg", "[Permit] Delete bot messages"),
        BotCommand("addtext", "[Permit] Add spam text"),
        BotCommand("listtext", "[Permit] List spam texts"),
        BotCommand("edittext", "[Permit] Edit spam text"),
        BotCommand("removetext", "[Permit] Remove spam text"),
        BotCommand("cleartext", "[Permit] Clear spam texts"),
        BotCommand("lang", "[Permit] Switch language (mm/en)"),
        BotCommand("afk", "[Permit] Set AFK status"),
        BotCommand("note", "[Permit] Save/get/del private note"),
        BotCommand("notes", "[Permit] List private notes"),
    ] + public_dm_commands

    for suid in bot_data_store["special_users"]:
        try:
            await app.bot.set_my_commands(special_dm_commands, scope=BotCommandScopeChat(chat_id=suid))
        except TelegramError:
            pass

    if OWNER_ID:
        super_dm_commands = [
            BotCommand("suser", "[Super] Add/remove Co-Owner (a/d)"),
            BotCommand("listsuser", "[Super] List Co-Owners"),
            BotCommand("apermit", "[Super] Grant user permission"),
            BotCommand("rmpermit", "[Super] Revoke user permission"),
            BotCommand("listpermit", "[Super] List permitted users"),
            BotCommand("broadcast", "[Super] Broadcast to all chats"),
            BotCommand("ask", "[Super AI] Gemini 2.0 Flash + Vision"),
            BotCommand("aiweb", "[Super AI] Live Web Search + AI"),
            BotCommand("think", "[Super AI] DeepSeek-R1 Reasoning"),
            BotCommand("fast", "[Super AI] Ultra-Fast Cerebras/Groq"),
            BotCommand("solve", "[Super AI] Step-by-Step Solver"),
            BotCommand("imghd", "[Super AI] Dual-Engine HD Art"),
            BotCommand("stt", "[Super AI] Voice Note to Text"),
            BotCommand("aistatus", "[Super AI] Check all AI keys"),
        ] + special_dm_commands
        try:
            await app.bot.set_my_commands(super_dm_commands, scope=BotCommandScopeChat(chat_id=OWNER_ID))
        except TelegramError as e:
            logger.warning(f"Could not set Super Owner DM menu yet: {e}")

    asyncio.create_task(periodic_sync_worker())
    asyncio.create_task(auto_quiz_scheduler(app))
    logger.info("Mobeho Bot v4.1 (Multi-AI VIP + Live Quiz + 4-Tier Access) initialized cleanly.")


# ==============================================================================
# 9. RENDER WEB HEALTH-CHECK SERVER & MAIN RUNNER
# ==============================================================================
async def start_health_server():
    app_web = web.Application()
    app_web.router.add_get("/", lambda request: web.Response(text="Mobeho Bot v4.1 is running!"))
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health server listening on port {PORT}")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing!")

    application = Application.builder().token(BOT_TOKEN).post_init(post_init_setup).build()

    # 1. Super Commands (14 — Primary Owner Only)
    application.add_handler(CommandHandler("suser", cmd_suser))
    application.add_handler(CommandHandler("listsuser", cmd_listsuser))
    application.add_handler(CommandHandler("apermit", cmd_apermit))
    application.add_handler(CommandHandler("rmpermit", cmd_rmpermit))
    application.add_handler(CommandHandler("listpermit", cmd_listpermit))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(CommandHandler("ask", cmd_ask))
    application.add_handler(CommandHandler("aiweb", cmd_aiweb))
    application.add_handler(CommandHandler("think", cmd_think))
    application.add_handler(CommandHandler("fast", cmd_fast))
    application.add_handler(CommandHandler("solve", cmd_solve))
    application.add_handler(CommandHandler(["imghd", "imagine"], cmd_imagine))
    application.add_handler(CommandHandler("stt", cmd_stt))
    application.add_handler(CommandHandler("aistatus", cmd_aistatus))

    # 2. Special Commands (11 — Super Owner + Co-Owners)
    application.add_handler(CommandHandler("aichat", cmd_aichat))
    application.add_handler(CommandHandler("gsearch", cmd_gsearch))
    application.add_handler(CommandHandler("tr", cmd_tr))
    application.add_handler(CommandHandler("tts", cmd_tts))
    application.add_handler(CommandHandler("ttsm", cmd_ttsm))
    application.add_handler(CommandHandler("tldr", cmd_tldr))
    application.add_handler(CommandHandler("factcheck", cmd_factcheck))
    application.add_handler(CommandHandler("grammar", cmd_grammar))
    application.add_handler(CommandHandler("flashcard", cmd_flashcard))
    application.add_handler(CommandHandler(["qimg", "quoteimg"], cmd_qimg))
    application.add_handler(CommandHandler(["news", "dailynews"], cmd_news))

    # 3. Permitted Commands (13)
    application.add_handler(CommandHandler("quiz", cmd_quiz))
    application.add_handler(CommandHandler("selsen", cmd_selsen))
    application.add_handler(CommandHandler("yat", cmd_yat))
    application.add_handler(CommandHandler(["delmsg", "deleownmsg"], cmd_delmsg))
    application.add_handler(CommandHandler("addtext", cmd_addtext))
    application.add_handler(CommandHandler("listtext", cmd_listtext))
    application.add_handler(CommandHandler("edittext", cmd_edittext))
    application.add_handler(CommandHandler("removetext", cmd_removetext))
    application.add_handler(CommandHandler(["cleartext", "removealltext"], cmd_cleartext))
    application.add_handler(CommandHandler("lang", cmd_lang))
    application.add_handler(CommandHandler("afk", cmd_afk))
    application.add_handler(CommandHandler("note", cmd_note))
    application.add_handler(CommandHandler("notes", cmd_notes))

    # 4. Public / Everyone Commands (19)
    application.add_handler(CommandHandler("cmlist", cmd_cmlist))
    application.add_handler(CommandHandler("wiki", cmd_wiki))
    application.add_handler(CommandHandler("movie", cmd_movie))
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
    application.add_handler(CommandHandler("pomodoro", cmd_pomodoro))
    application.add_handler(CommandHandler("periodic", cmd_periodic))
    application.add_handler(CommandHandler("unit", cmd_unit))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("ship", cmd_ship))
    application.add_handler(CommandHandler(["slang", "define_slang"], cmd_slang))

    # Inline Buttons & General Messages
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, general_message_handler))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

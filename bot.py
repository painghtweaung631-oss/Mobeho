import os
import sys
import json
import time
import asyncio
import subprocess
import threading
from datetime import datetime, timezone, timedelta
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from telegram.error import RetryAfter, Forbidden, BadRequest

# Auto-install pytz if not present
try:
    import pytz
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pytz"])
    import pytz

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
JSONBIN_ID = os.environ.get("JSONBIN_ID", "")
JSONBIN_KEY = os.environ.get("JSONBIN_KEY", "")
DATA_FILE = "bot_data.json"

active_tasks = {}
sent_messages = {}  # {chat_id: [message_id, ...]}
hourly_quote_cache = {"hour_key": None, "text": None}

DEFAULT_TEXTS = ["ဘာတွေလုပ်နေလဲ", "စားပြီးပြီလား"]

# ================= SOUTHEAST ASIA TIMEZONES =================
SEA_COUNTRIES = [
    ("🇲🇲 Myanmar", "Asia/Yangon"),
    ("🇹🇭 Thailand", "Asia/Bangkok"),
    ("🇱🇦 Laos", "Asia/Vientiane"),
    ("🇰🇭 Cambodia", "Asia/Phnom_Penh"),
    ("🇻🇳 Vietnam", "Asia/Ho_Chi_Minh"),
    ("🇲🇾 Malaysia", "Asia/Kuala_Lumpur"),
    ("🇸🇬 Singapore", "Asia/Singapore"),
    ("🇮🇩 Indonesia (WIB)", "Asia/Jakarta"),
    ("🇧🇳 Brunei", "Asia/Brunei"),
    ("🇵🇭 Philippines", "Asia/Manila"),
    ("🇹🇱 Timor-Leste", "Asia/Dili"),
]

# ================= GLOBAL & NATIONAL EVENTS DATABASE =================
FIXED_EVENTS = {
    "01-01": [
        "🌍 Global: New Year's Day (နှစ်သစ်ကူးနေ့)",
        "🇧🇳 Brunei: Independence Day (ဘရူနိုင်း လွတ်လပ်ရေးနေ့)"
    ],
    "01-04": [
        "🇲🇲 Myanmar: Independence Day (မြန်မာ့လွတ်လပ်ရေးနေ့)"
    ],
    "02-04": [
        "🌍 Global: World Cancer Day (ကမ္ဘာ့ကင်ဆာရောဂါတိုက်ဖျက်ရေးနေ့)"
    ],
    "02-12": [
        "🇲🇲 Myanmar: Union Day (ပြည်ထောင်စုနေ့)"
    ],
    "02-14": [
        "🌍 Global: Valentine's Day (ချစ်သူများနေ့)"
    ],
    "03-02": [
        "🇲🇲 Myanmar: Peasants' Day (တောင်သူလယ်သမားနေ့)"
    ],
    "03-08": [
        "🌍 Global: International Women's Day (အပြည်ပြည်ဆိုင်ရာ အမျိုးသမီးများနေ့)"
    ],
    "03-27": [
        "🇲🇲 Myanmar: Armed Forces Day (တပ်မတော်နေ့)"
    ],
    "04-07": [
        "🌍 Global: World Health Day (ကမ္ဘာ့ကျန်းမာရေးနေ့)"
    ],
    "04-13": [
        "🇲🇲 Myanmar / 🇹🇭 Thailand / 🇱🇦 Laos / 🇰🇭 Cambodia: Thingyan / Songkran Water Festival (သင်္ကြန်အကြိုနေ့)"
    ],
    "04-14": [
        "🇲🇲 Myanmar / 🇹🇭 Thailand: Thingyan / Songkran Festival (သင်္ကြန်အကျနေ့)"
    ],
    "04-15": [
        "🇲🇲 Myanmar / 🇹🇭 Thailand: Thingyan / Songkran Festival (သင်္ကြန်အကြတ်နေ့)"
    ],
    "04-16": [
        "🇲🇲 Myanmar: Thingyan Atat Day (သင်္ကြန်အတက်နေ့)"
    ],
    "04-17": [
        "🇲🇲 Myanmar: Myanmar New Year Day (မြန်မာနှစ်ဆန်းတစ်ရက်နေ့)"
    ],
    "04-22": [
        "🌍 Global: Earth Day (ကမ္ဘာမြေနေ့)"
    ],
    "05-01": [
        "🌍 Global: International Workers' Day / Labour Day (ကမ္ဘာ့အလုပ်သမားနေ့)"
    ],
    "05-10": [
        "🌍 Global: World Lupus Day & Mother's Day (အမေများနေ့ - မေလ ဒုတိယပတ် တနင်္ဂနွေ)"
    ],
    "05-31": [
        "🌍 Global: World No Tobacco Day (ကမ္ဘာ့ဆေးလိပ်မသောက်ရေးနေ့)"
    ],
    "06-05": [
        "🌍 Global: World Environment Day (ကမ္ဘာ့ပတ်ဝန်းကျင်ထိန်းသိမ်းရေးနေ့)"
    ],
    "06-12": [
        "🇵🇭 Philippines: Independence Day (ဖိလစ်ပိုင် လွတ်လပ်ရေးနေ့)"
    ],
    "06-21": [
        "🌍 Global: International Day of Yoga / World Music Day"
    ],
    "07-04": [
        "🇺🇸 USA: Independence Day (အမေရိကန် လွတ်လပ်ရေးနေ့)"
    ],
    "07-19": [
        "🇲🇲 Myanmar: Martyrs' Day (မြန်မာ့အာဇာနည်နေ့)"
    ],
    "08-08": [
        "🌏 ASEAN: ASEAN Day (အာဆီယံနေ့)"
    ],
    "08-09": [
        "🇸🇬 Singapore: National Day (စင်ကာပူ အမျိုးသားနေ့)"
    ],
    "08-12": [
        "🇹🇭 Thailand: Her Majesty Queen Sirikit's Birthday / Thai Mother's Day (ထိုင်း အမေများနေ့)"
    ],
    "08-15": [
        "🇮🇳 India / 🇰🇷 South Korea: Independence Day (အိန္ဒိယနှင့် တောင်ကိုရီးယား လွတ်လပ်ရေးနေ့)"
    ],
    "08-17": [
        "🇮🇩 Indonesia: Independence Day (အင်ဒိုနီးရှား လွတ်လပ်ရေးနေ့)"
    ],
    "08-31": [
        "🇲🇾 Malaysia: Merdeka Day / Independence Day (မလေးရှား လွတ်လပ်ရေးနေ့)"
    ],
    "09-02": [
        "🇻🇳 Vietnam: National Day (ဗီယက်နမ် အမျိုးသားနေ့)"
    ],
    "09-21": [
        "🌍 Global: International Day of Peace (အပြည်ပြည်ဆိုင်ရာ ငြိမ်းချမ်းရေးနေ့)"
    ],
    "10-01": [
        "🇨🇳 China: National Day (တရုတ် အမျိုးသားနေ့)"
    ],
    "10-05": [
        "🌍 Global: World Teachers' Day (ကမ္ဘာ့ဆရာများနေ့)"
    ],
    "10-31": [
        "🌍 Global: Halloween (ဟယ်လိုဝင်းနေ့)"
    ],
    "11-09": [
        "🇰🇭 Cambodia: Independence Day (ကမ္ဘောဒီးယား လွတ်လပ်ရေးနေ့)"
    ],
    "11-11": [
        "🌍 Global: Singles' Day / Remembrance Day"
    ],
    "11-14": [
        "🌍 Global: World Diabetes Day (ကမ္ဘာ့ဆီးချိုရောဂါနေ့)"
    ],
    "11-15": [
        "🌍 Global: World Day of Remembrance for Road Traffic Victims",
        "🇵🇸 Palestine: Independence Day (ပါလက်စတိုင်း လွတ်လပ်ရေးနေ့)",
        "🇧🇷 Brazil: Republic Proclamation Day (ဘရာဇီး သမ္မတနိုင်ငံနေ့)",
        "🇯🇵 Japan: Shichi-Go-San Festival (ဂျပန် ကလေးများပွဲတော်နေ့)"
    ],
    "11-19": [
        "🌍 Global: International Men's Day (အပြည်ပြည်ဆိုင်ရာ အမျိုးသားများနေ့)"
    ],
    "11-20": [
        "🌍 Global: World Children's Day (ကမ္ဘာ့ကလေးများနေ့)"
    ],
    "12-01": [
        "🌍 Global: World AIDS Day (ကမ္ဘာ့အေအိုင်ဒီအက်စ်နေ့)"
    ],
    "12-02": [
        "🇱🇦 Laos: National Day (လာအို အမျိုးသားနေ့)"
    ],
    "12-05": [
        "🇹🇭 Thailand: King Bhumibol's Birthday / Thai Father's Day (ထိုင်း အဖေများနေ့)"
    ],
    "12-10": [
        "🌍 Global: Human Rights Day (ကမ္ဘာ့လူ့အခွင့်အရေးနေ့)"
    ],
    "12-25": [
        "🌍 Global: Christmas Day (ခရစ္စမတ်နေ့)"
    ],
    "12-31": [
        "🌍 Global: New Year's Eve (နှစ်သစ်ကူးအကြိုည)"
    ]
}

FALLBACK_QUOTES = [
    ("The only way to do great work is to love what you do.", "Steve Jobs"),
    ("In the middle of every difficulty lies opportunity.", "Albert Einstein"),
    ("Success is not final, failure is not fatal: it is the courage to continue that counts.", "Winston Churchill"),
    ("It always seems impossible until it's done.", "Nelson Mandela"),
    ("Do not wait to strike till the iron is hot; but make it hot by striking.", "William Butler Yeats"),
    ("Whether you think you can or you think you can't, you're right.", "Henry Ford"),
    ("The future belongs to those who believe in the beauty of their dreams.", "Eleanor Roosevelt"),
    ("Your time is limited, so don't waste it living someone else's life.", "Steve Jobs"),
    ("Stay hungry, stay foolish.", "Steve Jobs"),
    ("The best way to predict the future is to create it.", "Peter Drucker"),
    ("Believe you can and you're halfway there.", "Theodore Roosevelt"),
    ("Act as if what you do makes a difference. It does.", "William James"),
    ("Quality is not an act, it is a habit.", "Aristotle"),
    ("Happiness depends upon ourselves.", "Aristotle"),
    ("Turn your wounds into wisdom.", "Oprah Winfrey"),
    ("Everything you've ever wanted is on the other side of fear.", "George Addair"),
    ("Dream big and dare to fail.", "Norman Vaughan"),
    ("Don't count the days, make the days count.", "Muhammad Ali"),
    ("Simplicity is the ultimate sophistication.", "Leonardo da Vinci"),
    ("Knowledge is power.", "Francis Bacon"),
    ("Whatever you are, be a good one.", "Abraham Lincoln"),
    ("It does not matter how slowly you go as long as you do not stop.", "Confucius"),
    ("Everything has beauty, but not everyone sees it.", "Confucius"),
    ("A journey of a thousand miles begins with a single step.", "Lao Tzu")
]

# ================= CLOUD + LOCAL DATA STORAGE =================
def load_data() -> dict:
    if JSONBIN_ID and JSONBIN_KEY:
        try:
            url = f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}/latest"
            headers = {"X-Master-Key": JSONBIN_KEY}
            res = requests.get(url, headers=headers, timeout=8)
            if res.status_code == 200:
                record = res.json().get("record", {})
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

# ================= NEW LIVE UTILITY COMMANDS (/time, /gevent, /qtoday) =================
async def cmd_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(update):
        return

    now_utc = datetime.now(pytz.utc)
    mm_now = now_utc.astimezone(pytz.timezone("Asia/Yangon"))
    date_header = mm_now.strftime("%d %b %Y (%A)")

    lines = [
        f"🕒 **Southeast Asia Live Time**",
        f"📅 `{date_header}`\n"
    ]

    for country_label, tz_name in SEA_COUNTRIES:
        tz = pytz.timezone(tz_name)
        local_dt = now_utc.astimezone(tz)
        time_str = local_dt.strftime("%I:%M:%S %p")
        lines.append(f"• {country_label}: `{time_str}`")

    await send_tracked_reply(update, "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

def fetch_live_wiki_holidays(month: int, day: int) -> list:
    results = []
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/holidays/{month:02d}/{day:02d}"
        headers = {"User-Agent": "MobehoTelegramBot/1.0"}
        res = requests.get(url, headers=headers, timeout=6)
        if res.status_code == 200:
            holidays = res.json().get("holidays", [])
            for item in holidays[:6]:
                text = item.get("text", "").strip()
                if text:
                    results.append(f"🌐 {text}")
    except Exception:
        pass
    return results

async def cmd_gevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(update):
        return

    mm_now = datetime.now(pytz.timezone("Asia/Yangon"))
    target_month = mm_now.month
    target_day = mm_now.day

    # /gevent 19-07 လိုမျိုး နေ့စွဲထည့်ရှာပါက ရှာပေးမည်
    if context.args:
        arg = context.args[0].replace("/", "-")
        parts = arg.split("-")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            d, m = int(parts[0]), int(parts[1])
            if 1 <= m <= 12 and 1 <= d <= 31:
                target_day, target_month = d, m

    mm_dd = f"{target_month:02d}-{target_day:02d}"
    display_date = datetime(mm_now.year, target_month, target_day).strftime("%d %B %Y")

    events = list(FIXED_EVENTS.get(mm_dd, []))

    # Mother's Day (2nd Sunday of May) & Father's Day (3rd Sunday of June) dynamic calculation
    if target_month == 5:
        first_day = datetime(mm_now.year, 5, 1)
        second_sunday = 1 + (6 - first_day.weekday()) % 7 + 7
        if target_day == second_sunday:
            events.insert(0, "🌍 Global: International Mother's Day (အပြည်ပြည်ဆိုင်ရာ အမေများနေ့)")
    elif target_month == 6:
        first_day = datetime(mm_now.year, 6, 1)
        third_sunday = 1 + (6 - first_day.weekday()) % 7 + 14
        if target_day == third_sunday:
            events.insert(0, "🌍 Global: International Father's Day (အပြည်ပြည်ဆိုင်ရာ အဖေများနေ့)")

    # Fetch live international observances from Wikipedia API
    wiki_events = await asyncio.to_thread(fetch_live_wiki_holidays, target_month, target_day)
    for w in wiki_events:
        if not any(w.lower()[3:15] in e.lower() for e in events):
            events.append(w)

    if not events:
        events.append("• ဒီနေ့အတွက် သီးသန့်မှတ်တမ်းတင်ထားသော နေ့ထူးနေ့မြတ် မရှိပါ။")

    header = (
        f"📅 **Global & National Events**\n"
        f"🗓️ **Date:** `{display_date}`\n\n"
    )
    body = "\n".join(events)
    footer = "\n\n💡 _အခြားရက်ကြည့်ရန်:_ `/gevent 19-07`"
    await send_tracked_reply(update, header + body + footer, parse_mode=ParseMode.MARKDOWN)

def get_hourly_quote() -> tuple:
    mm_now = datetime.now(pytz.timezone("Asia/Yangon"))
    hour_key = mm_now.strftime("%Y-%m-%d-%H")

    if hourly_quote_cache["hour_key"] == hour_key and hourly_quote_cache["text"]:
        return hourly_quote_cache["text"], mm_now

    # Live API မှ Quote အသစ် ဆွဲယူမည်
    quote_tuple = None
    try:
        res = requests.get("https://zenquotes.io/api/random", timeout=6)
        if res.status_code == 200:
            arr = res.json()
            if isinstance(arr, list) and arr:
                q = arr[0].get("q", "").strip()
                a = arr[0].get("a", "Unknown").strip()
                if q:
                    quote_tuple = (q, a)
    except Exception:
        pass

    if not quote_tuple:
        idx = (mm_now.day * 24 + mm_now.hour) % len(FALLBACK_QUOTES)
        quote_tuple = FALLBACK_QUOTES[idx]

    hourly_quote_cache["hour_key"] = hour_key
    hourly_quote_cache["text"] = quote_tuple
    return quote_tuple, mm_now

async def cmd_qtoday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_access(update):
        return

    (quote_text, author), mm_now = await asyncio.to_thread(get_hourly_quote)
    hour_label = mm_now.strftime("%I:00 %p")
    next_hour = (mm_now + timedelta(hours=1)).strftime("%I:00 %p")

    msg = (
        f"💬 **Quote of the Hour** (`{hour_label}`)\n\n"
        f"“_{quote_text}_”\n"
        f"— **{author}**\n\n"
        f"🔄 _Next live update at `{next_hour}` (MMT)_"
    )
    await send_tracked_reply(update, msg, parse_mode=ParseMode.MARKDOWN)

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

# ================= EXISTING COMMANDS (OWNER + PERMITTED USERS) =================
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


"""
Trade Tracker Bot — persistent Discord listener

Watches two channels:
  #trade-tracker — Robinhood fill screenshot -> Claude vision extract -> Sheets row
  #stock-alerts  — $TICKER message -> Tradier quote/chains -> Claude analysis

NOT a fire-and-exit job. Holds a Discord websocket and blocks until
timeout-minutes expires. Runs never show green — duration is the health
signal, not the status icon. ~5h55m = healthy full session.
"""

import discord
import requests
import json
import gspread
from google.oauth2.service_account import Credentials
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import base64
import aiohttp
import traceback

# ── TIMEZONE ──────────────────────────────────────────────────────────────────
# GitHub Actions runners are UTC. Bare datetime.now() was the source of the
# off-by-one DTE: expiration parses to midnight, now() is mid-morning, and
# .days truncated 13d9h down to 13 on what is really a 14-day trade.
ET = ZoneInfo("America/New_York")

def now_et():
    return datetime.now(ET)

def today_et():
    return now_et().date()

DISCORD_BOT_TOKEN   = os.environ.get("DISCORD_TRADE_BOT_TOKEN", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
TRADIER_TOKEN       = os.environ.get("TRADIER_TOKEN", "")
SPREADSHEET_ID      = "1yEN54IvJ7E8h0Eh1R3HlXdWFUXO6ePQAs40IiznxjKY"
TRADE_TRACKER_CHANNEL = "trade-tracker"
STOCK_ALERTS_CHANNEL  = "stock-alerts"

# Written to the ticker cell when extraction genuinely can't find one, so a
# blank never slips into the sheet looking like a successful log.
UNKNOWN_TICKER = "UNKNOWN"

print(f"TRADIER_TOKEN present: {bool(TRADIER_TOKEN)}")
print(f"ANTHROPIC_API_KEY present: {bool(ANTHROPIC_API_KEY)}")
print(f"DISCORD_BOT_TOKEN present: {bool(DISCORD_BOT_TOKEN)}")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

SERVICE_ACCOUNT_INFO = {
    "type": "service_account",
    "project_id": "trade-tracker-bot",
    "private_key_id": "2864785400f0d5bf9f2ccb51317260e665188c8f",
    "private_key": os.environ.get("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n"),
    "client_email": "trade-tracker@trade-tracker-bot.iam.gserviceaccount.com",
    "client_id": "114934753054312970043",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/trade-tracker%40trade-tracker-bot.iam.gserviceaccount.com",
    "universe_domain": "googleapis.com"
}

# ── DATE NORMALIZATION ────────────────────────────────────────────────────────
# Every date written to the sheet passes through here so the sheet never sees
# mixed formats (e.g. "2026-07-31" alongside "07/31/2026"), which silently
# broke the DTE formulas.
DATE_FORMATS = [
    "%m/%d/%Y",   # 07/31/2026
    "%Y-%m-%d",   # 2026-07-31
    "%m/%d/%y",   # 07/31/26
    "%m-%d-%Y",   # 07-31-2026
    "%Y/%m/%d",   # 2026/07/31
    "%b %d, %Y",  # Jul 31, 2026
    "%B %d, %Y",  # July 31, 2026
    "%b %d %Y",   # Jul 31 2026
    "%d %b %Y",   # 31 Jul 2026
]

def normalize_date(value):
    """Return MM/DD/YYYY for any recognized date string. Passthrough if unparseable."""
    if not value:
        return value
    s = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    print(f"WARN: could not normalize date '{s}' — writing as-is")
    return s

def days_to_expiration(expiration, opened=None):
    """
    Calendar DTE between the opened date and expiration.

    Compares DATES, not datetimes. The old version subtracted a mid-morning
    datetime.now() from a midnight expiration, so .days truncated 13d9h to 13
    on a trade that is genuinely 14 days out. Every DTE the bot wrote was one
    day short.
    """
    try:
        exp = datetime.strptime(normalize_date(expiration), "%m/%d/%Y").date()
    except Exception:
        return ""
    try:
        start = datetime.strptime(normalize_date(opened), "%m/%d/%Y").date() if opened else today_et()
    except Exception:
        start = today_et()
    return (exp - start).days

def clean_ticker(value):
    """
    Normalize an extracted ticker. Returns None when there genuinely isn't one
    — e.g. a redacted Robinhood order header — so the caller can flag it rather
    than writing an empty cell that looks like a successful log.
    """
    if value is None:
        return None
    s = str(value).strip().upper().lstrip("$")
    if not s or s in ("NULL", "NONE", "N/A", "?", "-"):
        return None
    if not re.fullmatch(r"[A-Z]{1,6}", s):
        print(f"WARN: extracted ticker '{s}' is not a plausible symbol")
        return None
    return s

def derive_width(data):
    """Fall back to |short - long| when the model didn't return a width."""
    w = data.get("width")
    if w not in (None, "", 0):
        return w
    try:
        return abs(float(data["short_strike"]) - float(data["long_strike"]))
    except Exception:
        return ""

def get_sheets_client():
    creds = Credentials.from_service_account_info(SERVICE_ACCOUNT_INFO, scopes=SCOPES)
    return gspread.authorize(creds)

def get_next_empty_row(worksheet):
    col_a = worksheet.col_values(1)
    for i, val in enumerate(col_a):
        if i < 3:
            continue
        if not val or val.strip() == "" or val.strip() == "-":
            return i + 1
    return len(col_a) + 1

async def image_to_base64(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.read()
            return base64.b64encode(data).decode("utf-8")

DISCORD_MAX_CHARS = 1900   # real cap is 2000; leave room for the [n/m] prefix

def chunk_for_discord(text, limit=DISCORD_MAX_CHARS):
    """
    Split a message so no piece exceeds Discord's 2000-character limit.

    analyze_stock returns up to 800 tokens, which can run well past 2000 chars.
    Sending it in one reply returned:
        400 Bad Request (error code: 50035): Invalid Form Body
        In content: Must be 2000 or fewer in length.
    send_telegram already chunked; the Discord reply path never did.
    Splits on line boundaries so formatting survives.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks, current = [], ""
    for line in text.split("\n"):
        # A single line longer than the limit has to be hard-split.
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = line if not current else current + "\n" + line
    if current:
        chunks.append(current)
    return chunks

async def reply_chunked(message, text):
    """Reply in Discord-safe pieces, numbering them when there is more than one."""
    parts = chunk_for_discord(text)
    total = len(parts)
    for i, part in enumerate(parts, start=1):
        body = part if total == 1 else f"**[{i}/{total}]**\n{part}"
        await message.reply(body)

def _first_text_block(payload):
    """
    Pull the first text block out of the API response.

    Indexing content[0] blindly breaks if the response ever leads with a
    non-text block.
    """
    for block in payload.get("content", []):
        if block.get("type") == "text":
            return block.get("text", "").strip()
    return ""

async def extract_trade_data(image_base64, image_url, media_type="image/png"):
    year  = now_et().strftime("%Y")
    today = now_et().strftime("%m/%d/%Y")

    prompt = """You are analyzing a trading screenshot from Robinhood.
Extract ALL trade information visible and return ONLY a JSON object with no other text.

Detect the trade type first — pick exactly one of: CSP, BPS, CS, CC

Type definitions (IMPORTANT — read carefully):
- CSP = Cash Secured Put: selling a single PUT option, no spread.
- CC = Covered Call: selling a single CALL option against owned shares.
- BPS = Bull Put Spread: a PUT credit spread (sell higher-strike put, buy lower-strike put). Both legs are PUTS.
- CS = Call Spread / Bear Call Spread: a CALL credit spread (sell lower-strike call, buy higher-strike call). Both legs are CALLS.

If the screenshot shows "Sell ... Call" and "Buy ... Call" as the two legs, the type is CS, NOT BPS.
If the screenshot shows "Sell ... Put" and "Buy ... Put" as the two legs, the type is BPS.

IMPORTANT — ticker:
The ticker is usually in the order title (e.g. "QQQ $685/$690 Put Credit Spread 8/21").
Some screenshots have the title redacted, blurred, or replaced with a generic
label like "Option order" — in that case look EVERYWHERE ELSE for the symbol:
the leg descriptions, the position name, a chart header, a watchlist row, any
breadcrumb or nav text.
If after checking all of that the ticker genuinely does not appear anywhere in
the image, return null for ticker. DO NOT GUESS and DO NOT infer it from the
strike prices — a wrong ticker is far worse than a null one.

IMPORTANT — date_opened (FILLED, not SUBMITTED):
Robinhood order screenshots often show TWO timestamps: "Submitted" and "Filled".
These can be DIFFERENT DAYS (e.g. Submitted 8/1, Filled 8/3).
ALWAYS use the FILLED date for date_opened — that is when the position actually opened.
Only fall back to the Submitted date if no Filled date appears on the screenshot.

IMPORTANT — date format:
Return EVERY date (date_opened and expiration) in MM/DD/YYYY format.
Never return YYYY-MM-DD, never return a 2-digit year, never return "Jul 31" style text.
Example: 07/31/2026 — correct. 2026-07-31 — WRONG.

IMPORTANT — expiration year:
Always use the FULL year shown on the screenshot. Today is """ + year + """. If the screenshot shows a month/day without a year, assume """ + year + """. Never use a past year unless explicitly shown.

IMPORTANT — premium field:
Robinhood spread screenshots show a large dollar total at the top (e.g. "$70.00") which is the TOTAL credit/debit for ALL contracts combined. Do NOT use that number.
Instead use the PER-SHARE "Limit price" value (e.g. "$0.70") shown in the order details.

Return JSON with type, ticker, date_opened, expiration, strike_price, short_strike, long_strike, width, contracts, premium, shares, call_strike, notes.
Use null for fields that don't apply.
Today's date: """ + today + """
Return ONLY the JSON."""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
        json={"model": "claude-sonnet-4-6", "max_tokens": 500, "messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_base64}}, {"type": "text", "text": prompt}]}]},
        timeout=30
    )
    if response.status_code != 200:
        print(f"Claude API error {response.status_code}: {response.text[:500]}")
    response.raise_for_status()

    raw = _first_text_block(response.json())
    raw = re.sub(r"```json|```", "", raw).strip()
    data = json.loads(raw)

    # Force every date into MM/DD/YYYY before anything touches the sheet.
    for field in ("date_opened", "expiration"):
        if data.get(field):
            original = data[field]
            data[field] = normalize_date(original)
            if str(original) != str(data[field]):
                print(f"Normalized {field}: {original} -> {data[field]}")

    # Ticker validation — a missing ticker is flagged, never written blank.
    ticker = clean_ticker(data.get("ticker"))
    data["ticker_missing"] = ticker is None
    data["ticker"] = ticker or UNKNOWN_TICKER
    if data["ticker_missing"]:
        print("WARN: no ticker found in screenshot — writing UNKNOWN")

    return data

def append_to_csp(ws, data):
    row = get_next_empty_row(ws)
    opened = data.get("date_opened") or now_et().strftime("%m/%d/%Y")
    ws.update(range_name=f"A{row}:F{row}", values=[[data.get("ticker",""), data.get("strike_price",""), data.get("contracts",1), opened, data.get("expiration",""), data.get("premium","")]])
    return row

def append_to_bps(ws, data):
    row = get_next_empty_row(ws)
    opened = data.get("date_opened") or now_et().strftime("%m/%d/%Y")
    dte = days_to_expiration(data.get("expiration",""), opened)
    ws.update(range_name=f"A{row}:I{row}", values=[[opened, data.get("ticker",""), data.get("short_strike",""), data.get("long_strike",""), derive_width(data), data.get("expiration",""), dte, data.get("premium",""), data.get("contracts",1)]])
    return row

def append_to_cs(ws, data):
    row = get_next_empty_row(ws)
    opened = data.get("date_opened") or now_et().strftime("%m/%d/%Y")
    dte = days_to_expiration(data.get("expiration",""), opened)
    ws.update(range_name=f"A{row}:I{row}", values=[[opened, data.get("ticker",""), data.get("short_strike",""), data.get("long_strike",""), derive_width(data), data.get("expiration",""), dte, data.get("premium",""), data.get("contracts",1)]])
    return row

def append_to_cc(ws, data):
    row = get_next_empty_row(ws)
    opened = data.get("date_opened") or now_et().strftime("%m/%d/%Y")
    contracts = data.get("contracts") or data.get("shares") or 1
    ws.update(range_name=f"A{row}:E{row}", values=[[data.get("ticker",""), contracts, opened, data.get("expiration",""), data.get("call_strike","") or data.get("strike_price","")]])
    ws.update(range_name=f"I{row}", values=[[data.get("premium","")]])
    return row

def write_to_sheet(trade_data):
    client = get_sheets_client()
    sheet = client.open_by_key(SPREADSHEET_ID)
    trade_type = trade_data.get("type","").upper()
    if trade_type == "CSP":
        return "CSP", append_to_csp(sheet.worksheet("CSP"), trade_data)
    elif trade_type == "BPS":
        return "BPS", append_to_bps(sheet.worksheet("BPS"), trade_data)
    elif trade_type == "CS":
        return "CS", append_to_cs(sheet.worksheet("CS"), trade_data)
    elif trade_type == "CC":
        return "CC", append_to_cc(sheet.worksheet("CC"), trade_data)
    else:
        raise ValueError(f"Unknown trade type: {trade_type}")

def get_stock_data(ticker):
    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/quotes",
            headers={"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"},
            params={"symbols": ticker}, timeout=10
        )
        r.raise_for_status()
        quote = r.json()["quotes"]["quote"]
        return {"price": quote.get("last") or quote.get("close"), "week52_high": quote.get("week_52_high"), "week52_low": quote.get("week_52_low"), "change_pct": quote.get("change_percentage")}
    except Exception as e:
        print(f"Tradier quote error: {e}")
        return None

def get_options_expirations(ticker):
    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/options/expirations",
            headers={"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"},
            params={"symbol": ticker, "includeAllRoots": "true"}, timeout=10
        )
        r.raise_for_status()
        expirations = r.json()["expirations"]["date"]
        if isinstance(expirations, str):
            expirations = [expirations]
        today = today_et()
        exp_30 = next((e for e in expirations if datetime.strptime(e, "%Y-%m-%d").date() >= today + timedelta(days=25)), None)
        exp_leaps = next((e for e in expirations if datetime.strptime(e, "%Y-%m-%d").date() >= today + timedelta(days=300)), None)
        return {"exp_30": exp_30, "exp_leaps": exp_leaps}
    except Exception as e:
        print(f"Tradier expirations error: {e}")
        return None

def get_options_chain(ticker, expiration, option_type="put"):
    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/options/chains",
            headers={"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"},
            params={"symbol": ticker, "expiration": expiration, "greeks": "false"}, timeout=10
        )
        r.raise_for_status()
        options = r.json()["options"]["option"]
        options = [options] if isinstance(options, dict) else options
        return [o for o in options if o.get("option_type") == option_type]
    except Exception as e:
        print(f"Tradier chain error: {e}")
        return []

def find_strike_near(options, target_price):
    best = min(options, key=lambda o: abs(o.get("strike", 0) - target_price), default=None)
    return best

async def analyze_stock(ticker):
    ticker = ticker.upper()
    print(f"Analyzing {ticker}...")

    stock = get_stock_data(ticker)
    if not stock or not stock.get("price"):
        return f"❌ No data for **{ticker}**."
    price = float(stock["price"])
    print(f"{ticker} price: {price}")

    exps = get_options_expirations(ticker)
    if not exps:
        return f"❌ No options data for **{ticker}**."

    exp_30 = exps.get("exp_30")
    exp_leaps = exps.get("exp_leaps")
    print(f"Expirations — 30DTE: {exp_30}, LEAPS: {exp_leaps}")

    puts = get_options_chain(ticker, exp_30, "put") if exp_30 else []
    calls = get_options_chain(ticker, exp_30, "call") if exp_30 else []
    leaps = get_options_chain(ticker, exp_leaps, "call") if exp_leaps else []

    csp = find_strike_near(puts, price * 0.95)
    spread_short = find_strike_near(puts, price * 0.95)
    spread_long = find_strike_near(puts, price * 0.90)
    call_spread_short = find_strike_near(calls, price * 1.05)
    call_spread_long = find_strike_near(calls, price * 1.10)
    leaps_call = find_strike_near(leaps, price * 1.10) if leaps else None

    context = f"""
Ticker: {ticker} | Price: ${price}
52W: ${stock.get('week52_high')} high / ${stock.get('week52_low')} low
Change: {stock.get('change_pct')}%
30DTE exp: {exp_30} | LEAPS exp: {exp_leaps}

CSP candidate: {json.dumps(csp) if csp else 'none'}
Bull Put Spread short: {json.dumps(spread_short) if spread_short else 'none'}
Bull Put Spread long: {json.dumps(spread_long) if spread_long else 'none'}
Bear Call Spread short: {json.dumps(call_spread_short) if call_spread_short else 'none'}
Bear Call Spread long: {json.dumps(call_spread_long) if call_spread_long else 'none'}
LEAPS call: {json.dumps(leaps_call) if leaps_call else 'none'}
"""
    print(f"Calling Claude for analysis...")
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
        json={"model": "claude-sonnet-4-6", "max_tokens": 800, "messages": [{"role": "user", "content": f"""Analyze this stock for options trader Z. His strategies: CSP (sell OTM put ~30DTE), Bull Put Spread, Bear Call Spread, Covered Call, LEAPS, PMCC.

His hard rules: $5 spread widths only, short-leg delta 0.18-0.22, OI floor 50,
GTC close at 47% of credit, minimum 10% ROI computed on MAX LOSS not width.

{context}

Reply in this exact Discord format:
📊 **{ticker}** — ${price}
━━━━━━━━━━━━━━━━━━━━━━
✅/❌ **CSP**: details
✅/❌ **Bull Put Spread**: details  
✅/❌ **Bear Call Spread**: details
✅/❌ **LEAPS**: details
✅/❌ **PMCC**: details
━━━━━━━━━━━━━━━━━━━━━━
🎯 **Best Play**: one strategy + 2 sentence reason
⚠️ **Risk**: one key risk"""}]},
        timeout=30
    )
    response.raise_for_status()
    result = _first_text_block(response.json())
    print(f"Analysis complete for {ticker}")
    return result

# ── DISCORD BOT ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

SUPPORTED_IMAGE_TYPES = {
    ".png" : "image/png",
    ".jpg" : "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif" : "image/gif",
}

def media_type_for(filename):
    lower = filename.lower()
    for ext, mtype in SUPPORTED_IMAGE_TYPES.items():
        if lower.endswith(ext):
            return mtype
    return None

@bot.event
async def on_ready():
    print(f"=== BOT READY: {bot.user} === [{now_et().strftime('%Y-%m-%d %H:%M:%S %Z')}]")
    for guild in bot.guilds:
        print(f"Server: {guild.name}")
        for ch in guild.channels:
            print(f"  #{ch.name} (type={ch.type})")

async def process_attachment(message, attachment):
    """Handle one screenshot. Returns a reply string."""
    media_type = media_type_for(attachment.filename)
    if not media_type:
        return None

    image_b64  = await image_to_base64(attachment.url)
    trade_data = await extract_trade_data(image_b64, attachment.url, media_type)
    print(f"Trade data: {trade_data}")
    tab, row = write_to_sheet(trade_data)

    t          = trade_data
    trade_type = t.get("type", "")
    ticker     = t.get("ticker", UNKNOWN_TICKER)
    missing    = t.get("ticker_missing", False)

    if trade_type in ["BPS", "CS"]:
        details = f"${t.get('short_strike')} / ${t.get('long_strike')} | Premium: ${t.get('premium')} | Exp: {t.get('expiration')}"
    elif trade_type == "CSP":
        details = f"Strike: ${t.get('strike_price')} | Premium: ${t.get('premium')} | Exp: {t.get('expiration')}"
    elif trade_type == "CC":
        details = f"Call Strike: ${t.get('call_strike') or t.get('strike_price')} | Premium: ${t.get('premium')} | Exp: {t.get('expiration')}"
    else:
        details = str(t)

    header = "⚠️ **Trade logged — TICKER MISSING**" if missing else "✅ **Trade logged!**"
    msg = (
        f"{header}\n"
        f"📋 Tab: **{tab}** | Row: {row}\n"
        f"📊 **{ticker} {trade_type}** — {details}\n"
        f"📅 Opened: {t.get('date_opened','?')}\n"
    )
    if missing:
        msg += (
            f"🚨 No ticker was visible in the screenshot — the header may be "
            f"redacted or cropped. Row {row} on tab **{tab}** was written with "
            f"`{UNKNOWN_TICKER}` in column B. Fix it by hand.\n"
        )
    msg += f"🔗 [Open Tracker](https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID})"
    return msg

@bot.event
async def on_message(message):
    try:
        print(f"MSG: #{message.channel.name} | {message.author}: {message.content[:80]}")

        if message.author.bot:
            return

        # ── STOCK ALERTS ──────────────────────────────────────────────────────
        if message.channel.name == STOCK_ALERTS_CHANNEL:
            tickers = re.findall(r'\$([A-Za-z]{1,5})', message.content)
            if tickers:
                ticker = tickers[0].upper()
                await message.add_reaction("⏳")
                try:
                    analysis = await analyze_stock(ticker)
                    await message.remove_reaction("⏳", bot.user)
                    await reply_chunked(message, analysis)
                except Exception as e:
                    print(f"Analysis error: {traceback.format_exc()}")
                    await message.remove_reaction("⏳", bot.user)
                    await reply_chunked(message, f"❌ Analysis failed for **{ticker}**: {str(e)}")
            return

        # ── TRADE TRACKER ─────────────────────────────────────────────────────
        if message.channel.name != TRADE_TRACKER_CHANNEL:
            return
        if not message.attachments:
            return

        images = [a for a in message.attachments if media_type_for(a.filename)]
        if not images:
            return

        # Both legs of a condor often arrive as two screenshots in ONE message.
        # The old code read attachments[0] and silently dropped the rest.
        await message.add_reaction("⏳")
        any_missing = False
        replies     = []
        failures    = []

        for idx, attachment in enumerate(images, start=1):
            try:
                reply = await process_attachment(message, attachment)
                if reply:
                    prefix = f"**[{idx}/{len(images)}]**\n" if len(images) > 1 else ""
                    replies.append(prefix + reply)
                    if "TICKER MISSING" in reply:
                        any_missing = True
            except Exception as e:
                print(f"Trade error on attachment {idx}: {traceback.format_exc()}")
                failures.append(f"❌ Image {idx}: {str(e)}")

        await message.remove_reaction("⏳", bot.user)

        if replies and not failures:
            await message.add_reaction("⚠️" if any_missing else "✅")
        elif replies and failures:
            await message.add_reaction("⚠️")
        else:
            await message.add_reaction("❌")

        for chunk in replies + failures:
            await reply_chunked(message, chunk)

    except Exception as e:
        print(f"on_message crash: {traceback.format_exc()}")

print(f"Starting bot... [{now_et().strftime('%Y-%m-%d %H:%M:%S %Z')}]")
if not DISCORD_BOT_TOKEN:
    raise ValueError("DISCORD_TRADE_BOT_TOKEN missing")
bot.run(DISCORD_BOT_TOKEN)

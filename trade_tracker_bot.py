"""
Discord Trade Tracker Bot
Watches #trade-tracker channel for trade screenshots
Uses Claude Vision to extract trade data
Auto-fills the correct tab in Google Sheet based on trade type

Also watches #stock-alerts for $TICKER messages
Analyzes stock against Z's options strategies using Tradier + Claude
"""

import discord
import requests
import json
import gspread
from google.oauth2.service_account import Credentials
import os
import re
from datetime import datetime
import base64
import aiohttp

# ── CONFIG ────────────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN   = os.environ.get("DISCORD_TRADE_BOT_TOKEN", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
TRADIER_TOKEN       = os.environ.get("TRADIER_TOKEN", "")
SPREADSHEET_ID      = "1yEN54IvJ7E8h0Eh1R3HlXdWFUXO6ePQAs40IiznxjKY"
TRADE_TRACKER_CHANNEL = "trade-tracker"
STOCK_ALERTS_CHANNEL  = "stock-alerts"

# Google Sheets setup
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

async def extract_trade_data(image_base64, image_url):
    prompt = """You are analyzing a trading screenshot from Robinhood.

Extract ALL trade information visible and return ONLY a JSON object with no other text.

Detect the trade type first:
- If it shows "Cash Secured Put" or "CSP" or a single put option sold → type: "CSP"
- If it shows "Call Debit Spread" or "Bull Call Spread" → type: "CS" (Call Spread)
- If it shows "Put Credit Spread" or "Bull Put Spread" or "Credit Spread" → type: "BPS"
- If it shows "Bear Call Spread" or "Call Credit Spread" → type: "CS"
- If it shows "Covered Call" or a single call option sold against stock → type: "CC"

Return this exact JSON structure:
{
  "type": "CSP|BPS|CS|CC",
  "ticker": "symbol",
  "date_opened": "MM/DD/YYYY",
  "expiration": "MM/DD/YYYY",
  "strike_price": number (for CSP/CC — the single strike),
  "short_strike": number (for spreads — the strike you sold),
  "long_strike": number (for spreads — the strike you bought),
  "width": number (difference between strikes, for spreads only),
  "contracts": number,
  "premium": number (credit collected per share),
  "shares": number (for CC only),
  "call_strike": number (for CC only),
  "notes": "any relevant notes visible"
}

Use null for fields that don't apply to the trade type.
Today's date if not visible: """ + datetime.now().strftime("%m/%d/%Y") + """
Return ONLY the JSON, no explanation."""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 500,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_base64
                        }
                    },
                    {"type": "text", "text": prompt}
                ]
            }]
        },
        timeout=30
    )
    response.raise_for_status()
    raw = response.json()["content"][0]["text"].strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

def append_to_csp(ws, data):
    row = get_next_empty_row(ws)
    today = data.get("date_opened") or datetime.now().strftime("%-m/%-d/%Y")
    ws.update(range_name=f"A{row}:F{row}", values=[[
        data.get("ticker", ""),
        data.get("strike_price", ""),
        data.get("contracts", 1),
        today,
        data.get("expiration", ""),
        data.get("premium", ""),
    ]])
    return row

def append_to_bps(ws, data):
    row = get_next_empty_row(ws)
    today = data.get("date_opened") or datetime.now().strftime("%-m/%-d/%Y")
    try:
        exp = datetime.strptime(data.get("expiration", ""), "%m/%d/%Y")
        dte = (exp - datetime.now()).days
    except:
        dte = ""
    ws.update(range_name=f"A{row}:I{row}", values=[[
        today,
        data.get("ticker", ""),
        data.get("short_strike", ""),
        data.get("long_strike", ""),
        data.get("width", ""),
        data.get("expiration", ""),
        dte,
        data.get("premium", ""),
        data.get("contracts", 1),
    ]])
    return row

def append_to_cs(ws, data):
    row = get_next_empty_row(ws)
    today = data.get("date_opened") or datetime.now().strftime("%-m/%-d/%Y")
    try:
        exp = datetime.strptime(data.get("expiration", ""), "%m/%d/%Y")
        dte = (exp - datetime.now()).days
    except:
        dte = ""
    ws.update(range_name=f"A{row}:I{row}", values=[[
        today,
        data.get("ticker", ""),
        data.get("short_strike", ""),
        data.get("long_strike", ""),
        data.get("width", ""),
        data.get("expiration", ""),
        dte,
        data.get("premium", ""),
        data.get("contracts", 1),
    ]])
    return row

def append_to_cc(ws, data):
    row = get_next_empty_row(ws)
    today = data.get("date_opened") or datetime.now().strftime("%-m/%-d/%Y")
    contracts = data.get("contracts") or data.get("shares") or 1
    ws.update(range_name=f"A{row}:E{row}", values=[[
        data.get("ticker", ""),
        contracts,
        today,
        data.get("expiration", ""),
        data.get("call_strike", "") or data.get("strike_price", ""),
    ]])
    ws.update(range_name=f"G{row}", values=[[data.get("premium", "")]])
    return row

def write_to_sheet(trade_data):
    client = get_sheets_client()
    sheet = client.open_by_key(SPREADSHEET_ID)
    trade_type = trade_data.get("type", "").upper()
    if trade_type == "CSP":
        ws = sheet.worksheet("CSP")
        row = append_to_csp(ws, trade_data)
        return "CSP", row
    elif trade_type == "BPS":
        ws = sheet.worksheet("BPS")
        row = append_to_bps(ws, trade_data)
        return "BPS", row
    elif trade_type == "CS":
        ws = sheet.worksheet("CS")
        row = append_to_cs(ws, trade_data)
        return "CS", row
    elif trade_type == "CC":
        ws = sheet.worksheet("CC")
        row = append_to_cc(ws, trade_data)
        return "CC", row
    else:
        raise ValueError(f"Unknown trade type: {trade_type}")

# ── STOCK ALERTS ANALYZER ─────────────────────────────────────────────────────

def get_stock_data(ticker):
    """Get current price and basic info from Tradier."""
    try:
        r = requests.get(
            f"https://api.tradier.com/v1/markets/quotes",
            headers={"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"},
            params={"symbols": ticker},
            timeout=10
        )
        r.raise_for_status()
        quote = r.json()["quotes"]["quote"]
        return {
            "price": quote.get("last") or quote.get("close"),
            "volume": quote.get("volume"),
            "week52_high": quote.get("week_52_high"),
            "week52_low": quote.get("week_52_low"),
            "change_pct": quote.get("change_percentage"),
        }
    except Exception as e:
        print(f"Tradier quote error: {e}")
        return None

def get_options_data(ticker):
    """Get nearest expirations and sample strikes from Tradier."""
    try:
        # Get expirations
        r = requests.get(
            f"https://api.tradier.com/v1/markets/options/expirations",
            headers={"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"},
            params={"symbol": ticker, "includeAllRoots": "true"},
            timeout=10
        )
        r.raise_for_status()
        expirations = r.json()["expirations"]["date"]
        if isinstance(expirations, str):
            expirations = [expirations]

        # Get ~30 DTE expiration for CSP/CC
        from datetime import timedelta
        today = datetime.now()
        target_30 = today + timedelta(days=30)
        target_90 = today + timedelta(days=90)

        exp_30 = None
        exp_90 = None
        exp_leaps = None

        for e in expirations:
            d = datetime.strptime(e, "%Y-%m-%d")
            if exp_30 is None and d >= target_30:
                exp_30 = e
            if exp_90 is None and d >= target_90:
                exp_90 = e
            if exp_leaps is None and d >= today + timedelta(days=300):
                exp_leaps = e

        return {
            "expirations": expirations[:6],
            "exp_30": exp_30,
            "exp_90": exp_90,
            "exp_leaps": exp_leaps,
        }
    except Exception as e:
        print(f"Tradier expirations error: {e}")
        return None

def get_options_chain(ticker, expiration, option_type="put"):
    """Get options chain for a specific expiration."""
    try:
        r = requests.get(
            f"https://api.tradier.com/v1/markets/options/chains",
            headers={"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"},
            params={"symbol": ticker, "expiration": expiration, "optionType": option_type},
            timeout=10
        )
        r.raise_for_status()
        options = r.json()["options"]["option"]
        if isinstance(options, dict):
            options = [options]
        return options
    except Exception as e:
        print(f"Tradier chain error: {e}")
        return []

def find_csp_strike(options, price):
    """Find best CSP strike ~5% OTM with decent premium."""
    target = price * 0.95
    best = None
    best_diff = float("inf")
    for o in options:
        strike = o.get("strike", 0)
        if strike <= 0:
            continue
        diff = abs(strike - target)
        if diff < best_diff:
            best_diff = diff
            best = o
    return best

def find_spread_strikes(options, price, width=5):
    """Find best credit spread: sell ~5% OTM, buy width below."""
    target_short = price * 0.95
    short = None
    best_diff = float("inf")
    for o in options:
        strike = o.get("strike", 0)
        diff = abs(strike - target_short)
        if diff < best_diff:
            best_diff = diff
            short = o
    if not short:
        return None, None
    long_target = short["strike"] - width
    long_opt = None
    best_diff = float("inf")
    for o in options:
        diff = abs(o.get("strike", 0) - long_target)
        if diff < best_diff:
            best_diff = diff
            long_opt = o
    return short, long_opt

def find_leaps_call(options, price):
    """Find LEAPS call ~10-15% OTM."""
    target = price * 1.10
    best = None
    best_diff = float("inf")
    for o in options:
        strike = o.get("strike", 0)
        diff = abs(strike - target)
        if diff < best_diff:
            best_diff = diff
            best = o
    return best

async def analyze_stock_for_strategies(ticker):
    """Full analysis of ticker against all Z's strategies."""
    ticker = ticker.upper()

    # Get stock data
    stock = get_stock_data(ticker)
    if not stock or not stock.get("price"):
        return f"❌ Could not get data for **{ticker}**. Check the ticker."

    price = stock["price"]

    # Get options expirations
    opts_info = get_options_data(ticker)
    if not opts_info:
        return f"❌ No options data for **{ticker}**."

    exp_30 = opts_info.get("exp_30")
    exp_leaps = opts_info.get("exp_leaps")

    # Get puts chain for CSP/BPS
    puts_30 = get_options_chain(ticker, exp_30, "put") if exp_30 else []
    # Get calls chain for CC/Bear Call
    calls_30 = get_options_chain(ticker, exp_30, "call") if exp_30 else []
    # Get LEAPS calls
    leaps_calls = get_options_chain(ticker, exp_leaps, "call") if exp_leaps else []

    # Find strikes
    csp_strike = find_csp_strike(puts_30, price)
    spread_short, spread_long = find_spread_strikes(puts_30, price, width=5)
    leaps_call = find_leaps_call(leaps_calls, price) if leaps_calls else None

    # Build context for Claude
    context = f"""
Ticker: {ticker}
Current Price: ${price}
52W High: ${stock.get('week52_high')}
52W Low: ${stock.get('week52_low')}
Change Today: {stock.get('change_pct')}%

~30 DTE Expiration: {exp_30}
LEAPS Expiration: {exp_leaps}

CSP Candidate (~5% OTM put):
{json.dumps(csp_strike, indent=2) if csp_strike else 'No data'}

Bull Put Spread Candidates (~5% OTM short / 5-wide):
Short: {json.dumps(spread_short, indent=2) if spread_short else 'No data'}
Long: {json.dumps(spread_long, indent=2) if spread_long else 'No data'}

LEAPS Call (~10% OTM):
{json.dumps(leaps_call, indent=2) if leaps_call else 'No data'}
"""

    # Ask Claude to analyze
    prompt = f"""You are analyzing a stock for an options trader named Z.

Z's strategies:
1. CSP (Cash Secured Put) — sell OTM put ~30 DTE, collect premium, 5% OTM rule, RSI <60, VIX <30
2. Bull Put Spread (BPS) — sell OTM put, buy further OTM put, same expiry, limits risk
3. Bear Call Spread — sell OTM call, buy further OTM call (bearish/neutral)
4. Covered Call (CC) — if Z owns shares, sell OTM call for income
5. LEAPS — buy deep ITM call 9-12 months out as stock replacement
6. PMCC (Poor Man's Covered Call) — buy LEAPS call, sell short-term OTM call against it

Here is the market data:
{context}

Analyze each strategy and respond in this EXACT Discord format:

📊 **{ticker}** — ${{price}}
━━━━━━━━━━━━━━━━━━━━━━
✅/❌ **CSP**: [Strike] put exp [date] — $[premium] premium | [reasoning in 1 line]
✅/❌ **Bull Put Spread**: Sell $[short] / Buy $[long] exp [date] — $[credit] credit | [reasoning]
✅/❌ **Bear Call Spread**: [reasoning why or why not]
✅/❌ **LEAPS**: $[strike] call exp [date] — $[mid] cost | [reasoning]
✅/❌ **PMCC**: [reasoning based on LEAPS availability]
━━━━━━━━━━━━━━━━━━━━━━
🎯 **Best Play**: [pick ONE strategy and explain why in 2 sentences]
⚠️ **Risk**: [one key risk to watch]

Use ✅ if the strategy looks good, ❌ if it doesn't fit right now.
Be specific with strikes, premiums, and dates. Keep it concise."""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 800,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=30
    )
    response.raise_for_status()
    return response.json()["content"][0]["text"].strip()

# ── DISCORD BOT ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"Trade Tracker Bot online as {client.user}")

@client.event
async def on_message(message):
    if message.author.bot:
        return

    # ── STOCK ALERTS: analyze $TICKER ─────────────────────────────────────────
    if message.channel.name == STOCK_ALERTS_CHANNEL:
        # Look for $TICKER pattern
        tickers = re.findall(r'\$([A-Za-z]{1,5})', message.content)
        if tickers:
            ticker = tickers[0].upper()
            await message.add_reaction("⏳")
            try:
                analysis = await analyze_stock_for_strategies(ticker)
                await message.remove_reaction("⏳", client.user)
                await message.reply(analysis)
            except Exception as e:
                print(f"Stock analysis error: {e}")
                await message.remove_reaction("⏳", client.user)
                await message.reply(f"❌ Analysis failed for **{ticker}**: {str(e)}")
        return

    # ── TRADE TRACKER: log screenshots ────────────────────────────────────────
    if message.channel.name != TRADE_TRACKER_CHANNEL:
        return
    if not message.attachments:
        return

    attachment = message.attachments[0]
    if not any(attachment.filename.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
        return

    await message.add_reaction("⏳")

    try:
        image_b64 = await image_to_base64(attachment.url)
        trade_data = await extract_trade_data(image_b64, attachment.url)
        print(f"Extracted trade data: {trade_data}")

        tab, row = write_to_sheet(trade_data)

        t = trade_data
        trade_type = t.get("type", "")
        ticker = t.get("ticker", "?")

        if trade_type in ["BPS", "CS"]:
            details = f"${t.get('short_strike')} / ${t.get('long_strike')} | Premium: ${t.get('premium')} | Exp: {t.get('expiration')}"
        elif trade_type == "CSP":
            details = f"Strike: ${t.get('strike_price')} | Premium: ${t.get('premium')} | Exp: {t.get('expiration')}"
        elif trade_type == "CC":
            details = f"Call Strike: ${t.get('call_strike') or t.get('strike_price')} | Premium: ${t.get('premium')} | Exp: {t.get('expiration')}"
        else:
            details = str(t)

        msg = (
            f"✅ **Trade logged to Google Sheet!**\n"
            f"📋 Tab: **{tab}** | Row: {row}\n"
            f"📊 **{ticker} {trade_type}** — {details}\n"
            f"🔗 [Open Tracker](https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID})"
        )

        await message.remove_reaction("⏳", client.user)
        await message.add_reaction("✅")
        await message.reply(msg)

    except Exception as e:
        print(f"Error processing trade: {e}")
        await message.remove_reaction("⏳", client.user)
        await message.add_reaction("❌")
        await message.reply(f"❌ Could not read trade. Error: {str(e)}\nTry a clearer screenshot of the filled order confirmation.")

if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise ValueError("DISCORD_TRADE_BOT_TOKEN missing")
    client.run(DISCORD_BOT_TOKEN)

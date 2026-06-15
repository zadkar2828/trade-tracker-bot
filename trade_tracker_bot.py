import discord
import requests
import json
import gspread
from google.oauth2.service_account import Credentials
import os
import re
from datetime import datetime, timedelta
import base64
import aiohttp
import traceback

DISCORD_BOT_TOKEN   = os.environ.get("DISCORD_TRADE_BOT_TOKEN", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
TRADIER_TOKEN       = os.environ.get("TRADIER_TOKEN", "")
SPREADSHEET_ID      = "1yEN54IvJ7E8h0Eh1R3HlXdWFUXO6ePQAs40IiznxjKY"
TRADE_TRACKER_CHANNEL = "trade-tracker"
STOCK_ALERTS_CHANNEL  = "stock-alerts"

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

async def extract_trade_data(image_base64, image_url, media_type="image/png"):
    prompt = """You are analyzing a trading screenshot from Robinhood.
Extract ALL trade information visible and return ONLY a JSON object with no other text.

Detect the trade type first — pick exactly one of: CSP, BPS, CS, CC

Type definitions (IMPORTANT — read carefully):
- CSP = Cash Secured Put: selling a single PUT option, no spread.
- CC = Covered Call: selling a single CALL option against owned shares.
- BPS = Bull Put Spread: a PUT credit spread (sell higher-strike put, buy lower-strike put). Both legs are PUTS.
- CS = Call Spread / Bear Call Spread: a CALL credit spread (sell lower-strike call, buy higher-strike call). Both legs are CALLS.

If the screenshot shows "Sell ... Call" and "Buy ... Call" as the two legs, the type is CS, NOT BPS — even though both are credit spreads.
If the screenshot shows "Sell ... Put" and "Buy ... Put" as the two legs, the type is BPS.

IMPORTANT — premium field:
Robinhood spread screenshots show a large dollar total at the top (e.g. "$70.00")
which is the TOTAL credit/debit for ALL contracts combined. Do NOT use that number.
Instead use the PER-SHARE "Limit price" value (e.g. "$0.70") shown in the
order details — this is the premium per contract, which is what "premium"
should contain. For CSP/CC trades, use the per-contract premium/limit price
the same way, not any multiplied total.

Return JSON with type, ticker, date_opened, expiration, strike_price, short_strike, long_strike, width, contracts, premium, shares, call_strike, notes.
Use null for fields that don't apply.
Today's date: """ + datetime.now().strftime("%m/%d/%Y") + """
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
    raw = response.json()["content"][0]["text"].strip()
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

def append_to_csp(ws, data):
    row = get_next_empty_row(ws)
    today = data.get("date_opened") or datetime.now().strftime("%-m/%-d/%Y")
    ws.update(range_name=f"A{row}:F{row}", values=[[data.get("ticker",""), data.get("strike_price",""), data.get("contracts",1), today, data.get("expiration",""), data.get("premium","")]])
    return row

def append_to_bps(ws, data):
    row = get_next_empty_row(ws)
    today = data.get("date_opened") or datetime.now().strftime("%-m/%-d/%Y")
    try:
        exp = datetime.strptime(data.get("expiration",""), "%m/%d/%Y")
        dte = (exp - datetime.now()).days
    except:
        dte = ""
    ws.update(range_name=f"A{row}:I{row}", values=[[today, data.get("ticker",""), data.get("short_strike",""), data.get("long_strike",""), data.get("width",""), data.get("expiration",""), dte, data.get("premium",""), data.get("contracts",1)]])
    return row

def append_to_cs(ws, data):
    row = get_next_empty_row(ws)
    today = data.get("date_opened") or datetime.now().strftime("%-m/%-d/%Y")
    try:
        exp = datetime.strptime(data.get("expiration",""), "%m/%d/%Y")
        dte = (exp - datetime.now()).days
    except:
        dte = ""
    ws.update(range_name=f"A{row}:I{row}", values=[[today, data.get("ticker",""), data.get("short_strike",""), data.get("long_strike",""), data.get("width",""), data.get("expiration",""), dte, data.get("premium",""), data.get("contracts",1)]])
    return row

def append_to_cc(ws, data):
    row = get_next_empty_row(ws)
    today = data.get("date_opened") or datetime.now().strftime("%-m/%-d/%Y")
    contracts = data.get("contracts") or data.get("shares") or 1
    ws.update(range_name=f"A{row}:E{row}", values=[[data.get("ticker",""), contracts, today, data.get("expiration",""), data.get("call_strike","") or data.get("strike_price","")]])
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
        today = datetime.now()
        exp_30 = next((e for e in expirations if datetime.strptime(e, "%Y-%m-%d") >= today + timedelta(days=25)), None)
        exp_leaps = next((e for e in expirations if datetime.strptime(e, "%Y-%m-%d") >= today + timedelta(days=300)), None)
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
    result = response.json()["content"][0]["text"].strip()
    print(f"Analysis complete for {ticker}")
    return result

# ── DISCORD BOT ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

@bot.event
async def on_ready():
    print(f"=== BOT READY: {bot.user} ===")
    for guild in bot.guilds:
        print(f"Server: {guild.name}")
        for ch in guild.channels:
            print(f"  #{ch.name} (type={ch.type})")

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
                    await message.reply(analysis)
                except Exception as e:
                    print(f"Analysis error: {traceback.format_exc()}")
                    await message.remove_reaction("⏳", bot.user)
                    await message.reply(f"❌ Analysis failed for **{ticker}**: {str(e)}")
            return

        # ── TRADE TRACKER ─────────────────────────────────────────────────────
        if message.channel.name != TRADE_TRACKER_CHANNEL:
            return
        if not message.attachments:
            return

        attachment = message.attachments[0]
        filename_lower = attachment.filename.lower()
        if filename_lower.endswith(".png"):
            media_type = "image/png"
        elif filename_lower.endswith((".jpg", ".jpeg")):
            media_type = "image/jpeg"
        elif filename_lower.endswith(".webp"):
            media_type = "image/webp"
        elif filename_lower.endswith(".gif"):
            media_type = "image/gif"
        else:
            return

        await message.add_reaction("⏳")
        try:
            image_b64 = await image_to_base64(attachment.url)
            trade_data = await extract_trade_data(image_b64, attachment.url, media_type)
            print(f"Trade data: {trade_data}")
            tab, row = write_to_sheet(trade_data)
            t = trade_data
            trade_type = t.get("type","")
            ticker = t.get("ticker","?")
            if trade_type in ["BPS","CS"]:
                details = f"${t.get('short_strike')} / ${t.get('long_strike')} | Premium: ${t.get('premium')} | Exp: {t.get('expiration')}"
            elif trade_type == "CSP":
                details = f"Strike: ${t.get('strike_price')} | Premium: ${t.get('premium')} | Exp: {t.get('expiration')}"
            elif trade_type == "CC":
                details = f"Call Strike: ${t.get('call_strike') or t.get('strike_price')} | Premium: ${t.get('premium')} | Exp: {t.get('expiration')}"
            else:
                details = str(t)
            msg = f"✅ **Trade logged!**\n📋 Tab: **{tab}** | Row: {row}\n📊 **{ticker} {trade_type}** — {details}\n🔗 [Open Tracker](https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID})"
            await message.remove_reaction("⏳", bot.user)
            await message.add_reaction("✅")
            await message.reply(msg)
        except Exception as e:
            print(f"Trade error: {traceback.format_exc()}")
            await message.remove_reaction("⏳", bot.user)
            await message.add_reaction("❌")
            await message.reply(f"❌ Could not read trade: {str(e)}")

    except Exception as e:
        print(f"on_message crash: {traceback.format_exc()}")

print("Starting bot...")
if not DISCORD_BOT_TOKEN:
    raise ValueError("DISCORD_TRADE_BOT_TOKEN missing")
bot.run(DISCORD_BOT_TOKEN)

"""
Discord Trade Tracker Bot
Watches #trade-tracker channel for trade screenshots
Uses Claude Vision to extract trade data
Auto-fills the correct tab in Google Sheet based on trade type
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
SPREADSHEET_ID      = "1yEN54IvJ7E8h0Eh1R3HlXdWFUXO6ePQAs40IiznxjKY"
TRADE_TRACKER_CHANNEL = "trade-tracker"

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
    """Find next empty row in sheet."""
    col_a = worksheet.col_values(1)
    # Skip header rows
    for i, val in enumerate(col_a):
        if i < 3:  # skip header rows
            continue
        if not val or val.strip() == "" or val.strip() == "-":
            return i + 1
    return len(col_a) + 1

async def image_to_base64(url):
    """Download image and convert to base64."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.read()
            return base64.b64encode(data).decode("utf-8")

async def extract_trade_data(image_base64, image_url):
    """Use Claude Vision to extract trade data from screenshot."""
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
    # Strip markdown if present
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

def append_to_csp(ws, data):
    """Append to CSP tab: Symbol, Strike Price, Contracts, Entry, Expiration, Premium"""
    row = get_next_empty_row(ws)
    today = data.get("date_opened") or datetime.now().strftime("%-m/%-d/%Y")
    ws.update(f"A{row}:F{row}", [[
        data.get("ticker", ""),
        data.get("strike_price", ""),
        data.get("contracts", 1),
        today,
        data.get("expiration", ""),
        data.get("premium", ""),
    ]])
    return row

def append_to_bps(ws, data):
    """Append to BPS tab: Date Opened, Ticker, Short Strike, Long Strike, Width, Expiration, DTE, Credit Collected, Contracts"""
    row = get_next_empty_row(ws)
    today = data.get("date_opened") or datetime.now().strftime("%-m/%-d/%Y")
    
    # Calculate DTE
    try:
        exp = datetime.strptime(data.get("expiration", ""), "%m/%d/%Y")
        dte = (exp - datetime.now()).days
    except:
        dte = ""

    ws.update(f"A{row}:I{row}", [[
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
    """Append to CS tab: Date Opened, Ticker, Short Strike, Long Strike, Width, Expiration, DTE, Credit Collected, Contracts"""
    row = get_next_empty_row(ws)
    today = data.get("date_opened") or datetime.now().strftime("%-m/%-d/%Y")

    try:
        exp = datetime.strptime(data.get("expiration", ""), "%m/%d/%Y")
        dte = (exp - datetime.now()).days
    except:
        dte = ""

    ws.update(f"A{row}:I{row}", [[
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
    """Append to CC tab: Symbol, Shares, Date Opened, Expiration, Call Strike, Current Stock Price, Premium"""
    row = get_next_empty_row(ws)
    today = data.get("date_opened") or datetime.now().strftime("%-m/%-d/%Y")
    ws.update(f"A{row}:G{row}", [[
        data.get("ticker", ""),
        data.get("shares", ""),
        today,
        data.get("expiration", ""),
        data.get("call_strike", "") or data.get("strike_price", ""),
        "",  # Current stock price — leave blank, auto-updates
        data.get("premium", ""),
    ]])
    return row

def write_to_sheet(trade_data):
    """Route trade data to correct sheet tab."""
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

# ── DISCORD BOT ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"Trade Tracker Bot online as {client.user}")

@client.event
async def on_message(message):
    # Only process in #trade-tracker channel
    if message.channel.name != TRADE_TRACKER_CHANNEL:
        return
    # Ignore bot messages
    if message.author.bot:
        return
    # Only process messages with images
    if not message.attachments:
        return

    # Check if attachment is an image
    attachment = message.attachments[0]
    if not any(attachment.filename.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
        return

    # React to show processing
    await message.add_reaction("⏳")

    try:
        # Download and encode image
        image_b64 = await image_to_base64(attachment.url)

        # Extract trade data via Claude Vision
        trade_data = await extract_trade_data(image_b64, attachment.url)
        print(f"Extracted trade data: {trade_data}")

        # Write to Google Sheet
        tab, row = write_to_sheet(trade_data)

        # Build confirmation message
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

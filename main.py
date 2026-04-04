import anthropic
import requests
import asyncio
import time
import os
import pandas as pd
import ta
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime

CLAUDE_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

def parse_channel(val):
    if not val or val == "0":
        return 0
    val = str(val).strip().strip('"').strip("'")
    try:
        return int(val)
    except:
        return val if val.startswith('@') else 0

CHANNELS = {
    "free":     parse_channel(os.environ.get("TG_FREE_CHANNEL", "0")),
    "basic":    parse_channel(os.environ.get("TG_BASIC_CHANNEL", "0")),
    "standard": parse_channel(os.environ.get("TG_STANDARD_CHANNEL", "0")),
    "premium":  parse_channel(os.environ.get("TG_PREMIUM_CHANNEL", "0")),
}

STABLECOINS = ["usdt","usdc","busd","dai","tusd","usds","usdp","usde","usd1",
               "usdf","usdg","usyc","pyusd","buidl","frax","lusd","gusd","usdd",
               "fdusd","crvusd","rain","xaut","paxg","rlusd","bfusd","usdy","wlfi"]

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
prev_prices = {}
last_signal_times = {}

def get_top_coins():
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {"vs_currency":"usd","order":"market_cap_desc","per_page":100,"page":1,"sparkline":False}
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if not isinstance(data, list):
            print(f"CoinGecko error: {data}")
            return []
        filtered = [c for c in data if isinstance(c, dict) and c.get("symbol","").lower() not in STABLECOINS]
        return filtered[:50]
    except Exception as e:
        print(f"get_top_coins error: {e}")
        return []

def generate_signal(coin_name, price, change_pct, fear_greed):
    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role":"user","content":f"""
You are the world's best crypto analyst.
{coin_name} just moved {change_pct:+.2f}% - give an urgent signal.
Current Price: ${price:,.4f}
Price Change: {change_pct:+.2f}%
Fear & Greed: {fear_greed}
Respond ONLY in this exact format:
DIRECTION: UP or DOWN
PERCENTAGE: X.X%
CONFIDENCE: XX%
REASON: (one sentence)
            """}]
        )
        return message.content[0].text
    except Exception as e:
        return f"DIRECTION: UP\nPERCENTAGE: 2.0%\nCONFIDENCE: 50%\nREASON: Error: {e}"

async def send_signal(plan, coin_name, symbol, price, change_pct, signal):
    direction_emoji = "📈" if "UP" in signal else "📉"
    msg = f"""
⚡ *LIVE SIGNAL - GanoFlow*
━━━━━━━━━━━━━━━━━━━━
🪙 {symbol} - {coin_name}
🔔 Moved {change_pct:+.2f}%!
💰 Price: ${price:,.4f}
━━━━━━━━━━━━━━━━━━━━
{direction_emoji} {signal}
━━━━━━━━━━━━━━━━━━━━
⚠️ For reference only. Trade at your own risk.
    """
    channel_id = CHANNELS.get(plan, 0)
    if not channel_id or channel_id == 0:
        return
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        await bot.send_message(chat_id=channel_id, text=msg, parse_mode="Markdown")
    except Exception as e:
        print(f"Error sending to {plan}: {e}")

async def monitor():
    global prev_prices, last_signal_times
    print("🚀 GanoFlow Signal Monitor Started!")
    while True:
        try:
            fear_greed = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()
            fg_value = fear_greed["data"][0]["value"]
            coins = get_top_coins()
            if not coins:
                await asyncio.sleep(60)
                continue
            print(f"\n📊 Checking {len(coins)} coins... [{datetime.now().strftime('%H:%M:%S')}]")
            for i, coin in enumerate(coins):
                coin_id = coin.get("id", "")
                symbol = coin.get("symbol", "").upper()
                name = coin.get("name", "")
                current_price = coin.get("current_price", 0)
                if not current_price:
                    continue
                if coin_id not in prev_prices:
                    prev_prices[coin_id] = current_price
                    continue
                prev_price = prev_prices[coin_id]
                change_pct = ((current_price - prev_price) / prev_price) * 100
                if abs(change_pct) >= 1.0:
                    now = time.time()
                    if now - last_signal_times.get(coin_id, 0) > 300:
                        print(f"\n🔔 {symbol} moved {change_pct:+.2f}%!")
                        signal = generate_signal(name, current_price, change_pct, fg_value)
                        if coin_id in ["bitcoin", "ethereum"]:
                            await send_signal("free", name, symbol, current_price, change_pct, signal)
                        if i < 10:
                            await send_signal("basic", name, symbol, current_price, change_pct, signal)
                        if i < 25:
                            await send_signal("standard", name, symbol, current_price, change_pct, signal)
                        await send_signal("premium", name, symbol, current_price, change_pct, signal)
                        prev_prices[coin_id] = current_price
                        last_signal_times[coin_id] = now
            await asyncio.sleep(30)
        except Exception as e:
            print(f"\n❌ Monitor error: {e}")
            await asyncio.sleep(60)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""
👋 Welcome to GanoFlow!
Commands:
/signal - Get latest Bitcoin signal
/subscribe - View our plans
/help - Show this menu
⚠️ For reference only. Trade at your own risk.
    """)

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Analyzing market... Please wait...")
    try:
        data = requests.get("https://api.binance.com/api/v3/klines",
            params={"symbol":"BTCUSDT","interval":"1h","limit":100}, timeout=10).json()
        closes = [float(x[4]) for x in data]
        df = pd.DataFrame(closes, columns=["close"])
        df["rsi"] = ta.momentum.RSIIndicator(df["close"]).rsi()
        fg_resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()
        fg_data = fg_resp.get("data", [{"value":"50","value_classification":"Neutral"}])[0]
        fg_value = fg_data.get("value", "50")
        fg_label = fg_data.get("value_classification", "Neutral")
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role":"user","content":f"""
You are a crypto analyst. Give a Bitcoin signal.
RSI: {round(float(df['rsi'].iloc[-1]), 2)}
Fear & Greed: {fg_value} ({fg_label})
Current Price: ${closes[-1]:,.2f}
Respond in this exact format:
DIRECTION: UP or DOWN
PERCENTAGE: X.X%
CONFIDENCE: XX%
REASON: (one sentence)
            """}]
        )
        signal_text = msg.content[0].text
        direction = "📈" if "UP" in signal_text else "📉"
        await update.message.reply_text(f"""
{direction} *BITCOIN SIGNAL - GanoFlow*
━━━━━━━━━━━━━━━━━━━━
💰 Price: ${closes[-1]:,.2f}
😱 Fear & Greed: {fg_value} ({fg_label})
📊 RSI: {round(float(df['rsi'].iloc[-1]), 2)}
━━━━━━━━━━━━━━━━━━━━
{signal_text}
━━━━━━━━━━━━━━━━━━━━
⚠️ For reference only. Trade at your own risk.
        """, parse_mode="Markdown")
    except Exception as e:
        print(f"signal_cmd error: {e}")
        await update.message.reply_text(f"❌ Error: {e}")

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""
💎 GanoFlow Plans
🆓 Free — BTC + ETH only
⚡ Basic — $29/mo — Top 10 coins
🚀 Standard — $59/mo — Top 25 coins
👑 Premium — $99/mo — Top 50 coins
🌐 https://ganoflow.com
    """)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{"role":"user","content":f"You are GanoFlow customer support. Be helpful and concise. User: {user_message}"}]
    )
    await update.message.reply_text(msg.content[0].text)

async def main():
    print("🚀 GanoFlow Bot + Signal Monitor Starting...")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    await app.initialize()
    await app.start()
    try:
        await app.updater.start_polling(drop_pending_updates=True)
    except Exception as e:
        print(f"Polling error (ignored): {e}")
    print("✅ Bot is running!")
    await monitor()

asyncio.run(main())

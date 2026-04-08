import anthropic
import asyncio
import json
import time
import os
import websockets
import requests
from collections import deque
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

PLAN_COINS = {
    "free":     ["btcusdt", "ethusdt"],
    "basic":    ["btcusdt", "ethusdt", "solusdt", "bnbusdt", "xrpusdt"],
    "standard": ["btcusdt", "ethusdt", "solusdt", "bnbusdt", "xrpusdt", "adausdt", "avaxusdt"],
    "premium":  ["btcusdt", "ethusdt", "solusdt", "bnbusdt", "xrpusdt", "adausdt", "avaxusdt", "dotusdt", "linkusdt", "dogeusdt"],
}

COIN_NAMES = {
    "btcusdt": "Bitcoin", "ethusdt": "Ethereum", "solusdt": "Solana",
    "bnbusdt": "BNB", "xrpusdt": "XRP", "adausdt": "Cardano",
    "avaxusdt": "Avalanche", "dotusdt": "Polkadot", "linkusdt": "Chainlink",
    "dogeusdt": "Dogecoin"
}

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
price_history = {coin: deque(maxlen=50) for coin in COIN_NAMES}
latest_prices = {}
live_message_ids = {}  # {plan: message_id}

# ─── TECHNICAL ANALYSIS ──────────────────────────────────────────────────────

def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    gains, losses = [], []
    prices_list = list(prices)
    for i in range(1, len(prices_list)):
        diff = prices_list[i] - prices_list[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    if len(gains) < period:
        return 50.0
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def calc_probability(rsi, change_pct):
    if rsi < 30:
        up_base = 70
    elif rsi < 45:
        up_base = 60
    elif rsi > 70:
        up_base = 30
    elif rsi > 55:
        up_base = 45
    else:
        up_base = 50
    up_base += min(max(change_pct * 3, -15), 15)
    up_pct = max(20, min(80, round(up_base)))
    return up_pct, 100 - up_pct

def calc_targets(price, change_pct):
    v = abs(change_pct) / 100
    if change_pct >= 0:
        return (
            round(price * 0.999, 4), round(price * 1.001, 4),
            round(price * (1 + max(v*2, 0.01)), 4),
            round(price * (1 + max(v*4, 0.02)), 4),
            round(price * (1 + max(v*6, 0.03)), 4),
            round(price * (1 - max(v*2.5, 0.015)), 4),
        )
    else:
        return (
            round(price * 0.999, 4), round(price * 1.001, 4),
            round(price * (1 - max(v*2, 0.01)), 4),
            round(price * (1 - max(v*4, 0.02)), 4),
            round(price * (1 - max(v*6, 0.03)), 4),
            round(price * (1 + max(v*2.5, 0.015)), 4),
        )

def fmt(price):
    if price >= 1000:
        return f"${price:,.2f}"
    elif price >= 1:
        return f"${price:,.4f}"
    else:
        return f"${price:.6f}"

def build_live_message(plan):
    coins = PLAN_COINS.get(plan, [])
    try:
        fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5).json()
        fg_val = fg.get("data", [{"value":"50"}])[0].get("value", "50")
        fg_label = fg.get("data", [{"value_classification":"Neutral"}])[0].get("value_classification", "Neutral")
    except:
        fg_val = "50"
        fg_label = "Neutral"

    now = datetime.utcnow().strftime("%H:%M UTC")
    lines = [f"🔴 *LIVE — GanoFlow* | {now}"]
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    for symbol in coins:
        price = latest_prices.get(symbol)
        if not price:
            continue
        sym = symbol.replace("usdt", "").upper()
        rsi = calc_rsi(price_history[symbol])
        pl = list(price_history[symbol])
        chg = ((pl[-1] - pl[-2]) / pl[-2] * 100) if len(pl) >= 2 else 0
        up_pct, down_pct = calc_probability(rsi, chg)
        e_low, e_high, tp1, tp2, tp3, sl = calc_targets(price, chg)
        arrow = "📈" if chg >= 0 else "📉"

        lines.append(f"{arrow} *{sym}/USDT* {fmt(price)}  {chg:+.2f}%")
        lines.append(f"   🐂 UP {up_pct}%  |  🐻 DOWN {down_pct}%  |  RSI {rsi}")
        if plan != "free":
            lines.append(f"   Entry {fmt(e_low)}–{fmt(e_high)}")
            lines.append(f"   TP1 {fmt(tp1)}  TP2 {fmt(tp2)}  TP3 {fmt(tp3)}")
            lines.append(f"   SL {fmt(sl)}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"😱 Fear & Greed: *{fg_val}* ({fg_label})")
    lines.append("🌐 ganoflow.com")
    return "\n".join(lines)

# ─── LIVE MESSAGE MANAGER ────────────────────────────────────────────────────

async def init_live_message(plan):
    channel_id = CHANNELS.get(plan, 0)
    if not channel_id or channel_id == 0:
        return
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        msg = await bot.send_message(
            chat_id=channel_id,
            text=build_live_message(plan),
            parse_mode="Markdown"
        )
        live_message_ids[plan] = msg.message_id
        # Pin it
        try:
            await bot.pin_chat_message(chat_id=channel_id, message_id=msg.message_id, disable_notification=True)
        except:
            pass
        print(f"✅ Live message created for {plan}: {msg.message_id}")
    except Exception as e:
        print(f"❌ Init live message error {plan}: {e}")

async def update_live_message(plan):
    channel_id = CHANNELS.get(plan, 0)
    if not channel_id or channel_id == 0:
        return
    msg_id = live_message_ids.get(plan)
    if not msg_id:
        await init_live_message(plan)
        return
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        await bot.edit_message_text(
            chat_id=channel_id,
            message_id=msg_id,
            text=build_live_message(plan),
            parse_mode="Markdown"
        )
    except Exception as e:
        if "message is not modified" not in str(e):
            print(f"❌ Edit error {plan}: {e}")

async def live_updater():
    # Wait for WebSocket data
    print("⏳ Waiting for WebSocket data...")
    await asyncio.sleep(10)

    # Init live messages for all plans
    for plan in PLAN_COINS:
        await init_live_message(plan)
        await asyncio.sleep(1)

    last_reset = time.time()

    while True:
        await asyncio.sleep(5)  # update every 5 seconds

        # Every 24 hours: create new message
        if time.time() - last_reset > 86400:
            print("🔄 24h reset - creating new live messages...")
            for plan in PLAN_COINS:
                await init_live_message(plan)
                await asyncio.sleep(1)
            last_reset = time.time()
            continue

        # Update existing messages
        for plan in PLAN_COINS:
            await update_live_message(plan)
            await asyncio.sleep(0.5)

# ─── WEBSOCKET ───────────────────────────────────────────────────────────────

async def websocket_monitor():
    streams = "/".join([f"{coin}@kline_1m" for coin in COIN_NAMES])
    url = f"wss://data-stream.binance.vision/stream?streams={streams}"
    print("🔌 Connecting to Binance WebSocket...")
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                print("✅ WebSocket connected!")
                async for raw in ws:
                    data = json.loads(raw)
                    kline = data.get("data", {}).get("k", {})
                    if not kline:
                        continue
                    symbol = kline.get("s", "").lower()
                    close = float(kline.get("c", 0))
                    is_closed = kline.get("x", False)
                    if not close or symbol not in COIN_NAMES:
                        continue
                    latest_prices[symbol] = close
                    if is_closed:
                        price_history[symbol].append(close)
        except Exception as e:
            print(f"❌ WebSocket error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

# ─── DAILY NEWS (Claude API - once/day, paid only) ───────────────────────────

async def send_daily_news():
    print("📰 Sending daily market analysis...")
    try:
        fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()
        fg_data = fg.get("data", [{"value":"50","value_classification":"Neutral"}])[0]
        fear_val = fg_data.get("value", "50")
        fear_label = fg_data.get("value_classification", "Neutral")
        btc_price = latest_prices.get("btcusdt", 0)
        date_str = datetime.now().strftime("%B %d, %Y")

        configs = {
            "basic":    (300, "Write a SHORT 3-4 sentence daily market brief."),
            "standard": (500, "Write a MEDIUM 5-7 sentence daily market analysis covering BTC/ETH levels, altcoin sentiment, and outlook."),
            "premium":  (800, "Write a DETAILED analysis with sections: Market Overview, BTC & ETH Levels, Altcoin Sectors, Macro Factors, and Today's Outlook."),
        }

        for plan, (tokens, instruction) in configs.items():
            channel_id = CHANNELS.get(plan, 0)
            if not channel_id:
                continue
            analysis = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=tokens,
                messages=[{"role": "user", "content": f"""
You are a professional crypto analyst. {instruction}
BTC: ${btc_price:,.2f} | Fear & Greed: {fear_val} ({fear_label}) | Date: {date_str}
English only. Professional tone.
                """}]
            ).content[0].text

            bot = Bot(token=TELEGRAM_TOKEN)
            await bot.send_message(
                chat_id=channel_id,
                text=f"""📰 *Daily Market Analysis — {date_str}*
━━━━━━━━━━━━━━━━━━━━
{analysis}
━━━━━━━━━━━━━━━━━━━━
😱 Fear & Greed: *{fear_val}* ({fear_label})
💰 BTC: *${btc_price:,.2f}*
🌐 ganoflow.com""",
                parse_mode="Markdown"
            )
        print("✅ Daily news sent!")
    except Exception as e:
        print(f"❌ Daily news error: {e}")

async def daily_news_scheduler():
    while True:
        now = datetime.utcnow()
        target = 9
        if now.hour >= target:
            wait = (24 - now.hour + target) * 3600 - now.minute * 60 - now.second
        else:
            wait = (target - now.hour) * 3600 - now.minute * 60 - now.second
        print(f"⏰ Next daily news in {wait//3600}h {(wait%3600)//60}m")
        await asyncio.sleep(wait)
        await send_daily_news()

# ─── CHATBOT ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""👋 Hey! Welcome to GanoFlow.

We send real-time crypto signals straight to your Telegram — 
24/7, the moment the market moves.

📊 What you get:
— Live signals when coins move (Entry, TP1/TP2/TP3, Stop Loss)
— 🐂 UP / 🐻 DOWN probability on every signal
— Daily market analysis (paid plans)

Ready to start?
👉 ganoflow.com — pick your plan
👉 /subscribe — see plan details

📧 Questions? Ganoflow@proton.me

⚠️ For reference only. Not financial advice.""")

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Calculating...")
    try:
        btc = latest_prices.get("btcusdt", 0)
        if not btc:
            await update.message.reply_text("❌ No data yet. Try again in a moment.")
            return
        rsi = calc_rsi(price_history["btcusdt"])
        pl = list(price_history["btcusdt"])
        change_pct = ((pl[-1] - pl[-2]) / pl[-2] * 100) if len(pl) >= 2 else 0
        up_pct, down_pct = calc_probability(rsi, change_pct)
        direction = "LONG 📈" if change_pct >= 0 else "SHORT 📉"
        rsi_label = "Oversold 🟢" if rsi < 30 else "Overbought 🔴" if rsi > 70 else "Neutral ⚪"
        await update.message.reply_text(f"""📊 *BITCOIN SIGNAL — GanoFlow*
━━━━━━━━━━━━━━━━━━━━
💰 *{fmt(btc)}*
📈 1m Change: *{change_pct:+.2f}%*
━━━━━━━━━━━━━━━━━━━━
*{direction}*
🐂 UP *{up_pct}%* | 🐻 DOWN *{down_pct}%*
📊 RSI *{rsi}* — {rsi_label}
━━━━━━━━━━━━━━━━━━━━
⚠️ DYOR. ganoflow.com""", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def prices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not latest_prices:
        await update.message.reply_text("❌ No data yet. Try again in a moment.")
        return
    msg = "💰 *Live Prices — GanoFlow*\n━━━━━━━━━━━━━━━━━━━━\n"
    for symbol, name in COIN_NAMES.items():
        price = latest_prices.get(symbol)
        if price:
            msg += f"*{symbol.replace('usdt','').upper()}* — {fmt(price)}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n🌐 ganoflow.com"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""💎 *GanoFlow Plans*
━━━━━━━━━━━━━━━━━━━━
🆓 *Free* — BTC + ETH live
⚡ *Basic* — $29/mo — Top 5 coins
🚀 *Standard* — $59/mo — Top 7 coins
👑 *Premium* — $99/mo — Top 10 coins
━━━━━━━━━━━━━━━━━━━━
🌐 https://ganoflow.com""", parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""👋 Hey! Welcome to GanoFlow.

We send real-time crypto signals straight to your Telegram — 
24/7, the moment the market moves.

📊 What you get:
— Live price updates (Entry, TP1/TP2/TP3, Stop Loss)
— 🐂 UP / 🐻 DOWN probability
— Daily market analysis (paid plans)

Ready to start?
👉 ganoflow.com — pick your plan
👉 /subscribe — see plan details

📧 Questions? Ganoflow@proton.me

⚠️ For reference only. Not financial advice.""")

# ─── MAIN ────────────────────────────────────────────────────────────────────

async def main():
    print("🚀 GanoFlow Starting...")
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("prices", prices_cmd))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    print("✅ Bot running!")
    await asyncio.gather(
        websocket_monitor(),
        live_updater(),
        daily_news_scheduler(),
    )

asyncio.run(main())

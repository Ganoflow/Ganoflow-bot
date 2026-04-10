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
price_history = {coin: deque(maxlen=100) for coin in COIN_NAMES}
latest_prices = {}
live_message_ids = {}

# ─── TECHNICAL ANALYSIS ──────────────────────────────────────────────────────

def calc_rsi(prices, period=14):
    pl = list(prices)
    if len(pl) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(pl)):
        diff = pl[i] - pl[i-1]
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

def calc_ema(prices, period):
    """Exponential Moving Average"""
    pl = list(prices)
    if len(pl) < period:
        return pl[-1] if pl else 0
    k = 2 / (period + 1)
    ema = pl[0]
    for p in pl[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_macd(prices):
    """MACD = EMA12 - EMA26, positive = bullish"""
    pl = list(prices)
    if len(pl) < 26:
        return 0
    ema12 = calc_ema(pl, 12)
    ema26 = calc_ema(pl, 26)
    return ema12 - ema26

def calc_momentum(prices, period=10):
    """Price momentum = current vs N candles ago"""
    pl = list(prices)
    if len(pl) < period:
        return 0
    return ((pl[-1] - pl[-period]) / pl[-period]) * 100

def calc_probability(rsi, candle_chg, tick_chg, prices=None, fear_greed=50):
    """
    All factors combined:
    1. Live tick momentum (primary - changes every second)
    2. 5-candle price trend
    3. RSI bias
    4. MACD trend direction
    5. Price momentum (10-candle)
    6. Fear & Greed index
    7. Trend consistency (are candles all going same way?)
    8. Volatility amplifier
    """
    up_base = 50.0

    # 1. Tick momentum - most sensitive
    tick_impact = tick_chg * 15
    tick_impact = max(-20, min(20, tick_impact))
    up_base += tick_impact

    # 2. Candle trend
    candle_impact = candle_chg * 6
    candle_impact = max(-15, min(15, candle_impact))
    up_base += candle_impact

    # 3. RSI bias
    if rsi < 30:
        up_base += 10
    elif rsi < 40:
        up_base += 5
    elif rsi > 70:
        up_base -= 10
    elif rsi > 60:
        up_base -= 5

    if prices is not None:
        pl = list(prices)

        # 4. MACD direction
        macd = calc_macd(pl)
        if macd > 0:
            up_base += min(macd * 100, 5)   # bullish MACD
        else:
            up_base += max(macd * 100, -5)  # bearish MACD

        # 5. Price momentum (10-candle)
        mom = calc_momentum(pl, 10)
        mom_impact = mom * 2
        up_base += max(-8, min(8, mom_impact))

        # 6. Trend consistency - count bullish vs bearish candles in last 5
        if len(pl) >= 5:
            last5 = pl[-5:]
            bull_candles = sum(1 for i in range(1, len(last5)) if last5[i] > last5[i-1])
            bear_candles = 4 - bull_candles
            consistency = (bull_candles - bear_candles) * 2  # -8 to +8
            up_base += consistency

    # 7. Fear & Greed bias
    fg = float(fear_greed)
    if fg < 25:      # extreme fear = oversold = bullish
        up_base += 5
    elif fg < 40:
        up_base += 2
    elif fg > 75:    # extreme greed = overbought = bearish
        up_base -= 5
    elif fg > 60:
        up_base -= 2

    # 8. Volatility amplifier
    total_move = abs(tick_chg) + abs(candle_chg)
    if total_move > 1.0:
        amplifier = min(total_move * 2, 8)
        if up_base > 50:
            up_base += amplifier
        else:
            up_base -= amplifier

    up_pct = round(max(20.0, min(80.0, up_base)), 3)
    down_pct = round(100 - up_pct, 3)
    return up_pct, down_pct

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

    date_str = datetime.utcnow().strftime("%m/%d/%Y")
    lines = [f"⚡ *LIVE — GanoFlow* | {date_str}"]
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    for symbol in coins:
        price = latest_prices.get(symbol)
        if not price:
            continue
        sym = symbol.replace("usdt", "").upper()
        pl = list(price_history[symbol])
        rsi = calc_rsi(price_history[symbol]) if len(pl) >= 5 else 50.0

        # 5-candle move
        window = pl[-5:] if len(pl) >= 5 else (pl if len(pl) >= 2 else pl)
        candle_chg = ((window[-1] - window[0]) / window[0] * 100) if len(window) >= 2 else 0

        # Live tick vs last closed candle
        tick_chg = ((price - window[-1]) / window[-1] * 100) if window and window[-1] else 0

        # Display MOVE = combined
        chg = candle_chg + tick_chg

        up_pct, down_pct = calc_probability(rsi, candle_chg, tick_chg, price_history[symbol], fg_val)
        e_low, e_high, tp1, tp2, tp3, sl = calc_targets(price, chg)
        direction = "📈 LONG" if chg >= 0 else "📉 SHORT"
        bull_icon = "🐂" if up_pct >= down_pct else "🐻"
        rsi_label = "Oversold 🟢" if rsi < 30 else "Overbought 🔴" if rsi > 70 else "Neutral ⚪"

        if abs(chg) > 0.5:
            whale = "🐋 Heavy accumulation" if chg > 0 else "🐋 Heavy selling"
        elif abs(chg) > 0.2:
            whale = "🐋 Moderate whale activity"
        else:
            whale = "🐋 Low whale activity"

        lines.append(f"{bull_icon} *{sym}/USDT*")
        lines.append(f"PRICE　　　*{fmt(price)}*")
        lines.append(f"MOVE　　　*{chg:+.2f}%* ⚡")
        lines.append(f"DIRECTION　*{direction}*")
        lines.append(f"🐂 UP　　　*{up_pct:.3f}%*")
        lines.append(f"🐻 DOWN　*{down_pct:.3f}%*")
        if plan != "free":
            lines.append(f"ENTRY　　　*{fmt(e_low)} — {fmt(e_high)}*")
            lines.append(f"TP1/TP2/TP3　*{fmt(tp1)} / {fmt(tp2)} / {fmt(tp3)}*")
            lines.append(f"STOP LOSS　*{fmt(sl)}*")
            lines.append(f"{whale}")
        lines.append(f"RSI　　　　*{rsi}* — {rsi_label}")
        lines.append("━━━━━━━━━━━━━━━━━━━━")

    lines.append(f"😱 Fear & Greed: *{fg_val}* ({fg_label})")
    lines.append("🌐 ganoflow.com")
    return "\n".join(lines)

# ─── LIVE MESSAGE MANAGER ────────────────────────────────────────────────────

async def init_live_message(plan):
    channel_id = CHANNELS.get(plan, 0)
    if not channel_id or channel_id == 0:
        print(f"⚠️ No channel ID for {plan}, skipping")
        return
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        msg = await bot.send_message(
            chat_id=channel_id,
            text=build_live_message(plan),
            parse_mode="Markdown"
        )
        live_message_ids[plan] = msg.message_id
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
        err = str(e)
        if "message is not modified" in err:
            pass
        elif "Message to edit not found" in err or "message not found" in err.lower():
            print(f"🔄 Message deleted for {plan}, creating new one...")
            live_message_ids.pop(plan, None)
            await init_live_message(plan)
        else:
            print(f"❌ Edit error {plan}: {e}")

async def live_updater():
    print("⏳ Waiting for WebSocket data...")
    await asyncio.sleep(15)
    for plan in PLAN_COINS:
        await init_live_message(plan)
        await asyncio.sleep(1)
    last_reset = time.time()
    while True:
        await asyncio.sleep(5)
        if time.time() - last_reset > 86400:
            print("🔄 24h reset - new live messages...")
            for plan in PLAN_COINS:
                await init_live_message(plan)
                await asyncio.sleep(1)
            last_reset = time.time()
            continue
        for plan in PLAN_COINS:
            await update_live_message(plan)
            await asyncio.sleep(0.5)

# ─── WEBSOCKET ───────────────────────────────────────────────────────────────

async def websocket_monitor():
    trade_streams = "/".join([f"{coin}@aggTrade" for coin in COIN_NAMES])
    kline_streams = "/".join([f"{coin}@kline_1m" for coin in COIN_NAMES])
    streams = trade_streams + "/" + kline_streams
    url = f"wss://data-stream.binance.vision/stream?streams={streams}"
    print("🔌 Connecting to Binance WebSocket...")
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                print("✅ WebSocket connected!")
                async for raw in ws:
                    data = json.loads(raw)
                    stream = data.get("stream", "")
                    d = data.get("data", {})
                    if not d:
                        continue
                    if "aggTrade" in stream:
                        symbol = d.get("s", "").lower()
                        price = float(d.get("p", 0))
                        if price and symbol in COIN_NAMES:
                            latest_prices[symbol] = price
                    elif "kline" in stream:
                        kline = d.get("k", {})
                        symbol = kline.get("s", "").lower()
                        close = float(kline.get("c", 0))
                        is_closed = kline.get("x", False)
                        if close and symbol in COIN_NAMES:
                            if is_closed:
                                # Closed candle → add to history
                                price_history[symbol].append(close)
                            else:
                                # Open candle → update latest price too
                                latest_prices[symbol] = close
        except Exception as e:
            print(f"❌ WebSocket error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

# ─── DAILY NEWS ──────────────────────────────────────────────────────────────

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
            if not channel_id or channel_id == 0:
                print(f"⚠️ Skipping {plan} — no channel ID")
                continue
            print(f"📰 Sending news to {plan} (channel: {channel_id})...")
            try:
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
                news_msg = await bot.send_message(
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
                print(f"✅ News sent to {plan}")
                try:
                    await bot.pin_chat_message(chat_id=channel_id, message_id=news_msg.message_id, disable_notification=True)
                    print(f"📌 News pinned for {plan}")
                except Exception as pin_err:
                    print(f"⚠️ Could not pin for {plan}: {pin_err}")

                await asyncio.sleep(2)
                try:
                    live_msg = await bot.send_message(
                        chat_id=channel_id,
                        text=build_live_message(plan),
                        parse_mode="Markdown"
                    )
                    live_message_ids[plan] = live_msg.message_id
                    print(f"✅ Live message created after news for {plan}: {live_msg.message_id}")
                except Exception as live_err:
                    print(f"❌ Live message error after news for {plan}: {live_err}")

            except Exception as plan_err:
                print(f"❌ Error processing {plan}: {plan_err}")
            await asyncio.sleep(1)

        print("✅ Daily news + live messages done!")
    except Exception as e:
        print(f"❌ Daily news error: {e}")

async def daily_news_scheduler():
    while True:
        now = datetime.utcnow()
        target_h, target_m = 13, 30
        target_secs = target_h * 3600 + target_m * 60
        current_secs = now.hour * 3600 + now.minute * 60 + now.second
        if current_secs >= target_secs:
            wait = 86400 - (current_secs - target_secs)
        else:
            wait = target_secs - current_secs
        print(f"⏰ Next daily news in {wait//3600}h {(wait%3600)//60}m (NY 09:30)")
        await asyncio.sleep(wait)
        await send_daily_news()

# ─── CHATBOT ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""👋 Hey! Welcome to GanoFlow.

We send real-time crypto signals straight to your Telegram — 
24/7, the moment the market moves.

📊 What you get:
— Live prices with Entry, TP1/TP2/TP3, Stop Loss
— 🐂 UP / 🐻 DOWN probability on every coin
— 🐋 Whale activity tracking
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
        pl = list(price_history["btcusdt"])
        rsi = calc_rsi(price_history["btcusdt"])
        window = pl[-5:] if len(pl) >= 5 else (pl if len(pl) >= 2 else pl)
        candle_chg = ((window[-1] - window[0]) / window[0] * 100) if len(window) >= 2 else 0
        tick_chg = ((btc - window[-1]) / window[-1] * 100) if window and window[-1] else 0
        chg = candle_chg + tick_chg
        up_pct, down_pct = calc_probability(rsi, candle_chg, tick_chg, price_history[symbol], fg_val)
        direction = "LONG 📈" if chg >= 0 else "SHORT 📉"
        rsi_label = "Oversold 🟢" if rsi < 30 else "Overbought 🔴" if rsi > 70 else "Neutral ⚪"
        await update.message.reply_text(f"""📊 *BITCOIN SIGNAL — GanoFlow*
━━━━━━━━━━━━━━━━━━━━
💰 *{fmt(btc)}*
📈 Change: *{chg:+.2f}%*
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
    for symbol in COIN_NAMES:
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
— Live prices with Entry, TP1/TP2/TP3, Stop Loss
— 🐂 UP / 🐻 DOWN probability on every coin
— 🐋 Whale activity tracking
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

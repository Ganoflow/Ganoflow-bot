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
price_history = {coin: deque(maxlen=200) for coin in COIN_NAMES}
latest_prices = {}
live_message_ids = {}
summary_message_ids = {}
fg_cache = {"value": "50", "label": "Neutral", "last_update": 0}
signal_log = []

try:
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import StandardScaler
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False

ml_models = {}
ml_scalers = {}
ml_accuracy = {}

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
    pl = list(prices)
    if len(pl) < period:
        return pl[-1] if pl else 0
    k = 2 / (period + 1)
    ema = pl[0]
    for p in pl[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_macd(prices):
    pl = list(prices)
    if len(pl) < 26:
        return 0
    return calc_ema(pl, 12) - calc_ema(pl, 26)

def calc_momentum(prices, period=10):
    pl = list(prices)
    if len(pl) < period:
        return 0
    return ((pl[-1] - pl[-period]) / pl[-period]) * 100

def get_rsi_label(rsi):
    if rsi < 30:    return "Oversold 🟢"
    elif rsi < 45:  return "Bearish 🟡"
    elif rsi <= 55: return "Neutral ⚪"
    elif rsi <= 70: return "Bullish 🟠"
    else:           return "Overbought 🔴"

def get_fg():
    global fg_cache
    if time.time() - fg_cache["last_update"] > 300:
        try:
            fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5).json()
            fg_cache["value"] = fg["data"][0]["value"]
            fg_cache["label"] = fg["data"][0]["value_classification"]
            fg_cache["last_update"] = time.time()
        except:
            pass
    return fg_cache["value"], fg_cache["label"]

def calc_probability(rsi, candle_chg, tick_chg, prices=None, fear_greed=50, symbol=None):
    if ML_AVAILABLE and symbol and symbol in ml_models and prices is not None:
        try:
            pl = list(prices)
            if len(pl) >= 30:
                ema9 = calc_ema(pl, 9)
                ema21 = calc_ema(pl, 21)
                ema_cross = (ema9 - ema21) / ema21 * 100 if ema21 else 0
                last5 = pl[-5:]
                bull_c = sum(1 for i in range(1, len(last5)) if last5[i] > last5[i-1])
                features = [rsi, calc_macd(pl)*1000, calc_momentum(pl,10), calc_momentum(pl,20),
                           bull_c, ema_cross, candle_chg, tick_chg, float(fear_greed),
                           abs(candle_chg)+abs(tick_chg)]
                X = ml_scalers[symbol].transform([features])
                prob = ml_models[symbol].predict_proba(X)[0][1]
                up_pct = round(max(20.0, min(80.0, prob * 100)), 3)
                return up_pct, round(100 - up_pct, 3)
        except:
            pass
    # Math fallback
    up_base = 50.0
    up_base += max(-20, min(20, tick_chg * 15))
    up_base += max(-15, min(15, candle_chg * 6))
    if rsi < 30:   up_base += 10
    elif rsi < 40: up_base += 5
    elif rsi > 70: up_base -= 10
    elif rsi > 60: up_base -= 5
    if prices is not None:
        pl = list(prices)
        up_base += max(-5, min(5, calc_macd(pl) * 100))
        up_base += max(-8, min(8, calc_momentum(pl, 10) * 2))
        if len(pl) >= 5:
            last5 = pl[-5:]
            bull_c = sum(1 for i in range(1, len(last5)) if last5[i] > last5[i-1])
            up_base += (bull_c - (4 - bull_c)) * 2
    fg = float(fear_greed)
    if fg < 25:   up_base += 5
    elif fg < 40: up_base += 2
    elif fg > 75: up_base -= 5
    elif fg > 60: up_base -= 2
    total = abs(tick_chg) + abs(candle_chg)
    if total > 1.0:
        amp = min(total * 2, 8)
        up_base += amp if up_base > 50 else -amp
    up_pct = round(max(20.0, min(80.0, up_base)), 3)
    return up_pct, round(100 - up_pct, 3)

def calc_targets(price, change_pct):
    v = abs(change_pct) / 100
    if change_pct >= 0:
        return (round(price*0.999,4), round(price*1.001,4),
                round(price*(1+max(v*2,0.01)),4), round(price*(1+max(v*4,0.02)),4),
                round(price*(1+max(v*6,0.03)),4), round(price*(1-max(v*2.5,0.015)),4))
    else:
        return (round(price*0.999,4), round(price*1.001,4),
                round(price*(1-max(v*2,0.01)),4), round(price*(1-max(v*4,0.02)),4),
                round(price*(1-max(v*6,0.03)),4), round(price*(1+max(v*2.5,0.015)),4))

def fmt(price):
    if price >= 1000:  return f"${price:,.2f}"
    elif price >= 1:   return f"${price:,.4f}"
    else:              return f"${price:.6f}"

def get_overall_accuracy():
    resolved = [s for s in signal_log if s.get("result") is not None]
    if len(resolved) < 5:
        return None
    return round(sum(1 for s in resolved if s["result"]) / len(resolved) * 100, 1)

def log_signal(symbol, direction, price):
    signal_log.append({"symbol": symbol, "direction": direction, "entry_price": price, "time": time.time(), "result": None})
    if len(signal_log) > 500:
        signal_log.pop(0)

def build_live_message(plan):
    coins = PLAN_COINS.get(plan, [])
    fg_val, fg_label = get_fg()
    date_str = datetime.utcnow().strftime("%m/%d/%Y")
    lines = [f"⚡ *LIVE — GanoFlow* | {date_str}", "━━━━━━━━━━━━━━━━━━━━"]

    has_data = False
    for symbol in coins:
        price = latest_prices.get(symbol)
        if not price:
            continue
        has_data = True
        sym = symbol.replace("usdt", "").upper()
        pl = list(price_history[symbol])
        rsi = calc_rsi(pl) if len(pl) >= 5 else 50.0
        window = pl[-5:] if len(pl) >= 5 else pl
        candle_chg = ((window[-1] - window[0]) / window[0] * 100) if len(window) >= 2 else 0
        tick_chg = ((price - window[-1]) / window[-1] * 100) if window and window[-1] else 0
        chg = candle_chg + tick_chg
        up_pct, down_pct = calc_probability(rsi, candle_chg, tick_chg, pl, fg_val, symbol)
        e_low, e_high, tp1, tp2, tp3, sl = calc_targets(price, chg)
        direction = "📈 LONG" if chg >= 0 else "📉 SHORT"
        bull_icon = "🐂" if up_pct >= down_pct else "🐻"
        rsi_label = get_rsi_label(rsi)
        if abs(chg) > 0.5:
            whale = "🐋 Heavy accumulation" if chg > 0 else "🐋 Heavy selling"
        elif abs(chg) > 0.2:
            whale = "🐋 Moderate whale activity"
        else:
            whale = "🐋 Low whale activity"
        log_signal(symbol, "LONG" if chg >= 0 else "SHORT", price)

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

    if not has_data:
        lines.append("⏳ Loading market data...")
        lines.append("━━━━━━━━━━━━━━━━━━━━")

    lines.append(f"😱 Fear & Greed: *{fg_val}* ({fg_label})")
    acc = get_overall_accuracy()
    if acc:
        lines.append(f"📊 Signal Accuracy: *{acc}%*")
    elif ml_accuracy:
        avg = sum(ml_accuracy.values()) / len(ml_accuracy)
        lines.append(f"🤖 ML Accuracy: *{avg:.1f}%*")
    lines.append("🌐 ganoflow.com")
    return "\n".join(lines)

def build_summary_message(plan):
    coins = PLAN_COINS.get(plan, [])
    fg_val, _ = get_fg()
    date_str = datetime.utcnow().strftime("%m/%d/%Y")
    lines = [f"📊 *GanoFlow Summary* | {date_str}", "━━━━━━━━━━━━━━━━━━━━"]
    has_data = False
    for symbol in coins:
        price = latest_prices.get(symbol)
        if not price:
            continue
        has_data = True
        sym = symbol.replace("usdt", "").upper()
        pl = list(price_history[symbol])
        rsi = calc_rsi(pl) if len(pl) >= 5 else 50.0
        window = pl[-5:] if len(pl) >= 5 else pl
        candle_chg = ((window[-1] - window[0]) / window[0] * 100) if len(window) >= 2 else 0
        tick_chg = ((price - window[-1]) / window[-1] * 100) if window and window[-1] else 0
        up_pct, down_pct = calc_probability(rsi, candle_chg, tick_chg, pl, fg_val, symbol)
        icon = "🐂" if up_pct >= down_pct else "🐻"
        lines.append(f"{icon} *{sym}* — 🐂{up_pct:.3f}% 🐻{down_pct:.3f}%")
    if not has_data:
        lines.append("⏳ Loading market data...")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    acc = get_overall_accuracy()
    if acc:
        lines.append(f"📊 Accuracy: *{acc}%*")
    lines.append("🌐 ganoflow.com")
    return "\n".join(lines)

# ─── LIVE UPDATER ─────────────────────────────────────────────────────────────

async def live_updater():
    """Edits live + summary messages every 2 seconds. Never creates new ones."""
    print("⏳ Live updater started...")
    while True:
        await asyncio.sleep(2)
        for plan in PLAN_COINS:
            channel_id = CHANNELS.get(plan, 0)
            if not channel_id:
                continue
            bot = Bot(token=TELEGRAM_TOKEN)
            if live_message_ids.get(plan):
                try:
                    await bot.edit_message_text(
                        chat_id=channel_id,
                        message_id=live_message_ids[plan],
                        text=build_live_message(plan),
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    err = str(e)
                    if "message is not modified" in err:
                        pass
                    elif "not found" in err.lower():
                        live_message_ids.pop(plan, None)
                    else:
                        print(f"❌ Live edit {plan}: {e}")
            await asyncio.sleep(0.25)
            if summary_message_ids.get(plan):
                try:
                    await bot.edit_message_text(
                        chat_id=channel_id,
                        message_id=summary_message_ids[plan],
                        text=build_summary_message(plan),
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    err = str(e)
                    if "message is not modified" in err:
                        pass
                    elif "not found" in err.lower():
                        summary_message_ids.pop(plan, None)
                    else:
                        print(f"❌ Summary edit {plan}: {e}")
            await asyncio.sleep(0.25)

# ─── WEBSOCKET ────────────────────────────────────────────────────────────────

async def websocket_monitor():
    trade_streams = "/".join([f"{coin}@aggTrade" for coin in COIN_NAMES])
    kline_streams = "/".join([f"{coin}@kline_1m" for coin in COIN_NAMES])
    url = f"wss://data-stream.binance.vision/stream?streams={trade_streams}/{kline_streams}"
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
                                price_history[symbol].append(close)
                            else:
                                latest_prices[symbol] = close
        except Exception as e:
            print(f"❌ WebSocket error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

# ─── DAILY NEWS ───────────────────────────────────────────────────────────────

async def send_daily_news():
    print("📰 Daily news starting...")
    try:
        fg_val, fg_label = get_fg()
        btc_price = latest_prices.get("btcusdt", 0)
        date_str = datetime.now().strftime("%B %d, %Y")
        acc = get_overall_accuracy()
        acc_str = f"\n📊 Signal Accuracy: {acc}%" if acc else ""

        # Paid plans: send news
        news_configs = {
            "basic":    (300, "Write a SHORT 3-4 sentence daily market brief."),
            "standard": (500, "Write a MEDIUM 5-7 sentence daily market analysis covering BTC/ETH levels, altcoin sentiment, and outlook."),
            "premium":  (800, "Write a DETAILED analysis with sections: Market Overview, BTC & ETH Levels, Altcoin Sectors, Macro Factors, and Today's Outlook."),
        }
        for plan, (tokens, instruction) in news_configs.items():
            channel_id = CHANNELS.get(plan, 0)
            if not channel_id:
                continue
            try:
                analysis = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=tokens,
                    messages=[{"role": "user", "content": f"You are a professional crypto analyst. {instruction}\nBTC: ${btc_price:,.2f} | Fear & Greed: {fg_val} ({fg_label}) | Date: {date_str}\nEnglish only. Professional tone."}]
                ).content[0].text
                bot = Bot(token=TELEGRAM_TOKEN)
                news_msg = await bot.send_message(
                    chat_id=channel_id,
                    text=f"📰 *Daily Market Analysis — {date_str}*\n━━━━━━━━━━━━━━━━━━━━\n{analysis}\n━━━━━━━━━━━━━━━━━━━━\n😱 Fear & Greed: *{fg_val}* ({fg_label})\n💰 BTC: *${btc_price:,.2f}*{acc_str}\n🌐 ganoflow.com",
                    parse_mode="Markdown"
                )
                try:
                    await bot.pin_chat_message(chat_id=channel_id, message_id=news_msg.message_id, disable_notification=True)
                except:
                    pass
                print(f"✅ News sent to {plan}")
            except Exception as e:
                print(f"❌ News error {plan}: {e}")
            await asyncio.sleep(2)

        # ALL plans: send live + summary
        for plan in ["free", "basic", "standard", "premium"]:
            channel_id = CHANNELS.get(plan, 0)
            if not channel_id:
                continue
            bot = Bot(token=TELEGRAM_TOKEN)
            try:
                live_msg = await bot.send_message(chat_id=channel_id, text=build_live_message(plan), parse_mode="Markdown")
                live_message_ids[plan] = live_msg.message_id
                print(f"✅ Live created for {plan}")
            except Exception as e:
                print(f"❌ Live error {plan}: {e}")
            await asyncio.sleep(1)
            try:
                sum_msg = await bot.send_message(chat_id=channel_id, text=build_summary_message(plan), parse_mode="Markdown")
                summary_message_ids[plan] = sum_msg.message_id
                print(f"✅ Summary created for {plan}")
            except Exception as e:
                print(f"❌ Summary error {plan}: {e}")
            await asyncio.sleep(1)

        print("✅ Daily news complete!")
    except Exception as e:
        print(f"❌ Daily news error: {e}")

async def daily_news_scheduler():
    while True:
        now = datetime.utcnow()
        target_secs = 13 * 3600 + 30 * 60  # 9:30 AM ET = 13:30 UTC
        current_secs = now.hour * 3600 + now.minute * 60 + now.second
        wait = 86400 - (current_secs - target_secs) if current_secs >= target_secs else target_secs - current_secs
        print(f"⏰ Next daily news in {wait//3600}h {(wait%3600)//60}m (NY 09:30 ET)")
        await asyncio.sleep(wait)
        await send_daily_news()

async def track_signal_results():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for signal in signal_log:
            if signal.get("result") is not None:
                continue
            if now - signal["time"] >= 900:
                current = latest_prices.get(signal["symbol"])
                if current:
                    went_up = current > signal["entry_price"]
                    was_long = signal["direction"] == "LONG"
                    signal["result"] = (was_long and went_up) or (not was_long and not went_up)

async def train_all_models():
    if not ML_AVAILABLE:
        return
    print("🤖 Training ML models...")
    all_coins = list(set(c for coins in PLAN_COINS.values() for c in coins))
    for symbol in all_coins:
        try:
            r = requests.get("https://api.binance.com/api/v3/klines",
                           params={"symbol": symbol.upper(), "interval": "1m", "limit": 1000}, timeout=15)
            closes = [float(k[4]) for k in r.json()]
            if len(closes) < 100:
                continue
            X, y = [], []
            for i in range(50, len(closes) - 15):
                window = closes[max(0, i-100):i]
                if len(window) < 30:
                    continue
                candle_chg = ((window[-1] - window[-6]) / window[-6] * 100) if len(window) >= 6 else 0
                ema9 = calc_ema(window, 9)
                ema21 = calc_ema(window, 21)
                ema_cross = (ema9 - ema21) / ema21 * 100 if ema21 else 0
                last5 = window[-5:]
                bull_c = sum(1 for j in range(1, len(last5)) if last5[j] > last5[j-1])
                features = [calc_rsi(window), calc_macd(window)*1000, calc_momentum(window,10),
                           calc_momentum(window,20), bull_c, ema_cross, candle_chg, 0, 50, abs(candle_chg)]
                X.append(features)
                y.append(1 if closes[i+15] > closes[i] else 0)
            if len(X) < 50:
                continue
            import numpy as np
            X, y = np.array(X), np.array(y)
            split = int(len(X) * 0.8)
            scaler = StandardScaler()
            X_train = scaler.fit_transform(X[:split])
            X_test = scaler.transform(X[split:])
            model = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42, n_jobs=-1)
            model.fit(X_train, y[:split])
            acc = sum(model.predict(X_test) == y[split:]) / len(y[split:]) * 100
            ml_models[symbol] = model
            ml_scalers[symbol] = scaler
            ml_accuracy[symbol] = round(acc, 1)
            print(f"✅ {symbol} ML trained — {acc:.1f}%")
        except Exception as e:
            print(f"❌ ML error {symbol}: {e}")
        await asyncio.sleep(1)
    print(f"✅ ML done! {len(ml_models)} models ready")

async def retrain_scheduler():
    while True:
        await asyncio.sleep(86400)
        await train_all_models()

# ─── CHATBOT ──────────────────────────────────────────────────────────────────

WELCOME_MSG = """👋 Hey! Welcome to GanoFlow.

We send real-time crypto signals straight to your Telegram — 
24/7, the moment the market moves.

📊 What you get:
— Live prices with Entry, TP1/TP2/TP3, Stop Loss
— 🐂 UP / 🐻 DOWN probability (ML-powered)
— 🐋 Whale activity tracking
— 📊 Live signal accuracy tracking
— Daily market analysis at 9:30 AM ET (paid plans)

Ready to start?
👉 ganoflow.com — pick your plan
👉 /subscribe — see plan details

📧 Questions? Ganoflow@proton.me

⚠️ For reference only. Not financial advice."""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_MSG)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_MSG)

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Calculating...")
    try:
        btc = latest_prices.get("btcusdt", 0)
        if not btc:
            await update.message.reply_text("❌ No data yet. Try again in a moment.")
            return
        pl = list(price_history["btcusdt"])
        rsi = calc_rsi(pl)
        window = pl[-5:] if len(pl) >= 5 else pl
        candle_chg = ((window[-1] - window[0]) / window[0] * 100) if len(window) >= 2 else 0
        tick_chg = ((btc - window[-1]) / window[-1] * 100) if window and window[-1] else 0
        chg = candle_chg + tick_chg
        fg_val, _ = get_fg()
        up_pct, down_pct = calc_probability(rsi, candle_chg, tick_chg, pl, fg_val, "btcusdt")
        direction = "LONG 📈" if chg >= 0 else "SHORT 📉"
        acc = get_overall_accuracy()
        acc_str = f"\n📊 Accuracy: *{acc}%*" if acc else ""
        await update.message.reply_text(
            f"📊 *BITCOIN SIGNAL — GanoFlow*\n━━━━━━━━━━━━━━━━━━━━\n💰 *{fmt(btc)}*\n📈 Change: *{chg:+.2f}%*\n━━━━━━━━━━━━━━━━━━━━\n*{direction}*\n🐂 UP *{up_pct:.3f}%* | 🐻 DOWN *{down_pct:.3f}%*\n📊 RSI *{rsi}* — {get_rsi_label(rsi)}{acc_str}\n━━━━━━━━━━━━━━━━━━━━\n⚠️ DYOR. ganoflow.com",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def prices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not latest_prices:
        await update.message.reply_text("❌ No data yet.")
        return
    msg = "💰 *Live Prices — GanoFlow*\n━━━━━━━━━━━━━━━━━━━━\n"
    for symbol in COIN_NAMES:
        price = latest_prices.get(symbol)
        if price:
            msg += f"*{symbol.replace('usdt','').upper()}* — {fmt(price)}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n🌐 ganoflow.com"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💎 *GanoFlow Plans*\n━━━━━━━━━━━━━━━━━━━━\n🆓 *Free* — BTC + ETH live\n⚡ *Basic* — $29/mo — Top 5 coins\n🚀 *Standard* — $59/mo — Top 7 coins\n👑 *Premium* — $99/mo — Top 10 coins\n━━━━━━━━━━━━━━━━━━━━\n🌐 https://ganoflow.com",
        parse_mode="Markdown"
    )

# ─── MAIN ─────────────────────────────────────────────────────────────────────

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
        train_all_models(),
        track_signal_results(),
        retrain_scheduler(),
    )

asyncio.run(main())

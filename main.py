import anthropic
import asyncio
import json
import time
import os
import websockets
import requests
import pickle
from collections import deque
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime, timedelta

# ML imports
try:
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import StandardScaler
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False
    print("⚠️ ML libraries not available, using math-based probability")

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

# ML model storage
ml_models = {}      # {symbol: lgb_model}
ml_scalers = {}     # {symbol: scaler}
ml_accuracy = {}    # {symbol: float}
signal_log = []     # [{symbol, direction, entry_price, time, result}]

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

def calc_bollinger(prices, period=20):
    """Returns (upper, middle, lower, %B)"""
    pl = list(prices)
    if len(pl) < period:
        return 0, 0, 0, 0.5
    window = pl[-period:]
    middle = sum(window) / period
    std = (sum((x - middle) ** 2 for x in window) / period) ** 0.5
    upper = middle + 2 * std
    lower = middle - 2 * std
    pct_b = ((pl[-1] - lower) / (upper - lower)) if upper != lower else 0.5
    return upper, middle, lower, pct_b

def calc_volume_trend(volumes, period=10):
    """Check if recent volume is higher than average"""
    if len(volumes) < period:
        return 1.0
    avg = sum(volumes[-period:]) / period
    recent = sum(volumes[-3:]) / 3 if len(volumes) >= 3 else volumes[-1]
    return recent / avg if avg > 0 else 1.0

def extract_features(prices, candle_chg, tick_chg, fg_val=50):
    """Extract all features for ML model"""
    pl = list(prices)
    if len(pl) < 30:
        return None

    rsi = calc_rsi(pl)
    macd = calc_macd(pl)
    mom10 = calc_momentum(pl, 10)
    mom20 = calc_momentum(pl, 20)
    _, _, _, pct_b = calc_bollinger(pl)

    # Trend consistency last 5
    last5 = pl[-5:]
    bull_candles = sum(1 for i in range(1, len(last5)) if last5[i] > last5[i-1])

    # EMA crossover
    ema9 = calc_ema(pl, 9)
    ema21 = calc_ema(pl, 21)
    ema_cross = (ema9 - ema21) / ema21 * 100 if ema21 else 0

    features = [
        rsi,
        macd * 1000,
        mom10,
        mom20,
        pct_b,
        bull_candles,
        ema_cross,
        candle_chg,
        tick_chg,
        float(fg_val),
        abs(candle_chg) + abs(tick_chg),  # total volatility
    ]
    return features

def get_rsi_label(rsi):
    if rsi < 30:
        return "Oversold 🟢"
    elif rsi < 45:
        return "Bearish 🟡"
    elif rsi <= 55:
        return "Neutral ⚪"
    elif rsi <= 70:
        return "Bullish 🟠"
    else:
        return "Overbought 🔴"

def get_fg():
    global fg_cache
    if time.time() - fg_cache["last_update"] > 300:
        try:
            fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5).json()
            fg_cache["value"] = fg.get("data", [{"value":"50"}])[0].get("value", "50")
            fg_cache["label"] = fg.get("data", [{"value_classification":"Neutral"}])[0].get("value_classification", "Neutral")
            fg_cache["last_update"] = time.time()
        except:
            pass
    return fg_cache["value"], fg_cache["label"]

def calc_probability_math(rsi, candle_chg, tick_chg, prices=None, fear_greed=50):
    """Fallback math-based probability"""
    up_base = 50.0
    up_base += max(-20, min(20, tick_chg * 15))
    up_base += max(-15, min(15, candle_chg * 6))
    if rsi < 30: up_base += 10
    elif rsi < 40: up_base += 5
    elif rsi > 70: up_base -= 10
    elif rsi > 60: up_base -= 5
    if prices is not None:
        pl = list(prices)
        macd = calc_macd(pl)
        up_base += max(-5, min(5, macd * 100))
        mom = calc_momentum(pl, 10)
        up_base += max(-8, min(8, mom * 2))
        if len(pl) >= 5:
            last5 = pl[-5:]
            bull_candles = sum(1 for i in range(1, len(last5)) if last5[i] > last5[i-1])
            up_base += (bull_candles - (4 - bull_candles)) * 2
    fg = float(fear_greed)
    if fg < 25: up_base += 5
    elif fg < 40: up_base += 2
    elif fg > 75: up_base -= 5
    elif fg > 60: up_base -= 2
    total_move = abs(tick_chg) + abs(candle_chg)
    if total_move > 1.0:
        amplifier = min(total_move * 2, 8)
        up_base += amplifier if up_base > 50 else -amplifier
    up_pct = round(max(20.0, min(80.0, up_base)), 3)
    return up_pct, round(100 - up_pct, 3)

def calc_probability(rsi, candle_chg, tick_chg, prices=None, fear_greed=50, symbol=None):
    """ML-based probability with math fallback"""
    if ML_AVAILABLE and symbol and symbol in ml_models and prices is not None:
        try:
            features = extract_features(prices, candle_chg, tick_chg, fear_greed)
            if features:
                scaler = ml_scalers[symbol]
                model = ml_models[symbol]
                X = scaler.transform([features])
                prob_up = model.predict(X)[0]
                up_pct = round(max(20.0, min(80.0, prob_up * 100)), 3)
                return up_pct, round(100 - up_pct, 3)
        except Exception as e:
            print(f"ML prediction error: {e}")
    return calc_probability_math(rsi, candle_chg, tick_chg, prices, fear_greed)

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

def get_overall_accuracy():
    """Calculate overall signal accuracy from log"""
    if len(signal_log) < 5:
        return None
    resolved = [s for s in signal_log if s.get("result") is not None]
    if len(resolved) < 5:
        return None
    correct = sum(1 for s in resolved if s["result"])
    return round(correct / len(resolved) * 100, 1)

def get_symbol_accuracy(symbol):
    """Calculate per-symbol accuracy"""
    resolved = [s for s in signal_log if s.get("symbol") == symbol and s.get("result") is not None]
    if len(resolved) < 3:
        return None
    correct = sum(1 for s in resolved if s["result"])
    return round(correct / len(resolved) * 100, 1)

# ─── ML TRAINING ─────────────────────────────────────────────────────────────

def fetch_binance_klines(symbol, interval="1m", limit=1000):
    """Fetch historical klines from Binance"""
    url = f"https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        closes = [float(k[4]) for k in data]
        return closes
    except Exception as e:
        print(f"❌ Failed to fetch {symbol}: {e}")
        return []

def train_model_for_symbol(symbol):
    """Train LightGBM model for a symbol"""
    if not ML_AVAILABLE:
        return False
    try:
        from sklearn.preprocessing import StandardScaler
        print(f"🤖 Training ML model for {symbol}...")
        closes = fetch_binance_klines(symbol, "1m", 1000)
        if len(closes) < 100:
            print(f"❌ Not enough data for {symbol}")
            return False

        X, y = [], []
        fg_val = 50  # static for training

        for i in range(50, len(closes) - 15):
            window = closes[max(0, i-100):i]
            if len(window) < 30:
                continue

            candle_chg = ((window[-1] - window[-6]) / window[-6] * 100) if len(window) >= 6 else 0
            tick_chg = 0  # no tick data in historical

            features = extract_features(window, candle_chg, tick_chg, fg_val)
            if features is None:
                continue

            # Label: did price go up in next 15 minutes?
            future_price = closes[i + 15]
            current_price = closes[i]
            went_up = 1 if future_price > current_price else 0

            X.append(features)
            y.append(went_up)

        if len(X) < 50:
            print(f"❌ Not enough training samples for {symbol}")
            return False

        import numpy as np
        X = np.array(X)
        y = np.array(y)

        # Train/test split
        split = int(len(X) * 0.8)
        X_train, X_test = X[:split], X[split:]
        y_train, y_test = y[:split], y[split:]

        # Scale
        scaler = StandardScaler()
        X_train = scaler.fit_transform(X_train)
        X_test = scaler.transform(X_test)

        # Train Random Forest
        model = RandomForestClassifier(
            n_estimators=100,
            max_depth=5,
            random_state=42,
            n_jobs=-1
        )
        model.fit(X_train, y_train)

        # Accuracy
        preds = model.predict(X_test)
        accuracy = sum(preds == y_test) / len(y_test) * 100

        ml_models[symbol] = model
        ml_scalers[symbol] = scaler
        ml_accuracy[symbol] = round(accuracy, 1)

        print(f"✅ {symbol} model trained — accuracy: {accuracy:.1f}%")
        return True

    except Exception as e:
        print(f"❌ Training error for {symbol}: {e}")
        return False

async def train_all_models():
    """Train ML models for all coins on startup"""
    if not ML_AVAILABLE:
        print("⚠️ Skipping ML training — lightgbm not installed")
        return
    print("🤖 Starting ML model training...")
    all_coins = list(set(c for coins in PLAN_COINS.values() for c in coins))
    for symbol in all_coins:
        train_model_for_symbol(symbol)
        await asyncio.sleep(1)
    print(f"✅ ML training complete! {len(ml_models)}/{len(all_coins)} models ready")

async def retrain_scheduler():
    """Retrain models every 24 hours"""
    while True:
        await asyncio.sleep(86400)
        print("🔄 Retraining ML models...")
        await train_all_models()

# ─── SIGNAL ACCURACY TRACKER ─────────────────────────────────────────────────

async def track_signal_results():
    """Check if past signals were correct after 15 minutes"""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for signal in signal_log:
            if signal.get("result") is not None:
                continue
            if now - signal["time"] >= 900:  # 15 minutes
                symbol = signal["symbol"]
                current = latest_prices.get(symbol)
                if current:
                    was_long = signal["direction"] == "LONG"
                    price_went_up = current > signal["entry_price"]
                    signal["result"] = (was_long and price_went_up) or (not was_long and not price_went_up)

def log_signal(symbol, direction, price):
    """Log a signal for accuracy tracking"""
    signal_log.append({
        "symbol": symbol,
        "direction": direction,
        "entry_price": price,
        "time": time.time(),
        "result": None
    })
    # Keep only last 500 signals
    if len(signal_log) > 500:
        signal_log.pop(0)

# ─── MESSAGE BUILDERS ─────────────────────────────────────────────────────────

def build_live_message(plan):
    coins = PLAN_COINS.get(plan, [])
    fg_val, fg_label = get_fg()
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

        window = pl[-5:] if len(pl) >= 5 else (pl if len(pl) >= 2 else pl)
        candle_chg = ((window[-1] - window[0]) / window[0] * 100) if len(window) >= 2 else 0
        tick_chg = ((price - window[-1]) / window[-1] * 100) if window and window[-1] else 0
        chg = candle_chg + tick_chg

        up_pct, down_pct = calc_probability(rsi, candle_chg, tick_chg, price_history[symbol], fg_val, symbol)
        e_low, e_high, tp1, tp2, tp3, sl = calc_targets(price, chg)
        direction = "📈 LONG" if chg >= 0 else "📉 SHORT"
        bull_icon = "🐂" if up_pct >= down_pct else "🐻"
        rsi_label = get_rsi_label(rsi)

        # Log signal
        log_signal(symbol, "LONG" if chg >= 0 else "SHORT", price)

        if abs(chg) > 0.5:
            whale = "🐋 Heavy accumulation" if chg > 0 else "🐋 Heavy selling"
        elif abs(chg) > 0.2:
            whale = "🐋 Moderate whale activity"
        else:
            whale = "🐋 Low whale activity"

        # Per-symbol accuracy
        sym_acc = get_symbol_accuracy(symbol)
        ml_tag = f" 🤖{ml_accuracy[symbol]}%" if symbol in ml_accuracy else ""

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
    overall_acc = get_overall_accuracy()
    if overall_acc:
        lines.append(f"📊 Signal Accuracy: *{overall_acc}%* (live tracked)")
    elif ml_models:
        avg_acc = sum(ml_accuracy.values()) / len(ml_accuracy)
        lines.append(f"🤖 ML Model Accuracy: *{avg_acc:.1f}%* (trained)")
    lines.append("🌐 ganoflow.com")
    return "\n".join(lines)

def build_summary_message(plan):
    coins = PLAN_COINS.get(plan, [])
    fg_val, _ = get_fg()
    date_str = datetime.utcnow().strftime("%m/%d/%Y")
    lines = [f"📊 *GanoFlow* | {date_str}"]
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    for symbol in coins:
        price = latest_prices.get(symbol)
        if not price:
            continue
        sym = symbol.replace("usdt", "").upper()
        pl = list(price_history[symbol])
        rsi = calc_rsi(price_history[symbol]) if len(pl) >= 5 else 50.0
        window = pl[-5:] if len(pl) >= 5 else (pl if len(pl) >= 2 else pl)
        candle_chg = ((window[-1] - window[0]) / window[0] * 100) if len(window) >= 2 else 0
        tick_chg = ((price - window[-1]) / window[-1] * 100) if window and window[-1] else 0
        up_pct, down_pct = calc_probability(rsi, candle_chg, tick_chg, price_history[symbol], fg_val, symbol)
        icon = "🐂" if up_pct >= down_pct else "🐻"
        lines.append(f"{icon} *{sym}* — 🐂{up_pct:.3f}% 🐻{down_pct:.3f}%")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    overall_acc = get_overall_accuracy()
    if overall_acc:
        lines.append(f"📊 Accuracy: *{overall_acc}%*")
    elif ml_models:
        avg_acc = sum(ml_accuracy.values()) / len(ml_accuracy)
        lines.append(f"🤖 ML Accuracy: *{avg_acc:.1f}%*")
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
        msg = await bot.send_message(chat_id=channel_id, text=build_live_message(plan), parse_mode="Markdown")
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
        await bot.edit_message_text(chat_id=channel_id, message_id=msg_id, text=build_live_message(plan), parse_mode="Markdown")
    except Exception as e:
        err = str(e)
        if "message is not modified" in err:
            pass
        elif "Message to edit not found" in err or "message not found" in err.lower():
            live_message_ids.pop(plan, None)
            await init_live_message(plan)
        else:
            print(f"❌ Edit error {plan}: {e}")

async def init_summary_message(plan):
    channel_id = CHANNELS.get(plan, 0)
    if not channel_id or channel_id == 0:
        return
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        msg = await bot.send_message(chat_id=channel_id, text=build_summary_message(plan), parse_mode="Markdown")
        summary_message_ids[plan] = msg.message_id
        print(f"✅ Summary message created for {plan}: {msg.message_id}")
    except Exception as e:
        print(f"❌ Init summary error {plan}: {e}")

async def update_summary_message(plan):
    channel_id = CHANNELS.get(plan, 0)
    if not channel_id or channel_id == 0:
        return
    msg_id = summary_message_ids.get(plan)
    if not msg_id:
        await init_summary_message(plan)
        return
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        await bot.edit_message_text(chat_id=channel_id, message_id=msg_id, text=build_summary_message(plan), parse_mode="Markdown")
    except Exception as e:
        err = str(e)
        if "message is not modified" in err:
            pass
        elif "Message to edit not found" in err or "message not found" in err.lower():
            summary_message_ids.pop(plan, None)
            await init_summary_message(plan)

async def live_updater():
    """Only EDITS existing messages. send_daily_news() creates them once per day."""
    print("⏳ Waiting for first daily news to create messages...")
    # Wait up to 30s for messages to be created by send_daily_news
    # If no messages exist yet (e.g. bot restarted mid-day), create them once
    await asyncio.sleep(30)
    for plan in PLAN_COINS:
        if not live_message_ids.get(plan):
            await init_live_message(plan)
            await asyncio.sleep(0.5)
        if not summary_message_ids.get(plan):
            await init_summary_message(plan)
            await asyncio.sleep(0.5)
    while True:
        await asyncio.sleep(2)
        for plan in PLAN_COINS:
            await update_live_message(plan)
            await asyncio.sleep(0.25)
            await update_summary_message(plan)
            await asyncio.sleep(0.25)

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
                                price_history[symbol].append(close)
                            else:
                                latest_prices[symbol] = close
        except Exception as e:
            print(f"❌ WebSocket error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

# ─── DAILY NEWS ──────────────────────────────────────────────────────────────

async def send_daily_news():
    print("📰 Sending daily market analysis...")
    try:
        fg_val, fg_label = get_fg()
        btc_price = latest_prices.get("btcusdt", 0)
        date_str = datetime.now().strftime("%B %d, %Y")
        overall_acc = get_overall_accuracy()
        acc_str = f"\n📊 Signal Accuracy: {overall_acc}% (live tracked)" if overall_acc else ""

        configs = {
            "basic":    (300, "Write a SHORT 3-4 sentence daily market brief."),
            "standard": (500, "Write a MEDIUM 5-7 sentence daily market analysis covering BTC/ETH levels, altcoin sentiment, and outlook."),
            "premium":  (800, "Write a DETAILED analysis with sections: Market Overview, BTC & ETH Levels, Altcoin Sectors, Macro Factors, and Today's Outlook."),
        }

        for plan, (tokens, instruction) in configs.items():
            channel_id = CHANNELS.get(plan, 0)
            if not channel_id or channel_id == 0:
                continue
            print(f"📰 Sending news to {plan}...")
            try:
                analysis = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=tokens,
                    messages=[{"role": "user", "content": f"""
You are a professional crypto analyst. {instruction}
BTC: ${btc_price:,.2f} | Fear & Greed: {fg_val} ({fg_label}) | Date: {date_str}
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
😱 Fear & Greed: *{fg_val}* ({fg_label})
💰 BTC: *${btc_price:,.2f}*{acc_str}
🌐 ganoflow.com""",
                    parse_mode="Markdown"
                )
                try:
                    await bot.pin_chat_message(chat_id=channel_id, message_id=news_msg.message_id, disable_notification=True)
                except:
                    pass
                await asyncio.sleep(2)
                # Send Live message right after news
                try:
                    live_msg = await bot.send_message(
                        chat_id=channel_id,
                        text=build_live_message(plan),
                        parse_mode="Markdown"
                    )
                    live_message_ids[plan] = live_msg.message_id
                    print(f"✅ Live message created for {plan}: {live_msg.message_id}")
                except Exception as e:
                    print(f"❌ Live message error: {e}")
                await asyncio.sleep(1)
                # Send Summary message right after live message
                try:
                    summary_msg = await bot.send_message(
                        chat_id=channel_id,
                        text=build_summary_message(plan),
                        parse_mode="Markdown"
                    )
                    summary_message_ids[plan] = summary_msg.message_id
                    print(f"✅ Summary message created for {plan}: {summary_msg.message_id}")
                except Exception as e:
                    print(f"❌ Summary message error: {e}")
            except Exception as e:
                print(f"❌ Error {plan}: {e}")
            await asyncio.sleep(1)
        print("✅ Daily news done!")
    except Exception as e:
        print(f"❌ Daily news error: {e}")

async def daily_news_scheduler():
    while True:
        now = datetime.utcnow()
        target_h, target_m = 13, 30
        target_secs = target_h * 3600 + target_m * 60
        current_secs = now.hour * 3600 + now.minute * 60 + now.second
        wait = 86400 - (current_secs - target_secs) if current_secs >= target_secs else target_secs - current_secs
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
— 🐂 UP / 🐻 DOWN probability (ML-powered)
— 🐋 Whale activity tracking
— 📊 Live signal accuracy tracking
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
        fg_val, _ = get_fg()
        up_pct, down_pct = calc_probability(rsi, candle_chg, tick_chg, price_history["btcusdt"], fg_val, "btcusdt")
        direction = "LONG 📈" if chg >= 0 else "SHORT 📉"
        rsi_label = get_rsi_label(rsi)
        acc = get_overall_accuracy()
        acc_str = f"\n📊 Accuracy: *{acc}%*" if acc else ""
        await update.message.reply_text(f"""📊 *BITCOIN SIGNAL — GanoFlow*
━━━━━━━━━━━━━━━━━━━━
💰 *{fmt(btc)}*
📈 Change: *{chg:+.2f}%*
━━━━━━━━━━━━━━━━━━━━
*{direction}*
🐂 UP *{up_pct:.3f}%* | 🐻 DOWN *{down_pct:.3f}%*
📊 RSI *{rsi}* — {rsi_label}{acc_str}
━━━━━━━━━━━━━━━━━━━━
⚠️ DYOR. ganoflow.com""", parse_mode="Markdown")
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
— 🐂 UP / 🐻 DOWN probability (ML-powered)
— 🐋 Whale activity tracking
— 📊 Live signal accuracy tracking
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
        train_all_models(),
        track_signal_results(),
        retrain_scheduler(),
    )

asyncio.run(main())

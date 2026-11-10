# main.py
from datetime import datetime, timedelta, timezone
import json
import statistics
import time

import config
import news_filter
import upstox_client
from upstox_client.models.place_order_request import PlaceOrderRequest

# Live order placement requires a static public IP to be whitelisted in the Upstox app.
# Keep this off until your broker account is configured for the machine/server running this bot.
AUTO_TRADE_ENABLED = True
NIFTY_50_SYMBOLS = [
    "TCS", "RELIANCE", "ICICIBANK", "HDFCBANK", "INFY", "LTIM", "SBIN", "ITC", "BHARTIARTL",
    "KOTAKBANK", "TATAMOTORS", "HINDUNILVR", "AXISBANK", "SUNPHARMA", "ASIANPAINT", "WIPRO",
    "BAJFINANCE", "TITAN", "NTPC", "POWERGRID", "TECHM", "JSWSTEEL", "HCLTECH", "ULTRACEMCO",
    "MARUTI", "INDUSINDBK", "TATASTEEL", "ONGC", "CIPLA", "NESTLEIND", "BAJAJ-AUTO", "GRASIM",
    "M&M", "HEROMOTOCO", "SHRIRAMFIN", "ADANIGREEN", "EICHERMOT", "COALINDIA", "APOLLOHOSP",
    "TATACONSUM", "DIVISLAB", "DRREDDY", "LT", "HDFCLIFE", "BAJAJFINSV", "BRITANNIA", "IOC",
    "ICICIPRULI", "BPCL", "GAIL", "BEL"
]
DEFAULT_SYMBOLS = NIFTY_50_SYMBOLS
TOP_GAINERS_SYMBOLS = ["TCS", "INFY", "RELIANCE", "HDFCBANK", "ICICIBANK", "LTIM", "WIPRO", "ITC", "SUNPHARMA", "JSWSTEEL"]
INTRADAY_RECOVERY_SYMBOLS = ["TCS", "SUNPHARMA", "JSWSTEEL", "BAJFINANCE", "HINDUNILVR", "ONGC", "SBIN", "DHANUKA", "BEL", "CIPLA"]
RISK_PERCENT = 0.75
TARGET_PERCENT = 5.5
MAX_POSITION_SIZE = 1
ACTIVE_TRADE = None
CONSERVATIVE_MODE = True
ENTRY_CONFIDENCE_THRESHOLD = 0.012
DYNAMIC_TARGET_MIN = 5.0
DYNAMIC_TARGET_MAX = 7.0
VOLUME_FILTER_MIN = 1.5
MULTI_TIMEFRAME_CONFIRMATION = True
TOP_MOVER_LIMIT = 15
STRONG_MOMENTUM_TARGET = 7.0
MOMENTUM_HOLD_THRESHOLD = 1.5
NEWS_SUPPORT_REQUIRED = True
MAX_TRADE_CAPITAL_PERCENT = 15.0
MIN_POSITION_QUANTITY = 1
MIN_REALIZED_PROFIT_TARGET = 3000.0
REALIZED_PROFIT = 0.0
TRADES_LOG_PATH = "trade_log.json"
STARTING_WALLET_BALANCE = 60000.0
BROKERAGE_RATE = 0.0003
EXCHANGE_TAX_RATE = 0.0001
SEBI_TAX_RATE = 0.00002
GST_RATE = 0.00018
TURNOVER_FEE_RATE = 0.00003


def get_ist_now():
    """Returns the current time in Indian Standard Time."""
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30)))


def is_market_open():
    """True only during regular NSE trading hours on weekdays."""
    now = get_ist_now()
    if now.weekday() >= 5:
        return False

    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


def initialize_upstox_session():
    """Validates your daily token and prepares the connection to Upstox."""
    if not config.ACCESS_TOKEN:
        print("❌ ERROR: Your config.py file is missing the ACCESS_TOKEN.")
        print("👉 Run 'python login.py' first to generate your token for today.")
        return None

    try:
        configuration = upstox_client.Configuration()
        configuration.access_token = config.ACCESS_TOKEN
        return upstox_client.ApiClient(configuration)
    except Exception as exc:
        print(f"❌ Failed to initialize Upstox session: {exc}")
        return None


def validate_market_data():
    """Quickly confirms the token can access Upstox market data and instrument metadata."""
    api_client = initialize_upstox_session()
    if not api_client:
        return

    try:
        api = upstox_client.InstrumentsApi(api_client)
        response = api.search_instrument("TCS", exchanges="NSE", instrument_types="EQ")
        print(response)
    except Exception as exc:
        print(f"⚠️ Market-data validation failed: {exc}")


def resolve_instrument(symbol, exchange="NSE", instrument_type="EQ"):
    """Looks up the exact Upstox instrument metadata for a trading symbol."""
    api_client = initialize_upstox_session()
    if not api_client:
        return None

    try:
        api = upstox_client.InstrumentsApi(api_client)
        response = api.search_instrument(symbol, exchanges=exchange, instrument_types=instrument_type)
        if not getattr(response, "data", None):
            print(f"❌ No instrument found for {symbol} on {exchange}.")
            return None
        return response.data[0]
    except Exception as exc:
        print(f"⚠️ Failed to resolve instrument {symbol}: {exc}")
        return None


def fetch_intraday_candles(symbol, interval="1minute", candle_count=35):
    """Fetches recent intraday candles for a symbol using the correct Upstox interval format."""
    instrument = resolve_instrument(symbol)
    if not instrument:
        return None

    api_client = initialize_upstox_session()
    if not api_client:
        return None

    try:
        history_api = upstox_client.HistoryApi(api_client)
        response = history_api.get_intra_day_candle_data(instrument["instrument_key"], interval, "2.0")
        candles = response.data.candles[-candle_count:]
        print(f"📈 {symbol} candles loaded: {len(candles)} bars")
        return {
            "symbol": symbol,
            "instrument_key": instrument["instrument_key"],
            "candles": candles,
        }
    except Exception as exc:
        print(f"⚠️ Failed to read candles for {symbol}: {exc}")
        return None


def average(values):
    return sum(values) / len(values) if values else 0.0


def signal_strength_score(last_close, short_ma, mid_ma, long_ma):
    """Measures conviction. Only trades when momentum is clearly stronger than noise."""
    if not long_ma:
        return 0.0

    trend_strength = abs(last_close - long_ma) / long_ma
    alignment_strength = abs(short_ma - long_ma) / long_ma + abs(mid_ma - long_ma) / long_ma
    return trend_strength + alignment_strength


def evaluate_symbol_signal(symbol):
    """Uses multi-timeframe momentum plus volume confirmation before allowing a trade."""
    minute_data = fetch_intraday_candles(symbol, interval="1minute", candle_count=35)
    if not minute_data:
        return None

    minute_closes = [float(candle[4]) for candle in minute_data["candles"] if len(candle) > 4]
    if len(minute_closes) < 20:
        print(f"⚠️ Insufficient candle data for {symbol}.")
        return None

    short_ma = average(minute_closes[-5:])
    mid_ma = average(minute_closes[-10:])
    long_ma = average(minute_closes[-20:])
    last_close = minute_closes[-1]
    volatility = statistics.pstdev(minute_closes[-10:])
    trend_gap = abs(last_close - long_ma) / long_ma if long_ma else 0.0
    strength = signal_strength_score(last_close, short_ma, mid_ma, long_ma)

    volume_factor = 1.0
    if len(minute_data["candles"]) > 0:
        minute_volumes = [float(candle[5]) for candle in minute_data["candles"] if len(candle) > 5]
        if minute_volumes:
            avg_volume = average(minute_volumes[-10:])
            recent_volume = minute_volumes[-1]
            volume_factor = recent_volume / avg_volume if avg_volume else 1.0

    if MULTI_TIMEFRAME_CONFIRMATION:
        thirty_min_data = fetch_intraday_candles(symbol, interval="30minute", candle_count=20)
        if thirty_min_data:
            thirty_closes = [float(candle[4]) for candle in thirty_min_data["candles"] if len(candle) > 4]
            if len(thirty_closes) >= 6:
                thirty_short = average(thirty_closes[-2:])
                thirty_long = average(thirty_closes[-6:])
                if last_close > short_ma > mid_ma > long_ma and thirty_short > thirty_long:
                    trend_confirmed = True
                elif last_close < short_ma < mid_ma < long_ma and thirty_short < thirty_long:
                    trend_confirmed = True
                else:
                    trend_confirmed = False
            else:
                trend_confirmed = True
        else:
            trend_confirmed = True
    else:
        trend_confirmed = True

    buy_condition = (
        last_close > short_ma > mid_ma > long_ma
        and last_close > long_ma * 1.01
        and trend_gap < 0.08
        and volatility > 0.002
        and strength >= ENTRY_CONFIDENCE_THRESHOLD
        and volume_factor >= VOLUME_FILTER_MIN
        and trend_confirmed
    )
    sell_condition = (
        last_close < short_ma < mid_ma < long_ma
        and last_close < long_ma * 0.99
        and trend_gap < 0.08
        and volatility > 0.002
        and strength >= ENTRY_CONFIDENCE_THRESHOLD
        and volume_factor >= VOLUME_FILTER_MIN
        and trend_confirmed
    )

    if buy_condition:
        signal = "BUY"
    elif sell_condition:
        signal = "SELL"
    else:
        signal = "HOLD"

    print(
        f"{symbol}: last_close={last_close:.2f}, short_ma={short_ma:.2f}, mid_ma={mid_ma:.2f}, long_ma={long_ma:.2f}, signal={signal}, strength={strength:.4f}, volatility={volatility:.4f}, volume_factor={volume_factor:.2f}"
    )

    if signal == "HOLD":
        return None

    return {
        "symbol": symbol,
        "instrument_key": minute_data["instrument_key"],
        "signal": signal,
        "last_close": last_close,
        "short_ma": short_ma,
        "long_ma": long_ma,
        "signal_strength": strength,
        "volume_factor": volume_factor,
    }


def fetch_live_market_movers(symbols=None, limit=TOP_MOVER_LIMIT):
    """Fetches a real-time sense of the strongest gainers/losers from Upstox quote data."""
    if not symbols:
        symbols = DEFAULT_SYMBOLS

    api_client = initialize_upstox_session()
    if not api_client:
        return []

    normalized_symbols = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    if not normalized_symbols:
        return []

    quote_api = upstox_client.MarketQuoteV3Api(api_client)
    try:
        instrument_keys = []
        for symbol in normalized_symbols[:100]:
            instrument = resolve_instrument(symbol)
            if instrument:
                instrument_key = instrument.get("instrument_key") or instrument.get("instrumentKey")
                if instrument_key:
                    instrument_keys.append(str(instrument_key))

        if not instrument_keys:
            return []

        response = quote_api.get_full_market_quote_v3(instrument_key=",".join(instrument_keys))
        payload = getattr(response, "data", {}) or {}
        if isinstance(payload, dict):
            quote_entries = []
            seen = set()

            for key, quote in payload.items():
                if not quote:
                    continue

                symbol = getattr(quote, "symbol", None) or str(key).split("-")[0].upper()
                last_price = getattr(quote, "last_price", None)
                prev_close = getattr(quote, "prev_close_price", None)
                volume = getattr(quote, "volume", 0) or 0
                if symbol in seen:
                    continue
                seen.add(symbol)

                if last_price is None or prev_close in (None, 0):
                    continue

                move_pct = ((float(last_price) - float(prev_close)) / float(prev_close)) * 100.0
                quote_entries.append({
                    "symbol": symbol,
                    "move_pct": move_pct,
                    "last_price": float(last_price),
                    "prev_close": float(prev_close),
                    "volume": float(volume),
                })

            quote_entries.sort(key=lambda item: abs(item["move_pct"]), reverse=True)
            live_movers = [entry["symbol"] for entry in quote_entries[:limit]]
            return live_movers
    except Exception as exc:
        print(f"⚠️ Live Upstox mover feed unavailable: {exc}")

    return []


def get_candidate_symbols():
    """Builds the universe of stocks to rank before choosing the strongest one."""
    live_movers = fetch_live_market_movers(DEFAULT_SYMBOLS, limit=TOP_MOVER_LIMIT)

    candidates = []
    if live_movers:
        candidates.extend(live_movers)

    candidates.extend(DEFAULT_SYMBOLS)
    for mover_group in (TOP_GAINERS_SYMBOLS, INTRADAY_RECOVERY_SYMBOLS):
        candidates.extend(mover_group)

    seen = set()
    ordered = []
    for symbol in candidates:
        if symbol not in seen:
            seen.add(symbol)
            ordered.append(symbol)
    return ordered


def rank_candidate_symbols():
    """Scores all candidate symbols and picks the strongest one by technical conviction."""
    ranked = []
    for symbol in get_candidate_symbols():
        signal_data = evaluate_symbol_signal(symbol)
        if not signal_data:
            continue

        score = float(signal_data.get("signal_strength", 0.0))
        volume_factor = float(signal_data.get("volume_factor", 1.0))
        if signal_data["signal"] == "BUY":
            score += 0.025
        elif signal_data["signal"] == "SELL":
            score += 0.02

        score += min(0.05, max(0.0, (volume_factor - 1.0) * 0.15))

        ranked.append(
            {
                "symbol": symbol,
                "signal": signal_data["signal"],
                "last_close": signal_data["last_close"],
                "strength": score,
                "instrument_key": signal_data["instrument_key"],
                "volume_factor": volume_factor,
            }
        )

    ranked.sort(key=lambda item: item["strength"], reverse=True)
    return ranked


def get_realized_profit_status():
    """Returns whether the minimum profit goal has already been reached on realized trades."""
    return float(REALIZED_PROFIT)


def get_trade_log():
    """Loads the persistent trade journal from disk."""
    try:
        with open(TRADES_LOG_PATH, "r", encoding="utf-8") as file:
            entries = json.load(file)
        return entries if isinstance(entries, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        return []


def save_trade_log(entries):
    """Writes the trade journal to disk."""
    with open(TRADES_LOG_PATH, "w", encoding="utf-8") as file:
        json.dump(entries, file, indent=2)


def calculate_entry_exit_costs(transaction_type, quantity, price):
    """Calculates an estimated fees-and-tax cost for a stock side using realistic market charges."""
    notional = float(quantity) * float(price)
    if transaction_type == "BUY":
        return notional * (BROKERAGE_RATE + GST_RATE + TURNOVER_FEE_RATE)
    if transaction_type == "SELL":
        return notional * (BROKERAGE_RATE + EXCHANGE_TAX_RATE + SEBI_TAX_RATE + GST_RATE + TURNOVER_FEE_RATE)
    return 0.0


def update_realized_profit_for_close(symbol, side, entry_price, exit_price, quantity):
    """Tracks realized profit and updates the daily summary with explicit fees and taxes."""
    global REALIZED_PROFIT
    if entry_price is None or exit_price is None or quantity <= 0:
        return

    if side == "BUY":
        pnl_before_fees = (float(exit_price) - float(entry_price)) * int(quantity)
    else:
        pnl_before_fees = (float(entry_price) - float(exit_price)) * int(quantity)

    entry_cost = calculate_entry_exit_costs("BUY", quantity, float(entry_price))
    exit_cost = calculate_entry_exit_costs("SELL", quantity, float(exit_price))
    net_pnl = pnl_before_fees - entry_cost - exit_cost
    REALIZED_PROFIT += net_pnl
    print(f"💹 Realized P&L update for {symbol}: ₹{net_pnl:.2f} | total realized: ₹{REALIZED_PROFIT:.2f}")

    entries = get_trade_log()
    matched = False
    for item in entries:
        if item.get("symbol") == symbol and item.get("status") == "open":
            item["status"] = "closed"
            item["exit_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            item["exit_price"] = float(exit_price)
            item["pnl_before_fees"] = float(pnl_before_fees)
            item["fees_and_tax"] = float(entry_cost + exit_cost)
            item["net_pnl"] = float(net_pnl)
            item["wallet_balance_after_trade"] = float(STARTING_WALLET_BALANCE + REALIZED_PROFIT)
            matched = True
            break

    if not matched:
        entries.append({
            "symbol": symbol,
            "side": side,
            "quantity": int(quantity),
            "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "exit_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "pnl_before_fees": float(pnl_before_fees),
            "fees_and_tax": float(entry_cost + exit_cost),
            "net_pnl": float(net_pnl),
            "status": "closed",
            "wallet_balance_after_trade": float(STARTING_WALLET_BALANCE + REALIZED_PROFIT),
        })

    save_trade_log(entries)


def log_trade_event(trade_record):
    """Persists a trade entry to the JSON log and visible dashboard."""
    entries = get_trade_log()
    entries.append(trade_record)
    save_trade_log(entries)


def get_day_trade_summary():
    """Returns the overall P&L, cost totals, and wallet balance from the first trade of the day."""
    trades = get_trade_log()
    total_profit = sum(float(item.get("net_pnl", 0.0)) for item in trades if item.get("status") == "closed")
    total_fees = sum(float(item.get("fees_and_tax", 0.0)) for item in trades if item.get("status") == "closed")
    trade_count = len([item for item in trades if item.get("status") == "closed"])
    balance = STARTING_WALLET_BALANCE + total_profit
    return {
        "trade_count": trade_count,
        "total_profit_loss": float(total_profit),
        "total_fees_tax": float(total_fees),
        "wallet_balance": float(balance),
        "trades": trades,
    }


def calculate_risk_levels(signal, last_close, signal_strength=0.0, volume_factor=1.0, news_support=True):
    """Treats ₹3000 as a floor, not a permanent lock. Once reached, later trades can continue with a relaxed target."""
    global REALIZED_PROFIT

    goal_met = REALIZED_PROFIT >= MIN_REALIZED_PROFIT_TARGET
    if signal == "BUY":
        stop_loss = last_close * (1 - (RISK_PERCENT / 100.0))
        base_target = TARGET_PERCENT if not goal_met else 3.5
        if signal_strength >= 0.03 and volume_factor >= 2.0 and news_support:
            base_target = STRONG_MOMENTUM_TARGET
        dynamic_target_ratio = max(DYNAMIC_TARGET_MIN, min(DYNAMIC_TARGET_MAX, base_target))
        if goal_met and signal_strength < 0.03:
            dynamic_target_ratio = 3.5
        target = last_close * (1 + (dynamic_target_ratio / 100.0))
    elif signal == "SELL":
        stop_loss = last_close * (1 + (RISK_PERCENT / 100.0))
        base_target = TARGET_PERCENT if not goal_met else 3.5
        if signal_strength >= 0.03 and volume_factor >= 2.0 and news_support:
            base_target = STRONG_MOMENTUM_TARGET
        dynamic_target_ratio = max(DYNAMIC_TARGET_MIN, min(DYNAMIC_TARGET_MAX, base_target))
        if goal_met and signal_strength < 0.03:
            dynamic_target_ratio = 3.5
        target = last_close * (1 - (dynamic_target_ratio / 100.0))
    else:
        stop_loss = None
        target = None

    return stop_loss, target


def get_available_trade_budget():
    """Returns the usable buying power from the Upstox wallet margin."""
    api_client = initialize_upstox_session()
    if not api_client:
        return 0.0

    try:
        user_api = upstox_client.UserApi(api_client)
        response = user_api.get_user_fund_margin_v3()
        data = getattr(response, "data", None)
        available = getattr(data, "available_to_trade", None)

        if isinstance(available, dict):
            total = available.get("total", 0.0)
            return float(total)
        if available is not None:
            total = getattr(available, "total", 0.0)
            return float(total)

        if isinstance(data, dict):
            available = data.get("available_to_trade", {})
            if isinstance(available, dict):
                return float(available.get("total", 0.0))

        return 0.0
    except Exception as exc:
        print(f"⚠️ Failed to read wallet margin: {exc}")
        return 0.0


def calculate_trade_quantity(symbol, signal, last_close, available_budget=None):
    """Sizes the order using available balance instead of a fixed qty of 1."""
    if last_close is None or last_close <= 0:
        return MIN_POSITION_QUANTITY

    if available_budget is None:
        available_budget = get_available_trade_budget()

    usable_budget = available_budget * (MAX_TRADE_CAPITAL_PERCENT / 100.0)
    if usable_budget <= 0:
        return MIN_POSITION_QUANTITY

    raw_quantity = usable_budget / float(last_close)
    quantity = int(raw_quantity)
    if quantity < MIN_POSITION_QUANTITY:
        quantity = MIN_POSITION_QUANTITY
    return quantity


def get_live_price(symbol):
    """Returns the most recent near-real-time close for a symbol."""
    minute_data = fetch_intraday_candles(symbol, interval="1minute", candle_count=5)
    if not minute_data or not minute_data.get("candles"):
        return None

    candles = minute_data["candles"]
    closes = [float(candle[4]) for candle in candles if len(candle) > 4]
    if not closes:
        return None
    return closes[-1]


def close_active_trade():
    """Closes an existing trade using the opposite side and the recorded quantity."""
    global ACTIVE_TRADE
    if ACTIVE_TRADE is None:
        return

    trade = ACTIVE_TRADE
    symbol = trade["symbol"]
    side = trade["side"]
    quantity = int(trade.get("quantity", 1))
    entry_price = float(trade.get("entry_price", 0.0))
    exit_side = "SELL" if side == "BUY" else "BUY"
    print(f"🚪 Closing active {side} trade in {symbol} with quantity {quantity}.")

    result = place_trade_order(symbol, transaction_type=exit_side, quantity=quantity)
    if result is not None:
        current_price = get_live_price(symbol)
        if current_price is not None:
            update_realized_profit_for_close(symbol, side, entry_price, current_price, quantity)
        ACTIVE_TRADE = None
        print(f"✅ Active trade closed for {symbol}.")
    else:
        print(f"⚠️ Exit order for {symbol} failed; trade remains active.")


def monitor_active_trade():
    """Checks if the current trade hit stop loss or target and exits if it did."""
    global ACTIVE_TRADE
    if ACTIVE_TRADE is None:
        return

    trade = ACTIVE_TRADE
    symbol = trade["symbol"]
    current_price = get_live_price(symbol)
    if current_price is None:
        return

    if trade["side"] == "BUY":
        if current_price <= trade["stop_loss"] or current_price >= trade["target"]:
            close_active_trade()
    else:
        if current_price >= trade["stop_loss"] or current_price <= trade["target"]:
            close_active_trade()


def build_order_request(instrument_key, transaction_type, quantity=1, price=None, trigger_price=0.0, order_type="MARKET"):
    """Creates a trade request object for a future order placement call."""
    return PlaceOrderRequest(
        quantity=quantity,
        product="I",
        validity="DAY",
        price=float(price) if price is not None else 0.0,
        instrument_token=instrument_key,
        order_type=order_type,
        transaction_type=transaction_type,
        disclosed_quantity=0,
        trigger_price=float(trigger_price) if trigger_price else 0.0,
        is_amo=False,
        tag="algo-bot",
    )


def place_trade_order(symbol, transaction_type="BUY", quantity=1, price=None, stop_loss=None, target=None):
    """Prepares and optionally sends an order when AUTO_TRADE_ENABLED is set to True."""
    instrument = resolve_instrument(symbol)
    if not instrument:
        return None

    orders = []
    primary_order = build_order_request(
        instrument["instrument_key"],
        transaction_type=transaction_type,
        quantity=min(quantity, MAX_POSITION_SIZE),
        price=price,
        order_type="MARKET" if price is None else "LIMIT",
    )
    orders.append(primary_order)

    if stop_loss is not None:
        opposite = "SELL" if transaction_type == "BUY" else "BUY"
        sl_request = build_order_request(
            instrument["instrument_key"],
            transaction_type=opposite,
            quantity=min(quantity, MAX_POSITION_SIZE),
            price=stop_loss,
            trigger_price=stop_loss,
            order_type="SL",
        )
        orders.append(sl_request)

    if target is not None:
        opposite = "SELL" if transaction_type == "BUY" else "BUY"
        target_request = build_order_request(
            instrument["instrument_key"],
            transaction_type=opposite,
            quantity=min(quantity, MAX_POSITION_SIZE),
            price=target,
            trigger_price=target,
            order_type="SL",
        )
        orders.append(target_request)

    if not AUTO_TRADE_ENABLED:
        print(f"🛑 AUTO_TRADE is disabled. Prepared {transaction_type} request for {symbol}, but NO order was sent.")
        for i, order in enumerate(orders, 1):
            print(f"Order {i}: {order.to_dict()}")
        return orders

    api_client = initialize_upstox_session()
    if not api_client:
        return None

    order_api = upstox_client.OrderApi(api_client)
    # Conservative execution: use a single position and one active trade at a time.
    for order in orders:
        try:
            response = order_api.place_order(order, "2.0")
            print(f"✅ Order placed for {symbol}: {response}")
        except Exception as exc:
            error_text = str(exc).lower()
            if "udapi1154" in error_text or "static ip" in error_text or "no static ip" in error_text:
                print("🚫 Upstox live order is blocked because this IP is not whitelisted.")
                print("👉 Add a static public IP in the Upstox developer app, or run the bot from a server with a fixed IP.")
                print("👉 Until then, keep AUTO_TRADE_ENABLED = False.")
            else:
                print(f"❌ Order placement failed for {symbol}: {exc}")
            return None
    return orders


def run_symbol_scan():
    """Scans a ranked universe of candidate stocks and picks only the strongest trade."""
    global ACTIVE_TRADE

    if ACTIVE_TRADE is not None:
        print(f"🛑 Active trade already exists for {ACTIVE_TRADE['symbol']} ({ACTIVE_TRADE['side']}). Skipping duplicate signal.")
        return

    if not is_market_open():
        print("⏰ Market is closed. Bot will wait for normal trading hours.")
        return

    ranked_candidates = rank_candidate_symbols()
    if not ranked_candidates:
        print("ℹ️ No strong BUY/SELL signal found in the current scan.")
        return

    best_candidate = ranked_candidates[0]
    symbol = best_candidate["symbol"]
    signal = best_candidate["signal"]
    print(f"🏆 Best candidate selected: {symbol} with signal={signal} and strength={best_candidate['strength']:.4f}")

    available_budget = get_available_trade_budget()
    quantity = calculate_trade_quantity(symbol, signal, best_candidate["last_close"], available_budget)
    print(f"💰 Available trade budget: ₹{available_budget:.2f} | computed quantity: {quantity}")

    if signal == "BUY":
        news_support = news_filter.scan_live_market_news() != "PAUSE_TRADING"
        stop_loss, target = calculate_risk_levels(
            "BUY",
            best_candidate["last_close"],
            best_candidate["strength"],
            volume_factor=best_candidate.get("volume_factor", 1.0),
            news_support=news_support,
        )
        result = place_trade_order(
            symbol,
            transaction_type="BUY",
            quantity=quantity,
            stop_loss=stop_loss,
            target=target,
        )
        if result is not None:
            ACTIVE_TRADE = {
                "symbol": symbol,
                "side": "BUY",
                "quantity": quantity,
                "entry_price": best_candidate["last_close"],
                "stop_loss": stop_loss,
                "target": target,
                "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "open",
            }
            log_trade_event({
                "symbol": symbol,
                "side": "BUY",
                "quantity": quantity,
                "entry_time": ACTIVE_TRADE["entry_time"],
                "entry_price": best_candidate["last_close"],
                "exit_time": None,
                "exit_price": None,
                "status": "open",
                "pnl_before_fees": 0.0,
                "fees_and_tax": 0.0,
                "net_pnl": 0.0,
                "wallet_balance_after_trade": STARTING_WALLET_BALANCE,
            })
        return

    if signal == "SELL":
        news_support = news_filter.scan_live_market_news() != "PAUSE_TRADING"
        stop_loss, target = calculate_risk_levels(
            "SELL",
            best_candidate["last_close"],
            best_candidate["strength"],
            volume_factor=best_candidate.get("volume_factor", 1.0),
            news_support=news_support,
        )
        result = place_trade_order(
            symbol,
            transaction_type="SELL",
            quantity=quantity,
            stop_loss=stop_loss,
            target=target,
        )
        if result is not None:
            ACTIVE_TRADE = {
                "symbol": symbol,
                "side": "SELL",
                "quantity": quantity,
                "entry_price": best_candidate["last_close"],
                "stop_loss": stop_loss,
                "target": target,
                "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "open",
            }
            log_trade_event({
                "symbol": symbol,
                "side": "SELL",
                "quantity": quantity,
                "entry_time": ACTIVE_TRADE["entry_time"],
                "entry_price": best_candidate["last_close"],
                "exit_time": None,
                "exit_price": None,
                "status": "open",
                "pnl_before_fees": 0.0,
                "fees_and_tax": 0.0,
                "net_pnl": 0.0,
                "wallet_balance_after_trade": STARTING_WALLET_BALANCE,
            })
        return

    print("ℹ️ No strong BUY/SELL signal found in the current scan.")


def execute_trading_brain():
    print("🤖 Launching Hybrid Automated Control Core...")

    if not is_market_open():
        print("⏰ Market is closed. Bot will wait for normal trading hours.")
        return

    market_status = news_filter.scan_live_market_news()
    if market_status == "PAUSE_TRADING":
        print("🚨 SYSTEM PAUSED: News-based risk parameters exceeded. Halting execution.")
        return

    api_client = initialize_upstox_session()
    if not api_client:
        return

    print("🟢 SYSTEM LIVE: News environment is clear. Running strict chart queries...")
    while is_market_open():
        monitor_active_trade()
        run_symbol_scan()
        time.sleep(60)

    print("⏰ Market is closed. Stopping the live scan loop.")


if __name__ == "__main__":
    validate_market_data()
    execute_trading_brain()

import os
import json
import time
import traceback
import threading
import webbrowser
import statistics
from collections import deque
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
import requests
import pyotp
from SmartApi import SmartConnect

# ---------- CONFIG ----------
API_KEY = os.getenv("SMARTAPI_API_KEY", "Aez3BY2l")
CLIENT_CODE = os.getenv("SMARTAPI_CLIENT_CODE", "AABY814754")
MPIN = os.getenv("SMARTAPI_MPIN", "4857")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET", "MPLVKXSWVPWCWA66BJHHYA47OQ")

EXCHANGE_WANTED = "NSE"
POLL_INTERVAL = 1
TOKEN_FILE = "token_list_nse.json"
INSTRUMENT_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Strategy Parameters
STRATEGY_PARAMS = {
    "enabled": False,
    "bb_window": 20,
    "std_dev_base": 2.5,
    "std_dev_alt": 2.8,
    "std_dev_switch_vol_atr": 0.5,
    "slippage_pct": 0.02,
    "risk_per_trade_pct": 0.01,
    "stop_loss_mode": "ATR",
    "stop_loss_pct": 0.07,
    "atr_period": 14,
    "atr_multiplier": 1.5,
    "confirmation_ticks": 1,
    "auto_trade_enabled": False
}
# ----------------------------

app = Flask(__name__)
CORS(app)

SMART_OBJ = None
TOKEN_DATA = None
LOCK = threading.Lock()

# ---------- SIMULATOR STATE ----------
SIMULATOR_STATE = {
    "balance": 10000000.00,
    "portfolio": {},
    "transactions": [],
    "price_history": {}  # symbol -> deque of prices for strategy
}

# ---------- Helper Functions ----------
def login_smartapi():
    print("🔐 Logging in to SmartAPI...")
    obj = SmartConnect(api_key=API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    data = obj.generateSession(CLIENT_CODE, MPIN, totp)
    if not data or data.get("status") is not True:
        raise RuntimeError(f"Login failed: {data}")
    print("✅ SmartAPI login successful.")
    return obj

def _get_first(item, keys):
    for k in keys:
        if k in item and item[k]:
            return item[k]
    return None

def _looks_like_nse(val):
    if not val: return False
    return str(val).upper().startswith("N")

def load_or_download_tokens():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    print("📥 Downloading NSE instrument list...")
    resp = requests.get(INSTRUMENT_URL, timeout=30)
    data = resp.json()
    if isinstance(data, dict):
        for k in ("data", "instruments", "result"):
            if k in data and isinstance(data[k], list):
                data = data[k]
                break
    parsed = []
    for item in data:
        exchange = _get_first(item, ["exchange", "exch", "exch_seg"])
        symbol = _get_first(item, ["symbol", "tradingsymbol", "name"])
        token = _get_first(item, ["token", "token_id", "instrument_token"])
        if exchange and symbol and token and _looks_like_nse(exchange):
            sym = symbol.upper().strip()
            if not sym.endswith("-EQ"):
                sym += "-EQ"
            parsed.append({"symbol": sym, "token": str(token)})
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(parsed, f, indent=2)
    print(f"✅ Saved {len(parsed)} NSE symbols.")
    return parsed

def find_symbol_token(token_data, stock_name):
    stock_name = stock_name.upper().strip()
    for item in token_data:
        sym = item["symbol"].upper()
        if sym == stock_name or sym == f"{stock_name}-EQ" or sym.startswith(stock_name):
            return item["symbol"], item["token"]
    return None, None

def fetch_ltp(obj, exchange, symbol, token):
    try:
        res = obj.ltpData(exchange, symbol, token)
        if "data" in res and "ltp" in res["data"]:
            return res["data"]["ltp"]
    except Exception:
        pass
    return None

def ensure_login():
    global SMART_OBJ, TOKEN_DATA
    with LOCK:
        if SMART_OBJ is None:
            SMART_OBJ = login_smartapi()
        if TOKEN_DATA is None:
            TOKEN_DATA = load_or_download_tokens()

# ---------- STRATEGY FUNCTIONS ----------
def init_price_history(symbol):
    """Initialize price history for a symbol"""
    if symbol not in SIMULATOR_STATE["price_history"]:
        max_len = max(STRATEGY_PARAMS["bb_window"], STRATEGY_PARAMS["atr_period"]) + 50
        SIMULATOR_STATE["price_history"][symbol] = deque(maxlen=max_len)

def update_price_history(symbol, price):
    """Add price to history"""
    init_price_history(symbol)
    SIMULATOR_STATE["price_history"][symbol].append(float(price))

def compute_bollinger(symbol, std_dev):
    """Calculate Bollinger Bands"""
    if symbol not in SIMULATOR_STATE["price_history"]:
        return None, None, None
    
    prices = list(SIMULATOR_STATE["price_history"][symbol])
    window = STRATEGY_PARAMS["bb_window"]
    
    if len(prices) < window:
        return None, None, None
    
    recent = prices[-window:]
    ma = statistics.mean(recent)
    sd = statistics.pstdev(recent)
    upper = ma + std_dev * sd
    lower = ma - std_dev * sd
    
    return upper, ma, lower

def compute_atr(symbol):
    """Calculate ATR"""
    if symbol not in SIMULATOR_STATE["price_history"]:
        return None
    
    prices = list(SIMULATOR_STATE["price_history"][symbol])
    period = STRATEGY_PARAMS["atr_period"]
    
    if len(prices) < period + 1:
        return None
    
    tr_values = []
    for i in range(1, period + 1):
        tr = abs(prices[-i] - prices[-i-1])
        tr_values.append(tr)
    
    return statistics.mean(tr_values)

def check_strategy_signal(symbol, current_price):
    """Check if strategy signals a buy or sell"""
    if not STRATEGY_PARAMS["enabled"]:
        return None
    
    if symbol not in SIMULATOR_STATE["price_history"]:
        return None
    
    prices = list(SIMULATOR_STATE["price_history"][symbol])
    if len(prices) < STRATEGY_PARAMS["bb_window"]:
        return None
    
    # Determine std_dev based on ATR
    atr = compute_atr(symbol)
    std_dev = STRATEGY_PARAMS["std_dev_base"]
    if atr is not None and atr > STRATEGY_PARAMS["std_dev_switch_vol_atr"]:
        std_dev = STRATEGY_PARAMS["std_dev_alt"]
    
    upper, ma, lower = compute_bollinger(symbol, std_dev)
    
    if upper is None or ma is None or lower is None:
        return None
    
    # Check for signals
    signal = None
    
    # BUY signal: price below lower band (mean reversion)
    if current_price < lower:
        # Confirmation check
        conf_ticks = STRATEGY_PARAMS["confirmation_ticks"]
        if conf_ticks > 0 and len(prices) >= conf_ticks + 1:
            confirmed = all(p < lower for p in prices[-conf_ticks:])
            if confirmed:
                signal = {
                    "action": "BUY",
                    "reason": "Price below lower Bollinger Band",
                    "price": current_price,
                    "lower_band": lower,
                    "middle_band": ma,
                    "upper_band": upper,
                    "atr": atr
                }
        elif conf_ticks == 0:
            signal = {
                "action": "BUY",
                "reason": "Price below lower Bollinger Band",
                "price": current_price,
                "lower_band": lower,
                "middle_band": ma,
                "upper_band": upper,
                "atr": atr
            }
    
    # SELL signal: price above upper band
    elif current_price > upper:
        conf_ticks = STRATEGY_PARAMS["confirmation_ticks"]
        if conf_ticks > 0 and len(prices) >= conf_ticks + 1:
            confirmed = all(p > upper for p in prices[-conf_ticks:])
            if confirmed:
                signal = {
                    "action": "SELL",
                    "reason": "Price above upper Bollinger Band",
                    "price": current_price,
                    "lower_band": lower,
                    "middle_band": ma,
                    "upper_band": upper,
                    "atr": atr
                }
        elif conf_ticks == 0:
            signal = {
                "action": "SELL",
                "reason": "Price above upper Bollinger Band",
                "price": current_price,
                "lower_band": lower,
                "middle_band": ma,
                "upper_band": upper,
                "atr": atr
            }
    
    return signal

def calculate_position_size(entry_price, atr):
    """Calculate position size based on risk parameters"""
    if STRATEGY_PARAMS["stop_loss_mode"] == "ATR" and atr:
        stop_dist = atr * STRATEGY_PARAMS["atr_multiplier"]
    else:
        stop_dist = entry_price * STRATEGY_PARAMS["stop_loss_pct"]
    
    if stop_dist <= 0:
        return 1
    
    risk_amt = SIMULATOR_STATE["balance"] * STRATEGY_PARAMS["risk_per_trade_pct"]
    qty = max(1, int(risk_amt / stop_dist))
    
    return qty

# ---------- SIMULATOR FUNCTIONS ----------
def get_current_price(stock_name):
    """Get current live price for a stock"""
    symbol, token = find_symbol_token(TOKEN_DATA, stock_name)
    if not symbol:
        return None, None
    ltp = fetch_ltp(SMART_OBJ, EXCHANGE_WANTED, symbol, token)
    return symbol, ltp

def execute_buy(stock_name, qty, auto_trade=False):
    """Execute a fake buy order"""
    symbol, price = get_current_price(stock_name)
    if not symbol or price is None:
        return {"success": False, "error": f"Stock '{stock_name}' not found or price unavailable"}
    
    # Update price history
    update_price_history(symbol, price)
    
    # Check strategy signal if auto_trade
    signal_info = None
    if auto_trade and STRATEGY_PARAMS["auto_trade_enabled"]:
        signal = check_strategy_signal(symbol, price)
        if signal and signal["action"] == "BUY":
            signal_info = signal
            # Recalculate quantity based on risk parameters
            atr = signal.get("atr")
            qty = calculate_position_size(price, atr)
        elif signal and signal["action"] != "BUY":
            return {"success": False, "error": "Strategy does not signal BUY at current price"}
        elif not signal:
            return {"success": False, "error": "Insufficient data or no clear signal"}
    
    qty = int(qty)
    entry_price = price * (1 + STRATEGY_PARAMS["slippage_pct"])
    total_cost = entry_price * qty
    
    if SIMULATOR_STATE["balance"] < total_cost:
        return {"success": False, "error": "Insufficient balance"}
    
    # Deduct balance
    SIMULATOR_STATE["balance"] -= total_cost
    
    # Update portfolio
    if symbol in SIMULATOR_STATE["portfolio"]:
        holding = SIMULATOR_STATE["portfolio"][symbol]
        old_qty = holding["qty"]
        old_avg = holding["avg_price"]
        new_qty = old_qty + qty
        new_avg = ((old_qty * old_avg) + (qty * entry_price)) / new_qty
        SIMULATOR_STATE["portfolio"][symbol] = {"qty": new_qty, "avg_price": new_avg}
    else:
        SIMULATOR_STATE["portfolio"][symbol] = {"qty": qty, "avg_price": entry_price}
    
    # Record transaction
    tx = {
        "type": "BUY",
        "symbol": symbol,
        "qty": qty,
        "price": entry_price,
        "total": total_cost,
        "timestamp": time.time()
    }
    
    if signal_info:
        tx["strategy_signal"] = signal_info
    
    SIMULATOR_STATE["transactions"].append(tx)
    
    return {
        "success": True,
        "message": f"Bought {qty} shares of {symbol} at ₹{entry_price:.2f}",
        "balance": SIMULATOR_STATE["balance"],
        "portfolio": SIMULATOR_STATE["portfolio"],
        "signal": signal_info
    }

def execute_sell(stock_name, qty, auto_trade=False):
    """Execute a fake sell order"""
    symbol, price = get_current_price(stock_name)
    if not symbol or price is None:
        return {"success": False, "error": f"Stock '{stock_name}' not found or price unavailable"}
    
    # Update price history
    update_price_history(symbol, price)
    
    # Check strategy signal if auto_trade
    signal_info = None
    if auto_trade and STRATEGY_PARAMS["auto_trade_enabled"]:
        signal = check_strategy_signal(symbol, price)
        if signal and signal["action"] == "SELL":
            signal_info = signal
            # Recalculate quantity based on risk parameters
            atr = signal.get("atr")
            qty = calculate_position_size(price, atr)
        elif signal and signal["action"] != "SELL":
            return {"success": False, "error": "Strategy does not signal SELL at current price"}
        elif not signal:
            return {"success": False, "error": "Insufficient data or no clear signal"}
    
    qty = int(qty)
    
    # Check if user has this stock
    if symbol not in SIMULATOR_STATE["portfolio"]:
        return {"success": False, "error": f"You don't own any shares of {symbol}"}
    
    holding = SIMULATOR_STATE["portfolio"][symbol]
    if holding["qty"] < qty:
        return {"success": False, "error": f"Insufficient quantity. You only have {holding['qty']} shares"}
    
    # Calculate proceeds
    exit_price = price * (1 - STRATEGY_PARAMS["slippage_pct"])
    total_proceeds = exit_price * qty
    
    # Add to balance
    SIMULATOR_STATE["balance"] += total_proceeds
    
    # Update portfolio
    holding["qty"] -= qty
    if holding["qty"] == 0:
        del SIMULATOR_STATE["portfolio"][symbol]
    
    # Record transaction
    tx = {
        "type": "SELL",
        "symbol": symbol,
        "qty": qty,
        "price": exit_price,
        "total": total_proceeds,
        "timestamp": time.time()
    }
    
    if signal_info:
        tx["strategy_signal"] = signal_info
    
    SIMULATOR_STATE["transactions"].append(tx)
    
    return {
        "success": True,
        "message": f"Sold {qty} shares of {symbol} at ₹{exit_price:.2f}",
        "balance": SIMULATOR_STATE["balance"],
        "portfolio": SIMULATOR_STATE["portfolio"],
        "signal": signal_info
    }

# ---------- API ENDPOINTS ----------
@app.route("/api/ltp")
def api_ltp():
    ensure_login()
    stock = request.args.get("stock", "").strip()
    if not stock:
        return jsonify({"error": "Stock name required"}), 400
    symbol, token = find_symbol_token(TOKEN_DATA, stock)
    if not symbol:
        return jsonify({"error": f"Stock '{stock}' not found"}), 404
    ltp = fetch_ltp(SMART_OBJ, EXCHANGE_WANTED, symbol, token)
    if ltp is None:
        return jsonify({"error": "Failed to fetch price"}), 500
    
    # Update price history
    update_price_history(symbol, ltp)
    
    # Check for strategy signal
    signal = None
    if STRATEGY_PARAMS["enabled"]:
        signal = check_strategy_signal(symbol, ltp)
    
    # Calculate Bollinger Bands for display
    bb_data = None
    if symbol in SIMULATOR_STATE["price_history"]:
        atr = compute_atr(symbol)
        std_dev = STRATEGY_PARAMS["std_dev_base"]
        if atr is not None and atr > STRATEGY_PARAMS["std_dev_switch_vol_atr"]:
            std_dev = STRATEGY_PARAMS["std_dev_alt"]
        upper, ma, lower = compute_bollinger(symbol, std_dev)
        if upper is not None:
            bb_data = {
                "upper": upper,
                "middle": ma,
                "lower": lower,
                "atr": atr
            }
    
    return jsonify({
        "symbol": symbol,
        "ltp": ltp,
        "signal": signal,
        "bollinger": bb_data
    })

@app.route("/api/buy", methods=["POST"])
def api_buy():
    ensure_login()
    data = request.get_json()
    stock = data.get("stock", "").strip()
    qty = data.get("qty", 1)
    auto_trade = data.get("auto_trade", False)
    
    if not stock:
        return jsonify({"error": "Stock name required"}), 400
    
    try:
        qty = int(qty)
        if qty <= 0:
            return jsonify({"error": "Quantity must be positive"}), 400
    except:
        return jsonify({"error": "Invalid quantity"}), 400
    
    result = execute_buy(stock, qty, auto_trade)
    
    if result["success"]:
        return jsonify(result), 200
    else:
        return jsonify(result), 400

@app.route("/api/sell", methods=["POST"])
def api_sell():
    ensure_login()
    data = request.get_json()
    stock = data.get("stock", "").strip()
    qty = data.get("qty", 1)
    auto_trade = data.get("auto_trade", False)
    
    if not stock:
        return jsonify({"error": "Stock name required"}), 400
    
    try:
        qty = int(qty)
        if qty <= 0:
            return jsonify({"error": "Quantity must be positive"}), 400
    except:
        return jsonify({"error": "Invalid quantity"}), 400
    
    result = execute_sell(stock, qty, auto_trade)
    
    if result["success"]:
        return jsonify(result), 200
    else:
        return jsonify(result), 400

@app.route("/api/status")
def api_status():
    """Return current simulator status with live P&L"""
    portfolio_with_pnl = {}
    total_invested = 0
    total_current_value = 0
    
    for symbol, info in SIMULATOR_STATE["portfolio"].items():
        qty = info["qty"]
        avg_price = info["avg_price"]
        invested = qty * avg_price
        
        _, current_price = get_current_price(symbol)
        if current_price:
            current_value = qty * current_price
            pnl = current_value - invested
            pnl_pct = (pnl / invested) * 100 if invested > 0 else 0
            
            portfolio_with_pnl[symbol] = {
                "qty": qty,
                "avg_price": avg_price,
                "current_price": current_price,
                "invested": invested,
                "current_value": current_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct
            }
            
            total_invested += invested
            total_current_value += current_value
        else:
            portfolio_with_pnl[symbol] = {
                "qty": qty,
                "avg_price": avg_price,
                "current_price": avg_price,
                "invested": invested,
                "current_value": invested,
                "pnl": 0,
                "pnl_pct": 0
            }
            total_invested += invested
            total_current_value += invested
    
    overall_pnl = total_current_value - total_invested
    overall_pnl_pct = (overall_pnl / total_invested) * 100 if total_invested > 0 else 0
    
    return jsonify({
        "balance": SIMULATOR_STATE["balance"],
        "portfolio": portfolio_with_pnl,
        "transactions": SIMULATOR_STATE["transactions"][-50:],
        "total_invested": total_invested,
        "total_current_value": total_current_value,
        "overall_pnl": overall_pnl,
        "overall_pnl_pct": overall_pnl_pct,
        "strategy_params": STRATEGY_PARAMS
    })

@app.route("/api/strategy/params", methods=["GET", "POST"])
def api_strategy_params():
    """Get or update strategy parameters"""
    if request.method == "GET":
        return jsonify(STRATEGY_PARAMS)
    
    data = request.get_json()
    for key, value in data.items():
        if key in STRATEGY_PARAMS:
            STRATEGY_PARAMS[key] = value
    
    return jsonify({"success": True, "params": STRATEGY_PARAMS})

@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Reset the simulator to initial state"""
    SIMULATOR_STATE["balance"] = 10000000.00
    SIMULATOR_STATE["portfolio"] = {}
    SIMULATOR_STATE["transactions"] = []
    SIMULATOR_STATE["price_history"] = {}
    return jsonify({"message": "Simulator reset successfully", "balance": 10000000.00})

# ---------- Frontend ----------
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Live Stock Price & Trading Simulator with Bollinger Strategy</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <meta name="viewport" content="width=device-width,initial-scale=1" />

  <link rel="icon" type="image/png" href="./static/Favicon.png">
  <style>
    :root{
  --bg: #f5f7fa;
  --card: #ffffff;
  --accent: #06b6d4;
  --muted: #4b5563;
  --success: #16a34a;
  --danger: #ef4444;
  --glass: rgba(0, 0, 0, 0.03);
  --card-radius: 14px;
}

html, body {
  height: 100%;
  margin: 0;
  font-family: Inter, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial;
}

body {
  background: linear-gradient(180deg, #e2e8f0 0%, #edf2f7 100%);
  color: #1f2937;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  padding: 18px;
  box-sizing: border-box;
  display: flex;
  justify-content: center;
}

.container {
  width: 100%;
  max-width: 1100px;
  margin: 18px;
}

header {
  text-align: center;
  margin-bottom: 14px;
}

header h1 {
  margin: 0;
  font-size: 22px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: linear-gradient(90deg, #c7f9ff, #7de3f4);
  -webkit-background-clip: text;
  background-clip: text;
  color: black;
}

header p {
  margin: 6px 0 0;
  color: var(--muted);
  font-size: 13px;
}

.card {
  background: var(--card);
  border-radius: var(--card-radius);
  padding: 14px;
  box-shadow: 0 4px 18px rgba(2, 6, 23, 0.1);
  margin-bottom: 14px;
}

.controls {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
}

.controls input[type="text"] {
  min-width: 220px;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid rgba(0, 0, 0, 0.1);
  background: var(--glass);
  color: inherit;
  font-size: 15px;
}

.controls button {
  padding: 10px 14px;
  border-radius: 10px;
  border: 0;
  background: linear-gradient(90deg, var(--accent), #3dd3c9);
  color: #042029;
  font-weight: 600;
  cursor: pointer;
  font-size: 14px;
}

.controls .small {
  padding: 8px 10px;
  font-size: 13px;
  background: transparent;
  border: 1px solid rgba(0, 0, 0, 0.1);
  color: var(--muted);
}

.layout {
  display: grid;
  grid-template-columns: 1fr 360px;
  gap: 12px;
}

@media (max-width: 920px) {
  .layout {
    grid-template-columns: 1fr;
  }
}

.chart-wrap {
  padding: 8px;
}

#priceBox {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.price-current {
  font-size: 20px;
  font-weight: 700;
}

.price-change {
  font-size: 13px;
  padding: 6px 8px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.03);
  color: var(--muted);
}

.trade-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.trade-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.trade-row input[type="number"] {
  width: 100%;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid rgba(0, 0, 0, 0.1);
  background: var(--glass);
  color: inherit;
}

.trade-actions {
  display: flex;
  gap: 8px;
}

.buy-btn {
  background: linear-gradient(180deg, #10b981, #059669);
  color: white;
  border: 0;
  padding: 10px 12px;
  border-radius: 10px;
  cursor: pointer;
  font-weight: 600;
  flex: 1;
}

.sell-btn {
  background: linear-gradient(180deg, #ef4444, #dc2626);
  color: white;
  border: 0;
  padding: 10px 12px;
  border-radius: 10px;
  cursor: pointer;
  font-weight: 600;
  flex: 1;
}

.info-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 14px;
  color: var(--muted);
  margin-top: 6px;
}

.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 12px;
}

@media (max-width: 640px) {
  .grid-2 {
    grid-template-columns: 1fr;
  }
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

th,
td {
  padding: 8px;
  text-align: left;
  border-bottom: 1px dashed rgba(0, 0, 0, 0.1);
}

th {
  font-weight: 600;
  color: var(--muted);
  font-size: 12px;
}

.muted {
  color: var(--muted);
  font-size: 13px;
}

.pos {
  color: var(--success);
  font-weight: 700;
}

.neg {
  color: var(--danger);
  font-weight: 700;
}

.small-muted {
  color: var(--muted);
  font-size: 12px;
}

.balance {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  padding: 8px;
  border-radius: 10px;
  background: linear-gradient(180deg, rgba(0, 0, 0, 0.02), transparent);
}

.tx-list {
  max-height: 220px;
  overflow: auto;
  padding-right: 6px;
}

.pnl-item {
  background: rgba(0, 0, 0, 0.015);
  border-radius: 8px;
  padding: 8px 10px;
  margin-bottom: 6px;
  border-left: 3px solid transparent;
}

.pnl-item.profit {
  border-left-color: var(--success);
}

.pnl-item.loss {
  border-left-color: var(--danger);
}

.pnl-item-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}

.pnl-item-symbol {
  font-weight: 700;
  font-size: 14px;
}

.pnl-item-value {
  font-weight: 700;
  font-size: 14px;
}

.pnl-item-details {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--muted);
  margin-top: 4px;
}

.param-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px;
  margin-top: 10px;
}

.param-item label {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 4px;
}

.param-item input,
.param-item select {
  width: 100%;
  padding: 8px;
  border-radius: 8px;
  border: 1px solid rgba(0, 0, 0, 0.1);
  background: var(--glass);
  color: inherit;
  font-size: 14px;
}

.param-item input[type="checkbox"] {
  width: auto;
  margin-right: 6px;
}

.toggle-section {
  cursor: pointer;
  user-select: none;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.02);
  margin-bottom: 10px;
}

.toggle-section:hover {
  background: rgba(0, 0, 0, 0.04);
}

.collapsible-content {
  max-height: 0;
  overflow: hidden;
  transition: max-height 0.3s ease;
}

.collapsible-content.open {
  max-height: 2000px;
}

 .signal-indicator {
      padding: 8px 12px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      text-align: center;
      margin-top: 8px;
    }

    .signal-buy {
      background: linear-gradient(90deg, #10b981, #059669);
      color: white;
    }

    .signal-sell {
      background: linear-gradient(90deg, #ef4444, #dc2626);
      color: white;
    }

    .signal-neutral {
      background: rgba(0, 0, 0, 0.05);
      color: var(--muted);
    }

    .bb-bands {
      font-size: 12px;
      color: var(--muted);
      margin-top: 6px;
      padding: 6px;
      background: rgba(0, 0, 0, 0.05);
      border-radius: 6px;
    }

    @media (max-width: 520px) {
      header h1 {
        font-size: 18px;
      }

      .controls input[type="text"] {
        min-width: 140px;
      }

      .layout {
        gap: 10px;
      }
    }
  </style>
</head>
<body>
  <div class="container">

    <header>
      <h1>📈 Live Stock Price Viewer & Trading Simulator</h1>
      <p>Enter a symbol, view live price + chart, use Bollinger Band strategy for automated signals.</p>
    </header>

    <div class="card">
      <div class="controls" role="region" aria-label="Stock controls">
        <input id="stockInput" placeholder="Enter stock (e.g. RELIANCE, TCS)" aria-label="Stock symbol" />
        <button id="getPriceBtn">Get Price</button>
        <button id="stopBtn" class="small">Stop Updates</button>
        <button id="resetBtn" class="small">Reset Simulator</button>
      </div>
    </div>

    <div class="layout">
      <!-- left: chart -->
      <div class="card chart-wrap">
        <div id="priceBox">
          <div>
            <div id="currentSymbol" class="muted">—</div>
            <div class="price-current" id="currentPrice">₹ -</div>
            <div class="price-change" id="priceChange">—</div>
          </div>
          <div style="min-width:220px;">
            <div class="muted small-muted">Bank balance</div>
            <div class="balance" id="balanceBox">
              <div style="font-weight:700" id="balanceAmt">₹ 10,000,000.00</div>
              <div class="small-muted" id="cashAvailable">available</div>
            </div>
          </div>
        </div>

        <div id="signalIndicator" class="signal-indicator signal-neutral">No active signal</div>
        
        <div id="bollingerBands" class="bb-bands" style="display:none;">
          <div><strong>Bollinger Bands:</strong></div>
          <div>Upper: <span id="bbUpper">-</span></div>
          <div>Middle: <span id="bbMiddle">-</span></div>
          <div>Lower: <span id="bbLower">-</span></div>
          <div>ATR: <span id="bbATR">-</span></div>
        </div>

        <canvas id="chart" height="160" style="width:100%;max-height:420px"></canvas>

        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;">
          <div class="small-muted">Live updates every 1s (when active)</div>
          <div class="small-muted" id="lastUpdated">—</div>
        </div>

        <div class="grid-2" style="margin-top:12px;">
          <div class="card" style="padding:10px;">
            <div style="font-weight:700;margin-bottom:6px">Portfolio</div>
            <div id="portfolioContainer"></div>
          </div>

          <div class="card" style="padding:10px;">
            <div style="font-weight:700;margin-bottom:6px">Transactions</div>
            <div class="tx-list" id="transactionsContainer"></div>
          </div>
        </div>
      </div>

      <!-- right: trading panel -->
      <div class="card trade-card">
        <!-- Strategy Parameters Section -->
        <div class="toggle-section" id="paramsToggle">
          <div style="font-weight:700; font-size:16px;">⚙️ Strategy Parameters</div>
          <div style="font-size:20px;">▼</div>
        </div>
        
        <div id="paramsContent" class="collapsible-content">
          <div class="param-item">
            <label>
              <input type="checkbox" id="strategyEnabled" />
              Enable Strategy Signals
            </label>
          </div>
          
          <div class="param-item">
            <label>
              <input type="checkbox" id="autoTradeEnabled" />
              Enable Auto-Trading (Execute on signals)
            </label>
          </div>

          <div class="param-grid">
            <div class="param-item">
              <label>BB Window (Periods)</label>
              <input type="number" id="bbWindow" min="5" max="100" step="1" value="20" />
            </div>

            <div class="param-item">
              <label>Std Dev (Base)</label>
              <input type="number" id="stdDevBase" min="1" max="5" step="0.1" value="2.5" />
            </div>

            <div class="param-item">
              <label>Std Dev (High Vol)</label>
              <input type="number" id="stdDevAlt" min="1" max="5" step="0.1" value="2.8" />
            </div>

            <div class="param-item">
              <label>ATR Period</label>
              <input type="number" id="atrPeriod" min="5" max="50" step="1" value="14" />
            </div>

            <div class="param-item">
              <label>ATR Multiplier</label>
              <input type="number" id="atrMultiplier" min="0.5" max="5" step="0.1" value="1.5" />
            </div>

            <div class="param-item">
              <label>Confirmation Ticks</label>
              <input type="number" id="confirmationTicks" min="0" max="10" step="1" value="1" />
            </div>

            <div class="param-item">
              <label>Stop Loss Mode</label>
              <select id="stopLossMode">
                <option value="ATR">ATR</option>
                <option value="PERCENT">Percent</option>
              </select>
            </div>

            <div class="param-item">
              <label>Stop Loss % (Fallback)</label>
              <input type="number" id="stopLossPct" min="0.01" max="0.5" step="0.01" value="0.07" />
            </div>

            <div class="param-item">
              <label>Risk Per Trade %</label>
              <input type="number" id="riskPerTrade" min="0.001" max="0.1" step="0.001" value="0.01" />
            </div>

            <div class="param-item">
              <label>Slippage %</label>
              <input type="number" id="slippagePct" min="0" max="0.1" step="0.001" value="0.02" />
            </div>
          </div>

          <button id="saveParamsBtn" style="width:100%;margin-top:10px;padding:10px;border-radius:10px;border:0;background:linear-gradient(90deg,var(--accent),#3dd3c9);color:#042029;font-weight:600;cursor:pointer;">
            Save Parameters
          </button>
        </div>

        <hr style="border:none;border-top:1px dashed rgba(255,255,255,0.03);margin:8px 0"/>

        <div style="font-weight:700; font-size:16px;">Trading Panel</div>
        <div class="muted small-muted">Symbol</div>
        <div><input id="tradeSymbol" type="text" placeholder="Symbol" /></div>

        <div class="trade-row">
          <div style="flex:1">
            <div class="muted small-muted">Quantity</div>
            <input id="tradeQty" type="number" min="1" step="1" value="1" />
          </div>
          <div style="width:120px">
            <div class="muted small-muted">Order Type</div>
            <select id="orderType" style="width:100%;padding:8px;border-radius:8px;background:var(--glass);border:1px solid rgba(255,255,255,0.04);">
              <option value="market">Market</option>
            </select>
          </div>
        </div>

        <div class="trade-row">
          <div style="flex:1">
            <div class="muted small-muted">Estimated Value</div>
            <div id="estValue">₹ 0.00</div>
          </div>
          <div style="flex:1">
            <div class="muted small-muted">Position Qty</div>
            <div id="posQty">0</div>
          </div>
        </div>

        <div class="trade-actions">
          <button class="buy-btn" id="buyBtn">Buy</button>
          <button class="sell-btn" id="sellBtn">Sell</button>
        </div>

        <div class="info-row">
          <div class="muted small-muted">Price used: <span id="usedPrice">—</span></div>
          <div class="muted small-muted">Available Cash: <span id="availCash">—</span></div>
        </div>

        <hr style="border:none;border-top:1px dashed rgba(255,255,255,0.03);margin:8px 0"/>

        <div style="font-weight:700">Holdings</div>
        <div id="holdingsList" style="margin-top:8px"></div>

        <hr style="border:none;border-top:1px dashed rgba(255,255,255,0.03);margin:8px 0"/>

        <div style="font-weight:700">Profit & Loss</div>
        <div id="pnlSection" style="margin-top:8px">
          <div style="background:linear-gradient(180deg,rgba(255,255,255,0.02),transparent);border-radius:10px;padding:10px;">
            <div style="display:flex;justify-content:space-between;margin-bottom:8px;">
              <span class="muted small-muted">Total Invested</span>
              <span id="totalInvested" style="font-weight:600">₹ 0.00</span>
            </div>
            <div style="display:flex;justify-content:space-between;margin-bottom:8px;">
              <span class="muted small-muted">Current Value</span>
              <span id="currentValue" style="font-weight:600">₹ 0.00</span>
            </div>
            <div style="height:1px;background:rgba(255,255,255,0.05);margin:8px 0"></div>
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <span style="font-weight:700;font-size:15px">Overall P&L</span>
              <div style="text-align:right">
                <div id="overallPnl" style="font-weight:700;font-size:16px">₹ 0.00</div>
                <div id="overallPnlPct" class="small-muted">(0.00%)</div>
              </div>
            </div>
          </div>
          
          <div style="margin-top:12px" id="stockPnlList"></div>
        </div>

      </div>
    </div>
  </div>

  <script>
  (function(){
    const API_LTP = "/api/ltp";
    const API_BUY = "/api/buy";
    const API_SELL = "/api/sell";
    const API_STATUS = "/api/status";
    const API_RESET = "/api/reset";
    const API_PARAMS = "/api/strategy/params";
    const POLL_MS = 1000;
    const MAX_POINTS = 120;

    const state = {
      symbol: null,
      price: null,
      prevPrice: null,
      priceHistory: [],
      timestamps: [],
      running: false,
      lastUpdated: null,
      balance: 10000000.00,
      holdings: {},
      transactions: [],
      strategyParams: {},
      currentSignal: null,
      bollingerData: null
    };

    const stockInput = document.getElementById("stockInput");
    const getPriceBtn = document.getElementById("getPriceBtn");
    const stopBtn = document.getElementById("stopBtn");
    const resetBtn = document.getElementById("resetBtn");
    const currentSymbolEl = document.getElementById("currentSymbol");
    const currentPriceEl = document.getElementById("currentPrice");
    const priceChangeEl = document.getElementById("priceChange");
    const lastUpdatedEl = document.getElementById("lastUpdated");
    const balanceAmtEl = document.getElementById("balanceAmt");
    const tradeSymbolEl = document.getElementById("tradeSymbol");
    const tradeQtyEl = document.getElementById("tradeQty");
    const estValueEl = document.getElementById("estValue");
    const posQtyEl = document.getElementById("posQty");
    const usedPriceEl = document.getElementById("usedPrice");
    const availCashEl = document.getElementById("availCash");
    const portfolioContainer = document.getElementById("portfolioContainer");
    const holdingsList = document.getElementById("holdingsList");
    const transactionsContainer = document.getElementById("transactionsContainer");
    const buyBtn = document.getElementById("buyBtn");
    const sellBtn = document.getElementById("sellBtn");
    const signalIndicator = document.getElementById("signalIndicator");
    const bollingerBands = document.getElementById("bollingerBands");

    let pollTimer = null;

    const ctx = document.getElementById('chart').getContext('2d');
    const chartData = {
      labels: state.timestamps,
      datasets: [{
        label: 'Price',
        data: state.priceHistory,
        tension: 0.25,
        borderWidth: 2,
        pointRadius: 0,
        fill: true,
        backgroundColor: 'rgba(63, 196, 255, 0.06)',
        borderColor: '#3dd3c9'
      }]
    };
    const chartOpts = {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: { display: false },
        y: {
          ticks: { callback: v => '₹' + Number(v).toLocaleString() },
          grid: { color: 'rgba(255,255,255,0.03)' }
        }
      },
      plugins: {
        legend: { display: false }
      }
    };
    const priceChart = new Chart(ctx, {
      type: 'line',
      data: chartData,
      options: chartOpts
    });

    function formatCurrency(v){ return '₹ ' + Number(v).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}); }
    function nowTime(){ return new Date().toLocaleTimeString(); }

    function pushPrice(p){
      state.prevPrice = state.price;
      state.price = p;
      state.lastUpdated = Date.now();

      state.priceHistory.push(p);
      state.timestamps.push(new Date().toLocaleTimeString());

      if (state.priceHistory.length > MAX_POINTS){
        state.priceHistory.shift();
        state.timestamps.shift();
      }
      priceChart.data.labels = state.timestamps.slice();
      priceChart.data.datasets[0].data = state.priceHistory.slice();
      priceChart.update();

      currentPriceEl.textContent = formatCurrency(p);
      currentSymbolEl.textContent = state.symbol || '—';

      if (state.prevPrice != null){
        const diff = p - state.prevPrice;
        const pct = (diff / state.prevPrice) * 100;
        const sign = diff >= 0 ? '+' : '-';
        priceChangeEl.innerHTML = `${sign} ₹${Math.abs(diff).toFixed(2)} (${sign}${Math.abs(pct).toFixed(2)}%)`;
        priceChangeEl.className = diff >= 0 ? 'price-change pos' : 'price-change neg';
      } else {
        priceChangeEl.textContent = '—';
        priceChangeEl.className = 'price-change';
      }
      lastUpdatedEl.textContent = `Updated ${nowTime()}`;
      usedPriceEl.textContent = formatCurrency(p);
      updateEstValue();
      calculateAndRenderPnL();
    }

    function updateSignalDisplay(signal, bbData){
      state.currentSignal = signal;
      state.bollingerData = bbData;

      if (signal && signal.action) {
        if (signal.action === "BUY") {
          signalIndicator.className = "signal-indicator signal-buy";
          signalIndicator.textContent = `🔵 BUY SIGNAL: ${signal.reason}`;
        } else if (signal.action === "SELL") {
          signalIndicator.className = "signal-indicator signal-sell";
          signalIndicator.textContent = `🔴 SELL SIGNAL: ${signal.reason}`;
        }
      } else {
        signalIndicator.className = "signal-indicator signal-neutral";
        signalIndicator.textContent = "No active signal";
      }

      if (bbData && bbData.upper !== undefined) {
        bollingerBands.style.display = "block";
        document.getElementById("bbUpper").textContent = formatCurrency(bbData.upper);
        document.getElementById("bbMiddle").textContent = formatCurrency(bbData.middle);
        document.getElementById("bbLower").textContent = formatCurrency(bbData.lower);
        document.getElementById("bbATR").textContent = bbData.atr ? bbData.atr.toFixed(4) : "N/A";
      } else {
        bollingerBands.style.display = "none";
      }
    }

    function setRunning(flag){
      state.running = flag;
      if (!flag){
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        stopBtn.textContent = 'Start Updates';
      } else {
        stopBtn.textContent = 'Stop Updates';
      }
    }

    function safeFetchJson(url, opts){
      return fetch(url, opts).then(async r => {
        const ct = r.headers.get('content-type') || '';
        if (!r.ok) {
          let text = await r.text();
          throw new Error(text || r.statusText || ('HTTP ' + r.status));
        }
        if (ct.indexOf('application/json') !== -1) return r.json();
        return JSON.parse(await r.text());
      });
    }

    async function fetchLtpFor(stock){
      const url = `${API_LTP}?stock=${encodeURIComponent(stock)}`;
      try {
        const data = await safeFetchJson(url, { method: 'GET' });
        if (data && (data.ltp !== undefined && data.symbol)) {
          return data;
        }
        throw new Error('Unexpected LTP response');
      } catch (err){
        throw err;
      }
    }

    async function fetchStatus(){
      try {
        const data = await safeFetchJson(API_STATUS);
        if (data) {
          state.balance = Number(data.balance ?? state.balance);
          state.holdings = data.portfolio ?? {};
          state.transactions = data.transactions ?? [];
          if (data.strategy_params) {
            state.strategyParams = data.strategy_params;
          }
          updateStatusUI();
        }
      } catch (err){
        console.warn('Status fetch failed', err);
      }
    }

    async function fetchStrategyParams(){
      try {
        const data = await safeFetchJson(API_PARAMS);
        if (data) {
          state.strategyParams = data;
          loadParamsToUI();
        }
      } catch (err){
        console.warn('Params fetch failed', err);
      }
    }

    async function saveStrategyParams(){
      const params = {
        enabled: document.getElementById("strategyEnabled").checked,
        auto_trade_enabled: document.getElementById("autoTradeEnabled").checked,
        bb_window: parseInt(document.getElementById("bbWindow").value),
        std_dev_base: parseFloat(document.getElementById("stdDevBase").value),
        std_dev_alt: parseFloat(document.getElementById("stdDevAlt").value),
        atr_period: parseInt(document.getElementById("atrPeriod").value),
        atr_multiplier: parseFloat(document.getElementById("atrMultiplier").value),
        confirmation_ticks: parseInt(document.getElementById("confirmationTicks").value),
        stop_loss_mode: document.getElementById("stopLossMode").value,
        stop_loss_pct: parseFloat(document.getElementById("stopLossPct").value),
        risk_per_trade_pct: parseFloat(document.getElementById("riskPerTrade").value),
        slippage_pct: parseFloat(document.getElementById("slippagePct").value)
      };

      try {
        const res = await safeFetchJson(API_PARAMS, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(params)
        });
        alert('Strategy parameters saved successfully!');
        state.strategyParams = res.params;
      } catch (err){
        alert('Failed to save parameters: ' + err.message);
      }
    }

    function loadParamsToUI(){
      const p = state.strategyParams;
      document.getElementById("strategyEnabled").checked = p.enabled || false;
      document.getElementById("autoTradeEnabled").checked = p.auto_trade_enabled || false;
      document.getElementById("bbWindow").value = p.bb_window || 20;
      document.getElementById("stdDevBase").value = p.std_dev_base || 2.5;
      document.getElementById("stdDevAlt").value = p.std_dev_alt || 2.8;
      document.getElementById("atrPeriod").value = p.atr_period || 14;
      document.getElementById("atrMultiplier").value = p.atr_multiplier || 1.5;
      document.getElementById("confirmationTicks").value = p.confirmation_ticks || 1;
      document.getElementById("stopLossMode").value = p.stop_loss_mode || "ATR";
      document.getElementById("stopLossPct").value = p.stop_loss_pct || 0.07;
      document.getElementById("riskPerTrade").value = p.risk_per_trade_pct || 0.01;
      document.getElementById("slippagePct").value = p.slippage_pct || 0.02;
    }

    async function placeBuy(stock, qty, autoTrade){
      try {
        const res = await safeFetchJson(API_BUY, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ stock, qty, auto_trade: autoTrade })
        });
        await fetchStatus();
        if (res.message) alert(res.message);
        if (res.signal) {
          console.log('Trade executed on signal:', res.signal);
        }
        return res;
      } catch (err){
        alert('Buy failed: ' + (err.message || err));
        throw err;
      }
    }

    async function placeSell(stock, qty, autoTrade){
      try {
        const res = await safeFetchJson(API_SELL, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ stock, qty, auto_trade: autoTrade })
        });
        await fetchStatus();
        if (res.message) alert(res.message);
        if (res.signal) {
          console.log('Trade executed on signal:', res.signal);
        }
        return res;
      } catch (err){
        alert('Sell failed: ' + (err.message || err));
        throw err;
      }
    }

    async function tryResetServer(){
      try {
        const res = await fetch(API_RESET, { method: 'POST' });
        if (res.ok) {
          alert('Simulator reset successfully!');
          await fetchStatus();
          state.priceHistory = [];
          state.timestamps = [];
          priceChart.data.labels = [];
          priceChart.data.datasets[0].data = [];
          priceChart.update();
        } else {
          const txt = await res.text().catch(()=>null);
          alert('Server reset responded: ' + (txt || res.statusText));
        }
      } catch (err){
        console.info('Server reset error', err);
        alert('Server reset failed: ' + err.message);
      }
    }

    function updateStatusUI(){
      balanceAmtEl.textContent = formatCurrency(state.balance);
      availCashEl.textContent = formatCurrency(state.balance);
      
      if (!state.holdings || Object.keys(state.holdings).length === 0){
        portfolioContainer.innerHTML = '<div class="muted">No holdings</div>';
        holdingsList.innerHTML = '<div class="muted">No holdings</div>';
        posQtyEl.textContent = '0';
      } else {
        let html = '<table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg Price</th><th>Value</th></tr></thead><tbody>';
        let holdingsListHtml = '';
        for (const [sym, info] of Object.entries(state.holdings)){
          const qty = Number(info.qty || 0);
          const avg = Number(info.avg_price || info.avgPrice || 0);
          const val = qty * avg;
          html += `<tr><td>${sym}</td><td>${qty}</td><td>${formatCurrency(avg)}</td><td>${formatCurrency(val)}</td></tr>`;
          holdingsListHtml += `<div style="margin-bottom:6px"><strong>${sym}</strong> — ${qty} @ ${formatCurrency(avg)}</div>`;
          if (sym === state.symbol) posQtyEl.textContent = qty;
        }
        html += '</tbody></table>';
        portfolioContainer.innerHTML = html;
        holdingsList.innerHTML = holdingsListHtml;
        
        if (state.symbol && state.holdings[state.symbol]) {
          posQtyEl.textContent = state.holdings[state.symbol].qty;
        } else if (state.symbol) {
          posQtyEl.textContent = '0';
        }
      }

      renderTransactions();
      calculateAndRenderPnL();
    }

    function renderTransactions(){
      if (!state.transactions || state.transactions.length === 0){
        transactionsContainer.innerHTML = '<div class="muted">No transactions yet</div>';
        return;
      }
      let html = '<table><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th></tr></thead><tbody>';
      for (let i = state.transactions.length-1; i >= 0; i--){
        const t = state.transactions[i];
        const time = new Date(t.timestamp * 1000).toLocaleTimeString();
        html += `<tr>
          <td>${time}</td>
          <td>${t.symbol}</td>
          <td class="${t.type === 'BUY' ? 'pos' : 'neg'}">${t.type}</td>
          <td>${t.qty}</td>
          <td>${formatCurrency(t.price)}</td>
        </tr>`;
      }
      html += '</tbody></table>';
      transactionsContainer.innerHTML = html;
    }

    function calculateAndRenderPnL(){
      let totalInvested = 0;
      let totalCurrentValue = 0;
      let stockPnlHtml = '';
      
      if (!state.holdings || Object.keys(state.holdings).length === 0){
        document.getElementById('totalInvested').textContent = formatCurrency(0);
        document.getElementById('currentValue').textContent = formatCurrency(0);
        document.getElementById('overallPnl').textContent = formatCurrency(0);
        document.getElementById('overallPnl').className = '';
        document.getElementById('overallPnlPct').textContent = '(0.00%)';
        document.getElementById('stockPnlList').innerHTML = '<div class="muted small-muted">No holdings to display P&L</div>';
        return;
      }

      for (const [sym, info] of Object.entries(state.holdings)){
        const qty = Number(info.qty || 0);
        const avgPrice = Number(info.avg_price || info.avgPrice || 0);
        const invested = qty * avgPrice;
        
        let currentPrice = avgPrice;
        if (sym === state.symbol && state.price) {
          currentPrice = Number(state.price);
        } else if (info.current_price) {
          currentPrice = Number(info.current_price);
        }
        
        const currentVal = qty * currentPrice;
        const pnl = currentVal - invested;
        const pnlPct = invested > 0 ? (pnl / invested) * 100 : 0;
        
        totalInvested += invested;
        totalCurrentValue += currentVal;
        
        const pnlClass = pnl >= 0 ? 'profit' : 'loss';
        const pnlColor = pnl >= 0 ? 'pos' : 'neg';
        const pnlSign = pnl >= 0 ? '+' : '';
        
        stockPnlHtml += `
          <div class="pnl-item ${pnlClass}">
            <div class="pnl-item-header">
              <span class="pnl-item-symbol">${sym}</span>
              <span class="pnl-item-value ${pnlColor}">${pnlSign}${formatCurrency(pnl)}</span>
            </div>
            <div class="pnl-item-details">
              <span>${qty} shares @ ${formatCurrency(avgPrice)}</span>
              <span class="${pnlColor}">${pnlSign}${pnlPct.toFixed(2)}%</span>
            </div>
            <div class="pnl-item-details">
              <span>Invested: ${formatCurrency(invested)}</span>
              <span>Current: ${formatCurrency(currentVal)}</span>
            </div>
          </div>
        `;
      }
      
      document.getElementById('totalInvested').textContent = formatCurrency(totalInvested);
      document.getElementById('currentValue').textContent = formatCurrency(totalCurrentValue);
      
      const overallPnl = totalCurrentValue - totalInvested;
      const overallPnlPct = totalInvested > 0 ? (overallPnl / totalInvested) * 100 : 0;
      
      const overallPnlEl = document.getElementById('overallPnl');
      const overallPnlSign = overallPnl >= 0 ? '+' : '';
      overallPnlEl.textContent = `${overallPnlSign}${formatCurrency(overallPnl)}`;
      overallPnlEl.className = overallPnl >= 0 ? 'pos' : 'neg';
      
      const overallPnlPctEl = document.getElementById('overallPnlPct');
      overallPnlPctEl.textContent = `(${overallPnlSign}${overallPnlPct.toFixed(2)}%)`;
      overallPnlPctEl.className = overallPnl >= 0 ? 'small-muted pos' : 'small-muted neg';
      
      document.getElementById('stockPnlList').innerHTML = stockPnlHtml;
    }

    function updateEstValue(){
      const qty = Number(tradeQtyEl.value) || 0;
      const price = Number(state.price) || 0;
      const est = qty * price;
      estValueEl.textContent = formatCurrency(est);
    }

    // Toggle collapsible section
    document.getElementById('paramsToggle').addEventListener('click', function(){
      const content = document.getElementById('paramsContent');
      const arrow = this.querySelector('div:last-child');
      if (content.classList.contains('open')) {
        content.classList.remove('open');
        arrow.textContent = '▼';
      } else {
        content.classList.add('open');
        arrow.textContent = '▲';
      }
    });

    // Save parameters button
    document.getElementById('saveParamsBtn').addEventListener('click', saveStrategyParams);

    // Get price button
    getPriceBtn.addEventListener('click', async () => {
      const raw = stockInput.value.trim();
      if (!raw) { alert('Enter a stock name'); return; }
      try {
        const data = await fetchLtpFor(raw);
        const symbol = data.symbol;
        const ltp = Number(data.ltp);
        state.symbol = symbol;
        tradeSymbolEl.value = symbol;
        pushPrice(ltp);
        
        // Update signal display
        updateSignalDisplay(data.signal, data.bollinger);
        
        await fetchStatus();

        if (!state.running){
          setRunning(true);
          pollTimer = setInterval(async () => {
            if (!state.symbol) return;
            try {
              const d = await fetchLtpFor(state.symbol);
              if (d && d.ltp !== undefined){
                pushPrice(Number(d.ltp));
                updateSignalDisplay(d.signal, d.bollinger);
              }
            } catch (err){
              console.warn('poll error', err);
            }
          }, POLL_MS);
        }
      } catch (err){
        alert('Failed to get price: ' + (err.message || err));
      }
    });

    stopBtn.addEventListener('click', () => {
      if (state.running){
        setRunning(false);
      } else {
        if (state.symbol){
          setRunning(true);
          pollTimer = setInterval(async () => {
            try {
              const d = await fetchLtpFor(state.symbol);
              if (d && d.ltp !== undefined) {
                pushPrice(Number(d.ltp));
                updateSignalDisplay(d.signal, d.bollinger);
              }
            } catch(e){ console.warn('poll error', e); }
          }, POLL_MS);
        }
      }
    });

    resetBtn.addEventListener('click', async () => {
      if (confirm('Reset simulator? This will reset your balance and clear all holdings.')) {
        await tryResetServer();
      }
    });

    tradeQtyEl.addEventListener('input', updateEstValue);
    tradeSymbolEl.addEventListener('input', () => { 
      state.symbol = tradeSymbolEl.value.trim().toUpperCase(); 
    });

    buyBtn.addEventListener('click', async () => {
      const qty = Math.max(1, Math.floor(Number(tradeQtyEl.value) || 1));
      const sym = (tradeSymbolEl.value || state.symbol || '').trim();
      if (!sym) return alert('No symbol selected.');
      
      const autoTrade = state.strategyParams.auto_trade_enabled || false;
      
      try {
        await placeBuy(sym, qty, autoTrade);
      } catch (err){
        // handled in placeBuy
      }
    });

    sellBtn.addEventListener('click', async () => {
      const qty = Math.max(1, Math.floor(Number(tradeQtyEl.value) || 1));
      const sym = (tradeSymbolEl.value || state.symbol || '').trim();
      if (!sym) return alert('No symbol selected.');
      
      const autoTrade = state.strategyParams.auto_trade_enabled || false;
      
      try {
        await placeSell(sym, qty, autoTrade);
      } catch (err){
        // handled in placeSell
      }
    });

    // Initial load
    (async function init(){
      await fetchStrategyParams();
      await fetchStatus();
      updateEstValue();
    })();

    window._simState = state;
    window._pushPrice = pushPrice;
    window._updateStatusUI = updateStatusUI;
  })();
  </script>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML_PAGE)

# ---------- Main ----------
if __name__ == "__main__":
    ensure_login()
    url = "http://127.0.0.1:5000"
    print(f"🚀 Server running at {url}")
    print(f"💰 Initial Balance: ₹{SIMULATOR_STATE['balance']:,.2f}")
    print(f"📊 Bollinger Band Strategy: {'Enabled' if STRATEGY_PARAMS['enabled'] else 'Disabled'}")
    print(f"🤖 Auto-Trading: {'Enabled' if STRATEGY_PARAMS['auto_trade_enabled'] else 'Disabled'}")
    print(f"📈 This is a FAKE trading simulator - all trades are simulated!")
    webbrowser.open(url)
    app.run(debug=False, port=5000)
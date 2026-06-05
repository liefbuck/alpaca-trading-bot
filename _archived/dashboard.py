import os
import json
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify
from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import datetime, timedelta, timezone
from config import DAILY_TARGET, DAILY_LOSS_LIMIT

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))

trading_client = TradingClient(
    api_key=os.getenv("ALPACA_API_KEY"),
    secret_key=os.getenv("ALPACA_SECRET_KEY"),
    paper=True,
)

data_client = StockHistoricalDataClient(
    api_key=os.getenv("ALPACA_API_KEY"),
    secret_key=os.getenv("ALPACA_SECRET_KEY"),
)


def get_position(symbol):
    try:
        pos = trading_client.get_open_position(symbol)
        return {
            "symbol": pos.symbol,
            "qty": float(pos.qty),
            "avg_entry_price": float(pos.avg_entry_price),
            "current_price": float(pos.current_price),
            "market_value": float(pos.market_value),
            "unrealized_pl": float(pos.unrealized_pl),
            "unrealized_plpc": round(float(pos.unrealized_plpc) * 100, 2),
        }
    except Exception:
        return None


def get_all_positions():
    positions = []
    for pos in trading_client.get_all_positions():
        positions.append({
            "symbol": pos.symbol,
            "qty": float(pos.qty),
            "avg_entry_price": float(pos.avg_entry_price),
            "current_price": float(pos.current_price),
            "market_value": float(pos.market_value),
            "unrealized_pl": float(pos.unrealized_pl),
            "unrealized_plpc": round(float(pos.unrealized_plpc) * 100, 2),
        })
    return positions


def get_account():
    acct = trading_client.get_account()
    # Use session_baseline from bot_state.json so P&L matches what the bot tracks.
    # Fall back to last_equity only if no baseline has been set yet today.
    state_path = os.path.join(BASE_DIR, "bot_state.json")
    baseline = None
    if os.path.exists(state_path):
        try:
            with open(state_path) as f:
                baseline = json.load(f).get("session_baseline")
        except Exception:
            pass
    if baseline is None:
        baseline = float(acct.last_equity)
    daily_pnl = float(acct.equity) - baseline
    return {
        "cash": float(acct.cash),
        "portfolio_value": float(acct.portfolio_value),
        "buying_power": float(acct.buying_power),
        "equity": float(acct.equity),
        "daily_pnl": round(daily_pnl, 2),
        "daily_pnl_pct": round(daily_pnl / baseline * 100, 3) if baseline else 0,
    }


def get_bars(symbol, days=30):
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        bars = data_client.get_stock_bars(
            StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end)
        )
        return [{"date": b.timestamp.strftime("%Y-%m-%d"), "close": float(b.close)} for b in bars[symbol]]
    except Exception:
        return []


def get_bot_state():
    path = os.path.join(BASE_DIR, "bot_state.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def get_bot_log(lines=20):
    path = os.path.join(BASE_DIR, "bot.log")
    if os.path.exists(path):
        with open(path) as f:
            all_lines = f.readlines()
            return [l.strip() for l in all_lines[-lines:]]
    return []


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    try:
        account = get_account()
        positions = get_all_positions()
        t_pos = get_position("T")
        t_bars = get_bars("T")
        bot_state = get_bot_state()
        bot_log = get_bot_log()
        return jsonify({
            "account": account,
            "positions": positions,
            "t_position": t_pos,
            "t_bars": t_bars,
            "bot_state": bot_state,
            "bot_log": bot_log,
            "daily_target": DAILY_TARGET,
            "daily_loss_limit": DAILY_LOSS_LIMIT,
        })
    except Exception as e:
        # Log internally but never expose tracebacks over HTTP
        print(f"ERROR in /api/status: {e}")
        return jsonify({"error": "Internal server error"}), 500


if __name__ == "__main__":
    print("Dashboard running at http://localhost:5000")
    app.run(debug=False, port=5000, use_reloader=False)

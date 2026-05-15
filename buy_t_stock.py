import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv()

client = TradingClient(
    api_key=os.getenv("ALPACA_API_KEY"),
    secret_key=os.getenv("ALPACA_SECRET_KEY"),
    paper=True,
)

order = client.submit_order(
    MarketOrderRequest(
        symbol="T",
        qty=5,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    )
)

print(f"Order submitted: {order.id}")
print(f"Symbol: {order.symbol}  Qty: {order.qty}  Side: {order.side}  Status: {order.status}")

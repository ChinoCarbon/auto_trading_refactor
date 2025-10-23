from decimal import Decimal, ROUND_DOWN
import requests

def get_symbol_step_size(base_url, symbol):
    """查询交易对数量精度 stepSize"""
    r = requests.get(f"{base_url}/fapi/v1/exchangeInfo", timeout=10)
    data = r.json()
    symbol_info = next((s for s in data["symbols"] if s["symbol"] == symbol), None)
    if not symbol_info:
        return Decimal("0.001")
    lot_size = next((f for f in symbol_info["filters"] if f["filterType"] == "LOT_SIZE"), None)
    return Decimal(lot_size["stepSize"]) if lot_size else Decimal("0.001")

def adjust_to_step_decimal(value, step):
    """用 Decimal 精确修正到 stepSize 的整数倍"""
    value = Decimal(str(value))
    step = Decimal(str(step))
    return (value // step * step).quantize(step, rounding=ROUND_DOWN)

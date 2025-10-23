from flask import Blueprint, jsonify
import requests
import time
from flask import request

symbol_bp = Blueprint("symbols", __name__)

BINANCE_FAPI_BASE = "https://fapi.binance.com"

@symbol_bp.route("/usdt", methods=["GET"])
def get_usdt_symbols():
    use_testnet = request.args.get("use_testnet", "false").lower() == "true"
    base_url = "https://testnet.binancefuture.com" if use_testnet else BINANCE_FAPI_BASE

    try:
        resp = requests.get(f"{base_url}/fapi/v1/exchangeInfo", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        symbols = []
        for s in data.get("symbols", []):
            if s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING":
                symbols.append(s.get("symbol"))
        symbols.sort()
        return jsonify({
            "success": True,
            "data": {
                "symbols": symbols,
                "total_count": len(symbols),
                "timestamp": int(time.time() * 1000)
            }
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"获取USDT合约列表失败: {e}"}), 500

@symbol_bp.route("/filters/<symbol>", methods=["GET"])
def get_symbol_filters(symbol):
    use_testnet = request.args.get("use_testnet", "false").lower() == "true"
    base_url = "https://testnet.binancefuture.com" if use_testnet else BINANCE_FAPI_BASE

    try:
        resp = requests.get(f"{base_url}/fapi/v1/exchangeInfo", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        for s in data.get("symbols", []):
            if s.get("symbol") == symbol:
                filters = {}
                for f in s.get("filters", []):
                    t = f.get("filterType")
                    if t == "LOT_SIZE":
                        filters["stepSize"] = float(f.get("stepSize", 0))
                    elif t == "PRICE_FILTER":
                        filters["tickSize"] = float(f.get("tickSize", 0))
                        filters["minPrice"] = float(f.get("minPrice", 0))
                        filters["maxPrice"] = float(f.get("maxPrice", 0))
                    elif t == "MARKET_LOT_SIZE":
                        filters["minQty"] = float(f.get("minQty", 0))
                        filters["maxQty"] = float(f.get("maxQty", 0))
                return jsonify({
                    "success": True,
                    "data": {
                        "symbol": symbol,
                        "status": s.get("status"),
                        "filters": filters
                    }
                })

        return jsonify({"success": False, "message": f"未找到交易对 {symbol} 的过滤规则"}), 404

    except Exception as e:
        return jsonify({"success": False, "message": f"获取交易对过滤规则失败: {e}"}), 500
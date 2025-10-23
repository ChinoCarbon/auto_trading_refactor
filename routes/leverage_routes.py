# routes/leverage_routes.py
from flask import Blueprint, jsonify, request
import time, json, hmac, hashlib, requests, math        
from concurrent.futures import ThreadPoolExecutor, as_completed

leverage_bp = Blueprint("leverage", __name__)

BINANCE_FAPI_BASE = "https://fapi.binance.com"
BINANCE_BASE = "https://fapi.binance.com"
BINANCE_TESTNET = "https://testnet.binancefuture.com"

def load_api_keys():
    with open("data/api_keys.json", "r", encoding="utf-8") as f:
        return json.load(f).get("api_keys", [])

def sign_request(secret_key: str, params: dict):
    query = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    signature = hmac.new(secret_key.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + f"&signature={signature}"


@leverage_bp.route("/symbol/<symbol>", methods=["GET"])
def get_symbol_leverage(symbol):
    use_testnet = request.args.get("use_testnet", "false").lower() == "true"
    base_url = "https://testnet.binancefuture.com" if use_testnet else BINANCE_FAPI_BASE

    try:
        resp = requests.get(f"{base_url}/fapi/v1/exchangeInfo", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        max_leverage = None
        for s in data.get("symbols", []):
            if s.get("symbol") == symbol:
                # 在本实现中假定最大杠杆从一级filters里读取（实际可能需签名接口）
                max_leverage = int(s.get("initialLeverage", 0)) if s.get("initialLeverage") else None
                break

        # 如果没拿到，就默认 125
        if max_leverage is None:
            max_leverage = 125

        return jsonify({
            "success": True,
            "data": {
                "symbol": symbol,
                "max_leverage": max_leverage,
                "timestamp": int(time.time() * 1000)
            }
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"查询杠杆失败: {e}"}), 500

@leverage_bp.route("/all", methods=["GET"])
def get_all_leverage():
    use_testnet = request.args.get("use_testnet", "false").lower() == "true"
    base_url = "https://testnet.binancefuture.com" if use_testnet else BINANCE_FAPI_BASE

    try:
        resp = requests.get(f"{base_url}/fapi/v1/exchangeInfo", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        symbols_info = []
        for s in data.get("symbols", []):
            if s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL":
                ml = int(s.get("initialLeverage", 0)) if s.get("initialLeverage") else 125
                symbols_info.append({
                    "symbol": s.get("symbol"),
                    "max_leverage": ml,
                    "status": s.get("status")
                })
        return jsonify({
            "success": True,
            "data": {
                "symbols": symbols_info,
                "timestamp": int(time.time() * 1000),
                "total_symbols": len(symbols_info)
            }
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"查询全部杠杆失败: {e}"}), 500

@leverage_bp.route("/modify", methods=["POST"])
def modify_user_leverage():
    """
    批量修改指定symbol、position_side的用户杠杆倍数
    请求示例：
    {
        "symbol": "ETHUSDT",
        "position_side": "LONG",
        "new_leverage": 31,
        "use_testnet": false
    }
    """
    data = request.get_json(force=True)
    symbol = data.get("symbol", "").upper()
    position_side = data.get("position_side", "").upper()
    new_leverage = data.get("new_leverage")
    use_testnet = data.get("use_testnet", False)

    if not symbol or not position_side or not new_leverage:
        return jsonify({"success": False, "msg": "缺少参数 symbol / position_side / new_leverage"}), 400

    base_url = BINANCE_TESTNET if use_testnet else BINANCE_BASE
    users = load_api_keys()
    active_users = [u for u in users if u.get("is_active", True)]

    results = []

    def modify_for_user(u):
        alias = u.get("alias")
        api_key = u.get("api_key")
        secret_key = u.get("secret_key")
        headers = {"X-MBX-APIKEY": api_key}

        try:
            # 1️⃣ 获取该用户当前仓位，判断是否持有该symbol + positionSide
            query = sign_request(secret_key, {"timestamp": int(time.time() * 1000)})
            pos_url = f"{base_url}/fapi/v2/positionRisk?{query}"
            r = requests.get(pos_url, headers=headers, timeout=10)
            positions = r.json()

            # 查找该symbol + position_side 是否有仓位
            pos = next((p for p in positions if p["symbol"] == symbol and p["positionSide"].upper() == position_side), None)
            if not pos or abs(float(pos["positionAmt"])) < 1e-12:
                return {
                    "alias": alias,
                    "symbol": symbol,
                    "position_side": position_side,
                    "has_position": False,
                    "result": "无此方向持仓"
                }

            # 2️⃣ 修改杠杆
            params = {
                "symbol": symbol,
                "leverage": int(new_leverage),
                "timestamp": int(time.time() * 1000)
            }
            query2 = sign_request(secret_key, params)
            url = f"{base_url}/fapi/v1/leverage?{query2}"

            resp = requests.post(url, headers=headers, timeout=10)
            result_json = resp.json()

            return {
                "alias": alias,
                "symbol": symbol,
                "position_side": position_side,
                "has_position": True,
                "status_code": resp.status_code,
                "result": result_json
            }

        except Exception as e:
            return {"alias": alias, "error": str(e)}

    # 并发执行
    with ThreadPoolExecutor(max_workers=min(10, len(active_users))) as executor:
        futures = [executor.submit(modify_for_user, u) for u in active_users]
        for f in as_completed(futures):
            results.append(f.result())

    # 汇总结果
    success_count = sum(1 for r in results if r.get("status_code") == 200)
    failed_count = len(results) - success_count

    return jsonify({
        "success": True,
        "data": {
            "symbol": symbol,
            "position_side": position_side,
            "new_leverage": int(new_leverage),
            "user_count": len(active_users),
            "modified_count": success_count,
            "failed_count": failed_count,
            "results": results,
            "timestamp": int(time.time() * 1000)
        }
    })



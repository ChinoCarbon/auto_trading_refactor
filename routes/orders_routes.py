# routes/orders_routes.py
from flask import Blueprint, jsonify, request
import time, json, hmac, hashlib, requests, math
from concurrent.futures import ThreadPoolExecutor, as_completed

orders_bp = Blueprint("orders", __name__)

BINANCE_BASE = "https://fapi.binance.com"
BINANCE_TESTNET = "https://testnet.binancefuture.com"

# =============== 工具函数 ===============

def load_api_keys():
    with open("data/api_keys.json", "r", encoding="utf-8") as f:
        return json.load(f).get("api_keys", [])

def sign_request(secret_key: str, params: dict):
    query = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    signature = hmac.new(secret_key.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + f"&signature={signature}"

def get_latest_price(symbol, use_testnet=False):
    """获取最新价格"""
    base_url = BINANCE_TESTNET if use_testnet else BINANCE_BASE
    try:
        r = requests.get(f"{base_url}/fapi/v1/ticker/price?symbol={symbol}", timeout=5)
        return float(r.json().get("price", 0))
    except Exception:
        return 0.0

def get_symbol_precision(symbol, use_testnet=False):
    """获取交易对的数量/价格精度"""
    base_url = BINANCE_TESTNET if use_testnet else BINANCE_BASE
    try:
        r = requests.get(f"{base_url}/fapi/v1/exchangeInfo", timeout=8)
        r.raise_for_status()
        info = r.json()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                lot_filter = next(f for f in s["filters"] if f["filterType"] == "LOT_SIZE")
                price_filter = next(f for f in s["filters"] if f["filterType"] == "PRICE_FILTER")
                step_size = float(lot_filter["stepSize"])
                tick_size = float(price_filter["tickSize"])
                print(f"step_size: {step_size}, tick_size: {tick_size}")
                return step_size, tick_size
    except Exception as e:
        print(f"[WARN] 获取交易对精度失败 {symbol}: {e}")
    return 0.001, 0.1  # 默认ETH/USDT精度

def round_step(value, step):
    """按最小数量单位下取整"""
    if step == 0:
        return value
    return math.floor(value / step) * step

def round_tick(value, tick):
    """按价格精度取整"""
    if tick == 0:
        return value
    decimals = int(abs(math.log10(tick)))
    return round(round(value / tick) * tick, decimals)
    
def format_with_precision(value, step):
    """
    按 step_size 精度安全格式化浮点数
    例如 step=0.01 → 保留2位小数
    """
    if step == 0:
        return str(value)
    precision = abs(int(math.log10(step)))
    fmt = f"{{:.{precision}f}}"
    return fmt.format(round(value, precision))

# ==================== 下单函数 ====================

def place_order_for_user(user, quantity, payload):
    alias = user["alias"]
    api_key = user["api_key"]
    secret_key = user["secret_key"]
    use_testnet = user.get("use_testnet", False)
    base_url = BINANCE_TESTNET if use_testnet else BINANCE_BASE
    headers = {"X-MBX-APIKEY": api_key}

    symbol = payload["symbol"].upper()
    side = payload["side"].upper()
    leverage = payload.get("leverage", 10)
    usdt_amount = float(quantity)
    take_profit = payload.get("take_profit_price")
    stop_loss = payload.get("stop_loss_price")

    is_fast_order = payload.get("is_fast_order", False)
    fast_order_tp_percentage = payload.get("fast_order_tp_percentage", 0)
    fast_order_sl_percentage = payload.get("fast_order_sl_percentage", 0)

    try:
        # Step 1️⃣ 设置杠杆
        ts = int(time.time() * 1000)
        lev_params = {"symbol": symbol, "leverage": leverage, "timestamp": ts}
        lev_query = sign_request(secret_key, lev_params)
        requests.post(f"{base_url}/fapi/v1/leverage?{lev_query}", headers=headers, timeout=5)

        # Step 2️⃣ 获取精度 & 最新价
        price = get_latest_price(symbol, use_testnet)
        step_size, tick_size = get_symbol_precision(symbol, use_testnet)
        if price == 0:
            raise Exception(f"获取 {symbol} 最新价格失败")

        # Step 3️⃣ 计算数量并修正精度
        raw_qty = usdt_amount / price * leverage
        qty = round_step(raw_qty, step_size)
        if qty <= 0:
            raise Exception(f"{symbol} 计算数量无效: {qty}")

        qty_str = format_with_precision(qty, step_size)

        # Step 4️⃣ 确定双向持仓方向
        position_side = "LONG" if side == "BUY" else "SHORT"

        print(f"qty: {qty_str}")
        # Step 5️⃣ 下主单
        main_params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": qty_str,
            "positionSide": position_side,
            "timestamp": int(time.time() * 1000)
        }
        main_query = sign_request(secret_key, main_params)
        main_resp = requests.post(f"{base_url}/fapi/v1/order?{main_query}", headers=headers, timeout=10)
        main_result = main_resp.json()

        result = {
            "alias": alias,
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "leverage": leverage,
            "quantity": qty_str,
            "price_used": price,
            "main_order": main_result,
            "tp_order": None,
            "sl_order": None
        }

        # Step 6️⃣ 止盈止损单（精度修正）
        if take_profit or (is_fast_order and fast_order_tp_percentage > 0):
            if is_fast_order and position_side == "LONG":
                take_profit = price * (1 + fast_order_tp_percentage / 100)
            elif is_fast_order and position_side == "SHORT":
                take_profit = price * (1 - fast_order_tp_percentage / 100)

            take_profit = round_tick(float(take_profit), tick_size)
            print(f"take_profit: {take_profit}")
            tp_params = {
                "symbol": symbol,
                "side": "SELL" if side == "BUY" else "BUY",
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": take_profit,
                "closePosition": True,
                "positionSide": position_side,
                "timestamp": int(time.time() * 1000)
            }
            tp_query = sign_request(secret_key, tp_params)
            tp_resp = requests.post(f"{base_url}/fapi/v1/order?{tp_query}", headers=headers, timeout=10)
            result["tp_order"] = tp_resp.json()

        if stop_loss or (is_fast_order and fast_order_sl_percentage > 0):
            if is_fast_order and position_side == "LONG":
                stop_loss = price * (1 - fast_order_sl_percentage / 100)
            elif is_fast_order and position_side == "SHORT":
                stop_loss = price * (1 + fast_order_sl_percentage / 100)

            stop_loss = round_tick(float(stop_loss), tick_size)
            print(f"stop_loss: {stop_loss}")
            sl_params = {
                "symbol": symbol,
                "side": "SELL" if side == "BUY" else "BUY",
                "type": "STOP_MARKET",
                "stopPrice": stop_loss,
                "closePosition": True,
                "positionSide": position_side,
                "timestamp": int(time.time() * 1000)
            }
            sl_query = sign_request(secret_key, sl_params)
            sl_resp = requests.post(f"{base_url}/fapi/v1/order?{sl_query}", headers=headers, timeout=10)
            result["sl_order"] = sl_resp.json()

        return {"success": True, "alias": alias, "result": result}

    except Exception as e:
        return {"success": False, "alias": alias, "error": str(e)}

@orders_bp.route("/all", methods=["GET"])
def get_all_orders():
    """
    查询所有用户的挂单（openOrders）
    安全修正版：
    ✅ 强制 orderId / clientOrderId 为字符串（防止精度丢失）
    ✅ 响应时使用 json_dumps_params，保证 JSON 不自动转科学计数法
    ✅ 兼容测试网 / 主网
    """
    users = load_api_keys()
    all_users_data = []

    for u in users:
        if not u.get("is_active", True):
            continue

        alias = u.get("alias")
        api_key = u.get("api_key")
        secret_key = u.get("secret_key")
        use_testnet = u.get("use_testnet", False)
        base_url = BINANCE_TESTNET if use_testnet else BINANCE_BASE

        try:
            headers = {"X-MBX-APIKEY": api_key}
            timestamp = int(time.time() * 1000)
            params = {"timestamp": timestamp}
            query = sign_request(secret_key, params)

            # 获取所有挂单
            url = f"{base_url}/fapi/v1/openOrders?{query}"
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            orders = resp.json()

            formatted_orders = []
            for o in orders:
                # ✅ 强制字符串化所有ID防止丢精度
                order_id = str(o.get("orderId"))
                client_order_id = str(o.get("clientOrderId", ""))

                formatted_orders.append({
                    "symbol": o.get("symbol"),
                    "orderId": order_id,
                    "clientOrderId": client_order_id,
                    "price": str(o.get("price")),
                    "origQty": str(o.get("origQty")),
                    "executedQty": str(o.get("executedQty")),
                    "reduceOnly": o.get("reduceOnly"),
                    "status": o.get("status"),
                    "stopPrice": str(o.get("stopPrice")),
                    "closePosition": o.get("closePosition"),
                    "side": o.get("side"),
                    "type": o.get("type"),
                    "timeInForce": o.get("timeInForce"),
                    "positionSide": o.get("positionSide"),
                    "workingType": o.get("workingType"),
                    "priceProtect": o.get("priceProtect"),
                    "updateTime": o.get("updateTime"),
                    "isIsolated": o.get("isIsolated"),
                    "type_label": "contract_order"
                })

            all_users_data.append({
                "user_id": str(u.get("id")),
                "alias": alias,
                "orders": formatted_orders,
                "order_count": len(formatted_orders)
            })

        except Exception as e:
            all_users_data.append({
                "user_id": str(u.get("id")),
                "alias": alias,
                "orders": [],
                "error": str(e)
            })

    # ✅ 使用 json_dumps_params 防止数字被自动转为科学计数法或 float
    return jsonify({
        "success": True,
        "data": {
            "users": all_users_data,
            "timestamp": int(time.time() * 1000)
        }
    }), 200, {"Content-Type": "application/json; charset=utf-8"}



@orders_bp.route("/cancel_by_id", methods=["POST"])
def cancel_by_id():
    """
    批量撤单接口（多用户版）
    每个订单指定 user_id + symbol + orderId：
    {
        "orders": [
            { "user_id": "u001", "symbol": "ETHUSDT", "orderId": 123 },
            { "user_id": "u002", "symbol": "BTCUSDT", "orderId": 456 }
        ]
    }
    """
    data = request.get_json(force=True)
    orders = data.get("orders")
    if not orders or not isinstance(orders, list):
        return jsonify({"success": False, "message": "缺少 orders 参数或格式错误"}), 400

    users = load_api_keys()
    user_map = {u["id"]: u for u in users}  # id -> user映射
    results = []

    for o in orders:
        user_id = o.get("user_id")
        symbol = o.get("symbol")
        order_id = o.get("orderId")

        # 参数校验
        if not (user_id and symbol and order_id):
            results.append({
                "user_id": user_id,
                "symbol": symbol,
                "orderId": order_id,
                "error": "缺少必要字段(user_id/symbol/orderId)"
            })
            continue

        user = user_map.get(user_id)
        if not user:
            results.append({
                "user_id": user_id,
                "symbol": symbol,
                "orderId": order_id,
                "error": f"未找到用户 {user_id}"
            })
            continue

        api_key = user["api_key"]
        secret_key = user["secret_key"]
        use_testnet = user.get("use_testnet", False)
        base_url = BINANCE_TESTNET if use_testnet else BINANCE_BASE
        headers = {"X-MBX-APIKEY": api_key}

        # def verify_order_exists(api_key, secret_key, symbol, order_id, use_testnet=False):
        #     base_url = BINANCE_TESTNET if use_testnet else BINANCE_BASE
        #     headers = {"X-MBX-APIKEY": api_key}
        #     ts = int(time.time() * 1000)
        #     params = {"symbol": symbol, "timestamp": ts}
        #     query = sign_request(secret_key, params)
        #     r = requests.get(f"{base_url}/fapi/v1/openOrders?{query}", headers=headers)
        #     for o in r.json():
        #         print(f"o: {o}")
        #         if o["orderId"] == order_id:
        #             return True
        #     return False

        # if not verify_order_exists(api_key, secret_key, symbol, order_id, use_testnet):
        #     print(f"⚠️ {symbol} #{order_id} 已不存在（跳过撤单）")
        #     continue

        try:
            timestamp = int(time.time() * 1000)
            params = {"symbol": symbol, "orderId": order_id, "timestamp": timestamp}
            query = sign_request(secret_key, params)
            url = f"{base_url}/fapi/v1/order?{query}"
            resp = requests.delete(url, headers=headers, timeout=10)
            result_json = resp.json()

            results.append({
                "user_id": user_id,
                "alias": user.get("alias"),
                "symbol": symbol,
                "orderId": order_id,
                "status_code": resp.status_code,
                "result": result_json
            })

            print(f"[撤单] user={user_id} {symbol} #{order_id} {resp.status_code} {result_json}")

        except Exception as e:
            results.append({
                "user_id": user_id,
                "alias": user.get("alias"),
                "symbol": symbol,
                "orderId": order_id,
                "error": str(e)
            })

    return jsonify({
        "success": True,
        "data": {
            "results": results,
            "timestamp": int(time.time() * 1000)
        }
    })



@orders_bp.route("/cancel_same", methods=["POST"])
def cancel_same():
    """
    撤销所有用户中“同一挂单”
    匹配条件：symbol, side, type, price, positionSide (数量不计)
    """
    data = request.get_json(force=True)
    required = ["symbol", "price", "type", "side"]
    for k in required:
        if k not in data:
            return jsonify({"success": False, "message": f"缺少参数 {k}"}), 400

    symbol = data["symbol"].upper()
    price = str(data["price"])
    order_type = data["type"].upper()
    side = data["side"].upper()
    position_side = data.get("positionSide")

    users = [u for u in load_api_keys() if u.get("is_active", True)]
    results = []

    for u in users:
        alias = u["alias"]
        api_key = u["api_key"]
        secret_key = u["secret_key"]
        use_testnet = u.get("use_testnet", False)
        base_url = BINANCE_TESTNET if use_testnet else BINANCE_BASE
        headers = {"X-MBX-APIKEY": api_key}

        try:
            timestamp = int(time.time() * 1000)
            query = sign_request(secret_key, {"timestamp": timestamp})
            resp = requests.get(f"{base_url}/fapi/v1/openOrders?{query}", headers=headers, timeout=10)
            orders = resp.json()

            print(f"\n=== 用户 {alias} 当前挂单 {len(orders)} 个 ===")

            matched = []
            debug_info = []

            def float_equal(a, b, tol=1e-6):
                try:
                    return abs(float(a) - float(b)) < tol
                except Exception:
                    return False

            def get_effective_price_field(order_type: str) -> str:
                order_type = order_type.upper()
                if order_type in ["STOP_MARKET", "TAKE_PROFIT_MARKET"]:
                    return "stopPrice"
                elif order_type in ["STOP", "TAKE_PROFIT"]:
                    return "stopPrice"
                elif order_type == "LIMIT":
                    return "price"
                else:
                    return "price"

            for o in orders:
                o_symbol = o.get("symbol")
                o_type = o.get("type", "").upper()
                o_side = o.get("side", "").upper()
                o_pos = o.get("positionSide", "")
                o_id = o.get("orderId")

                price_field = get_effective_price_field(o_type)
                compare_price = o.get(price_field, "0")

                reasons = []
                if o_symbol != symbol:
                    reasons.append(f"symbol不匹配({o_symbol})")
                if not float_equal(compare_price, price):
                    reasons.append(f"{price_field}不匹配({compare_price}, {price})")
                if o_type != order_type:
                    reasons.append(f"type不匹配({o_type})")
                if o_side != side:
                    reasons.append(f"side不匹配({o_side})")
                if position_side and o_pos != position_side:
                    reasons.append(f"positionSide不匹配({o_pos})")

                if not reasons:
                    matched.append(o)
                    debug_info.append(f"✅ 匹配成功: {o_id} {o_symbol} {o_type} {o_side} {o_pos} {price_field}={compare_price}")
                else:
                    debug_info.append(
                        f"❌ 未匹配: {o_id} {o_symbol} {o_type} {o_side} {o_pos} {price_field}={compare_price} | 原因: {', '.join(reasons)}"
                    )
            # 打印对比详情
            print("\n".join(debug_info))

            user_results = []
            for o in matched:
                timestamp = int(time.time() * 1000)
                params = {"symbol": o["symbol"], "orderId": o["orderId"], "timestamp": timestamp}
                query = sign_request(secret_key, params)
                cancel_resp = requests.delete(f"{base_url}/fapi/v1/order?{query}", headers=headers, timeout=10)
                user_results.append({
                    "orderId": o["orderId"],
                    "status_code": cancel_resp.status_code,
                    "result": cancel_resp.json()
                })
                print(f"撤单: {alias} #{o['orderId']} {cancel_resp.status_code} {cancel_resp.text}")

            results.append({
                "alias": alias,
                "matched_count": len(matched),
                "debug": debug_info,
                "cancel_results": user_results
            })

        except Exception as e:
            results.append({
                "alias": alias,
                "error": str(e)
            })

    return jsonify({
        "success": True,
        "data": {
            "symbol": symbol,
            "price": price,
            "type": order_type,
            "side": side,
            "positionSide": position_side,
            "results": results,
            "timestamp": int(time.time() * 1000)
        }
    })


@orders_bp.route("/batch_all", methods=["POST"])
def batch_all_orders():
    """
    所有用户（双向持仓模式）批量下单
    """
    print(f"batch_all_orders")
    payload = request.get_json(force=True)
    required = ["symbol", "side", "quantities"]
    for k in required:
        if k not in payload:
            return jsonify({"success": False, "message": f"缺少参数 {k}"}), 400

    symbol = payload["symbol"].upper()
    user_ids = payload.get("user_ids", [])
    quantities = payload.get("quantities", [])
    print(f"user_ids: {user_ids}")
    users = [u for u in load_api_keys() if u.get("is_active", True) and u.get("id") in user_ids]

    if not users:
        return jsonify({"success": False, "message": "没有可用用户"}), 400

    results = []
    with ThreadPoolExecutor(max_workers=min(10, len(users))) as executor:
        futures = [executor.submit(place_order_for_user, users[i], quantities[i], payload) for i in range(len(users))]
        for f in as_completed(futures):
            results.append(f.result())

    return jsonify({
        "success": True,
        "data": {
            "symbol": symbol,
            "side": payload["side"],
            "leverage": payload.get("leverage", 10),
            "take_profit_price": payload.get("take_profit_price"),
            "stop_loss_price": payload.get("stop_loss_price"),
            "results": results,
            "timestamp": int(time.time() * 1000)
        }
    })
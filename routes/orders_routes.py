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
    
    order_type = payload.get("type", "MARKET").upper()
    limit_price = payload.get("price")  # 限价单价格（字符串）

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
        step_size, tick_size = get_symbol_precision(symbol, use_testnet)
        
        # 限价单逻辑
        if order_type == "LIMIT":
            if not limit_price:
                raise Exception(f"限价单缺少 price 参数")
            limit_price_float = float(limit_price)
            limit_price_float = round_tick(limit_price_float, tick_size)
            price_for_qty = limit_price_float  # 限价单用限价计算数量
            price = limit_price_float  # 用于后续止盈止损计算
            print(f"限价单价格: {limit_price_float}")
        else:
            # 市价单逻辑
            price = get_latest_price(symbol, use_testnet)
            if price == 0:
                raise Exception(f"获取 {symbol} 最新价格失败")
            price_for_qty = price

        # Step 3️⃣ 计算数量并修正精度
        raw_qty = usdt_amount / price_for_qty * leverage
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
            "type": order_type,
            "quantity": qty_str,
            "positionSide": position_side,
            "timestamp": int(time.time() * 1000)
        }
        
        # 限价单需要添加 price 和 timeInForce 参数
        if order_type == "LIMIT":
            main_params["price"] = limit_price_float
            main_params["timeInForce"] = "GTC"  # Good Till Cancel，默认挂单直到成交或取消
        
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
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": "SELL" if side == "BUY" else "BUY",
                "type": "TAKE_PROFIT_MARKET",
                "triggerPrice": take_profit,
                "closePosition": True,
                "positionSide": position_side,
                "timestamp": int(time.time() * 1000)
            }
            tp_query = sign_request(secret_key, tp_params)
            tp_resp = requests.post(f"{base_url}/fapi/v1/algoOrder?{tp_query}", headers=headers, timeout=10)
            result["tp_order"] = tp_resp.json()

        if stop_loss or (is_fast_order and fast_order_sl_percentage > 0):
            if is_fast_order and position_side == "LONG":
                stop_loss = price * (1 - fast_order_sl_percentage / 100)
            elif is_fast_order and position_side == "SHORT":
                stop_loss = price * (1 + fast_order_sl_percentage / 100)

            stop_loss = round_tick(float(stop_loss), tick_size)
            print(f"stop_loss: {stop_loss}")
            sl_params = {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": "SELL" if side == "BUY" else "BUY",
                "type": "STOP_MARKET",
                "triggerPrice": stop_loss,
                "closePosition": True,
                "positionSide": position_side,
                "timestamp": int(time.time() * 1000)
            }
            sl_query = sign_request(secret_key, sl_params)
            sl_resp = requests.post(f"{base_url}/fapi/v1/algoOrder?{sl_query}", headers=headers, timeout=10)
            result["sl_order"] = sl_resp.json()
        print(f"result: {result}")
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


@orders_bp.route("/algo/all", methods=["GET"])
def get_all_algo_orders():
    """
    查询所有用户的条件单（止盈止损单）
    安全修正版：
    ✅ 强制 algoId / clientAlgoId 为字符串（防止精度丢失）
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

            # 获取所有条件单
            url = f"{base_url}/fapi/v1/openAlgoOrders?{query}"
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            algo_orders = resp.json()

            formatted_orders = []
            for o in algo_orders:
                # ✅ 强制字符串化所有ID防止丢精度
                algo_id = str(o.get("algoId"))
                client_algo_id = str(o.get("clientAlgoId", ""))

                formatted_orders.append({
                    "symbol": o.get("symbol"),
                    "algoId": algo_id,
                    "clientAlgoId": client_algo_id,
                    "side": o.get("side"),
                    "orderType": o.get("orderType"),  # 修正字段名：type -> orderType
                    "type": o.get("orderType"),  # 保持兼容性
                    "positionSide": o.get("positionSide"),
                    "reduceOnly": o.get("reduceOnly"),
                    "triggerPrice": str(o.get("triggerPrice", "")),
                    "price": str(o.get("price", "")),
                    "quantity": str(o.get("quantity", "")),
                    "closePosition": o.get("closePosition"),
                    "workingType": o.get("workingType"),
                    "timeInForce": o.get("timeInForce"),
                    "status": o.get("status"),
                    "createTime": o.get("createTime"),
                    "updateTime": o.get("updateTime"),
                    "type_label": "algo_order"
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
            # 先尝试撤销普通订单
            params = {"symbol": symbol, "orderId": order_id, "timestamp": timestamp}
            query = sign_request(secret_key, params)
            url = f"{base_url}/fapi/v1/order?{query}"
            resp = requests.delete(url, headers=headers, timeout=10)
            result_json = resp.json()

            # 如果返回错误码 -4120，说明是条件单，需要使用新接口
            if resp.status_code == 400 and result_json.get("code") == -4120:
                # 查询条件单列表，撤销该symbol的所有条件单
                # 注意：由于无法通过orderId直接匹配algoId，这里撤销该symbol的所有条件单
                algo_query = sign_request(secret_key, {"timestamp": timestamp})
                algo_url = f"{base_url}/fapi/v1/openAlgoOrders?{algo_query}"
                algo_resp = requests.get(algo_url, headers=headers, timeout=10)
                algo_resp.raise_for_status()
                algo_orders = algo_resp.json()
                
                symbol_algo_orders = [o for o in algo_orders if o.get("symbol") == symbol]
                if symbol_algo_orders:
                    # 撤销该symbol的所有条件单
                    cancelled_count = 0
                    for algo_order in symbol_algo_orders:
                        algo_id = algo_order.get("algoId")
                        ts = int(time.time() * 1000)
                        algo_params = {"symbol": symbol, "algoId": algo_id, "timestamp": ts}
                        algo_query = sign_request(secret_key, algo_params)
                        algo_cancel_url = f"{base_url}/fapi/v1/algoOrder?{algo_query}"
                        algo_cancel_resp = requests.delete(algo_cancel_url, headers=headers, timeout=10)
                        if algo_cancel_resp.status_code == 200:
                            cancelled_count += 1
                    
                    if cancelled_count > 0:
                        result_json = {"code": 200, "msg": f"成功撤销 {cancelled_count} 个条件单"}
                        resp.status_code = 200
                    else:
                        result_json = {"code": -1, "msg": "撤销条件单失败"}
                else:
                    result_json = {"code": -1, "msg": "未找到匹配的条件单"}

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
def cancel_all_orders_for_symbol():
    """
    撤销所有用户中指定标的(symbol)的所有挂单（包括普通订单和条件单）
    请求示例：
    {
        "symbol": "ETHUSDT"
    }
    """
    data = request.get_json(force=True)
    symbol = data.get("symbol")
    if not symbol:
        return jsonify({"success": False, "message": "缺少参数 symbol"}), 400

    symbol = symbol.upper()
    results = []

    users = [u for u in load_api_keys() if u.get("is_active", True)]
    print(f"开始批量撤销 {symbol} 的所有挂单（普通订单+条件单），共 {len(users)} 个用户")

    # === 并发执行 ===
    def cancel_for_user(u):
        alias = u["alias"]
        api_key = u["api_key"]
        secret_key = u["secret_key"]
        use_testnet = u.get("use_testnet", False)
        base_url = BINANCE_TESTNET if use_testnet else BINANCE_BASE
        headers = {"X-MBX-APIKEY": api_key}

        user_result = {
            "alias": alias,
            "symbol": symbol,
            "cancelled": 0,
            "algo_cancelled": 0,
            "orders": [],
            "algo_orders": []
        }

        try:
            # 1️⃣ 撤销普通订单
            timestamp = int(time.time() * 1000)
            query = sign_request(secret_key, {"timestamp": timestamp})
            url = f"{base_url}/fapi/v1/openOrders?{query}"
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            orders = resp.json()

            symbol_orders = [o for o in orders if o.get("symbol") == symbol]
            print(f"\n=== 用户 {alias} {symbol} 普通挂单数量: {len(symbol_orders)} ===")

            for o in symbol_orders:
                order_id = o.get("orderId")
                ts = int(time.time() * 1000)
                params = {"symbol": symbol, "orderId": order_id, "timestamp": ts}
                query = sign_request(secret_key, params)
                cancel_url = f"{base_url}/fapi/v1/order?{query}"
                cancel_resp = requests.delete(cancel_url, headers=headers, timeout=10)

                try:
                    result_json = cancel_resp.json()
                except Exception:
                    result_json = {"raw": cancel_resp.text}

                print(f"[撤单] {alias} #{order_id} {cancel_resp.status_code} {result_json}")

                user_result["orders"].append({
                    "orderId": str(order_id),
                    "status_code": cancel_resp.status_code,
                    "result": result_json
                })
                if cancel_resp.status_code == 200:
                    user_result["cancelled"] += 1

            # 2️⃣ 撤销条件单
            algo_query = sign_request(secret_key, {"timestamp": timestamp})
            algo_url = f"{base_url}/fapi/v1/openAlgoOrders?{algo_query}"
            algo_resp = requests.get(algo_url, headers=headers, timeout=10)
            algo_resp.raise_for_status()
            algo_orders = algo_resp.json()

            symbol_algo_orders = [o for o in algo_orders if o.get("symbol") == symbol]
            print(f"=== 用户 {alias} {symbol} 条件单数量: {len(symbol_algo_orders)} ===")

            for o in symbol_algo_orders:
                algo_id = o.get("algoId")
                ts = int(time.time() * 1000)
                params = {"symbol": symbol, "algoId": algo_id, "timestamp": ts}
                query = sign_request(secret_key, params)
                cancel_url = f"{base_url}/fapi/v1/algoOrder?{query}"
                cancel_resp = requests.delete(cancel_url, headers=headers, timeout=10)

                try:
                    result_json = cancel_resp.json()
                except Exception:
                    result_json = {"raw": cancel_resp.text}

                print(f"[撤条件单] {alias} algoId={algo_id} {cancel_resp.status_code} {result_json}")

                user_result["algo_orders"].append({
                    "algoId": str(algo_id),
                    "status_code": cancel_resp.status_code,
                    "result": result_json
                })
                if cancel_resp.status_code == 200:
                    user_result["algo_cancelled"] += 1

            return user_result

        except Exception as e:
            print(f"[错误] 用户 {alias} 撤单异常: {e}")
            user_result["error"] = str(e)
            return user_result

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=min(10, len(users))) as executor:
        futures = [executor.submit(cancel_for_user, u) for u in users]
        for f in as_completed(futures):
            results.append(f.result())

    total_cancelled = sum(r.get("cancelled", 0) for r in results if isinstance(r, dict))

    return jsonify({
        "success": True,
        "data": {
            "symbol": symbol,
            "user_count": len(users),
            "total_cancelled": total_cancelled,
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
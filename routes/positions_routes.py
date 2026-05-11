from flask import Blueprint, jsonify, request
import time, json, hmac, hashlib, requests, math
from concurrent.futures import ThreadPoolExecutor, as_completed
from utils.binance_precision import get_symbol_step_size, adjust_to_step_decimal
from decimal import Decimal, ROUND_DOWN
from services import paper_trading as paper

positions_bp = Blueprint("positions", __name__)

BINANCE_BASE = "https://fapi.binance.com"
BINANCE_TESTNET = "https://testnet.binancefuture.com"

def load_api_keys():
    with open("data/api_keys.json", "r", encoding="utf-8") as f:
        return json.load(f).get("api_keys", [])

def sign_request(secret_key: str, params: dict):
    query = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    signature = hmac.new(secret_key.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + f"&signature={signature}"

@positions_bp.route("/all", methods=["GET"])
def get_all_positions():
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
            if paper.is_paper_user(u):
                wallet_rows, contract_rows = paper.build_positions_view(u["id"])
                row = {
                    "user_id": u.get("id"),
                    "alias": alias,
                    "positions": wallet_rows + contract_rows,
                }
                if request.args.get("include_paper_history", "").lower() == "true":
                    row["paper_history"] = paper.get_history(u["id"], operations_limit=400)
                all_users_data.append(row)
                continue

            headers = {"X-MBX-APIKEY": api_key}
            timestamp = int(time.time() * 1000)

            # ======== 钱包资产 ========
            acc_params = {"timestamp": timestamp}
            acc_query = sign_request(secret_key, acc_params)
            acc_resp = requests.get(f"{base_url}/fapi/v3/account?{acc_query}", headers=headers, timeout=10)
            acc_resp.raise_for_status()
            acc_data = acc_resp.json()

            wallet_positions = []
            for asset in acc_data.get("assets", []):
                if asset.get("asset") == "USDT":
                    wallet_positions.append({
                        "type": "wallet",
                        "asset": asset.get("asset"),
                        "availableBalance": asset.get("availableBalance", "0"),
                        "walletBalance": asset.get("walletBalance", "0"),
                        "crossWalletBalance": asset.get("crossWalletBalance", "0"),
                        "marginBalance": asset.get("marginBalance", "0"),
                        "unrealizedProfit": asset.get("unrealizedProfit", "0"),
                        "crossUnPnl": asset.get("crossUnPnl", "0"),
                        "initialMargin": asset.get("positionInitialMargin", "0"),
                        "maintMargin": asset.get("maintMargin", "0")
                    })

            # ======== 合约仓位 ========
            pos_params = {"timestamp": int(time.time() * 1000)}
            pos_query = sign_request(secret_key, pos_params)
            pos_resp = requests.get(f"{base_url}/fapi/v2/positionRisk?{pos_query}", headers=headers, timeout=10)
            pos_resp.raise_for_status()
            pos_data = pos_resp.json()

            contract_positions = []

            for p in pos_data:
                amt = float(p.get("positionAmt", 0))
                if amt == 0:
                    continue

                notional = abs(float(p.get("notional", 0)))
                leverage = float(p.get("leverage", 0)) if float(p.get("leverage", 0)) > 0 else 1
                mark_price = float(p.get("markPrice", 0))
                maint_margin_rate = 0.004  # 默认0.4%，后面可查精确表

                # ======= 计算保证金 =======
                initial_margin = notional / leverage if leverage > 0 else 0
                maint_margin = notional * maint_margin_rate

                contract_positions.append({
                    "type": "contract",
                    "symbol": p.get("symbol"),
                    "positionSide": p.get("positionSide"),
                    "positionAmt": p.get("positionAmt"),
                    "notional": p.get("notional"),
                    "initialMargin": f"{initial_margin:.8f}",       # ✅ 计算后填充
                    "isolatedMargin": p.get("isolatedMargin", "0"),
                    "isolatedWallet": p.get("isolatedWallet", "0"),
                    "unrealizedProfit": p.get("unRealizedProfit", p.get("unrealizedProfit", "0")),
                    "markPrice": f"{mark_price:.8f}",
                    "maintMargin": f"{maint_margin:.8f}",            # ✅ 计算后填充
                    "liquidation_price_usdt": p.get("liquidationPrice", 0),
                    "updateTime": p.get("updateTime", None),
                    "price_update_time": None
                })

            # 汇总结果
            all_positions = wallet_positions + contract_positions
            all_users_data.append({
                "user_id": u.get("id"),
                "alias": alias,
                "positions": all_positions
            })

        except Exception as e:
            all_users_data.append({
                "user_id": u.get("id"),
                "alias": alias,
                "positions": [],
                "error": str(e)
            })

    return jsonify({
        "success": True,
        "data": {
            "users": all_users_data,
            "timestamp": int(time.time() * 1000)
        }
    })


@positions_bp.route("/paper/history", methods=["GET"])
def paper_position_history():
    """
    模拟盘（use_testnet）仓位历史：已平仓会话列表，可选附带当前未平仓合约。
    Query:
      user_id（必填）
      symbol（可选）
      position_side（可选 LONG|SHORT）
      limit（默认100，最大500） offset order=desc|asc
      include_open=true 附带当前持仓合约列表（与 /positions/all 中 contract 段形状一致）
    """
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"success": False, "message": "缺少 user_id"}), 400
    u = next((x for x in load_api_keys() if x.get("id") == user_id and x.get("is_active", True)), None)
    if not u:
        return jsonify({"success": False, "message": "用户不存在或未激活"}), 404
    if not paper.is_paper_user(u):
        return jsonify({"success": False, "message": "仅模拟盘用户（use_testnet）可查询仓位历史"}), 400

    ps = request.args.get("position_side")
    if ps and ps.upper() not in ("LONG", "SHORT"):
        return jsonify({"success": False, "message": "position_side 须为 LONG 或 SHORT"}), 400
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 500)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return jsonify({"success": False, "message": "limit/offset 无效"}), 400
    order = request.args.get("order", "desc")
    sym = request.args.get("symbol")
    if sym:
        sym = sym.strip().upper()
    include_open = request.args.get("include_open", "").lower() == "true"

    data = paper.get_position_session_history(
        user_id,
        symbol=sym or None,
        position_side=ps.upper() if ps else None,
        limit=limit,
        offset=offset,
        order=order,
        include_open=include_open,
    )
    return jsonify({
        "success": True,
        "data": {
            **data,
            "user_id": user_id,
            "alias": u.get("alias"),
            "timestamp": int(time.time() * 1000),
        },
    })


@positions_bp.route("/market-close", methods=["POST"])
def market_close_all_users_dual_mode():
    """
    市价平仓接口（并发版，固定双向持仓）
    为所有用户平掉同一币、同方向的仓位
    请求体示例：
    {
        "symbol": "ETHUSDT",
        "position_side": "LONG",
        "close_ratio": 100,
        "use_testnet": false
    }
    """
    data = request.get_json(force=True)
    symbol = data.get("symbol")
    position_side = data.get("position_side", "").upper()
    close_ratio = float(data.get("close_ratio", 100))
    use_testnet = data.get("use_testnet", False)

    if not symbol or position_side not in ["LONG", "SHORT"]:
        return jsonify({"success": False, "msg": "参数错误：symbol或position_side无效"}), 400
    if close_ratio <= 0:
        return jsonify({"success": False, "msg": "close_ratio必须大于0"}), 400

    users = load_api_keys()
    base_url = BINANCE_TESTNET if use_testnet else BINANCE_BASE
    results = []

    def close_user_position(u):
        alias = u.get("alias")
        api_key = u.get("api_key")
        secret_key = u.get("secret_key")
        if not u.get("is_active", True):
            return {"alias": alias, "result": "用户未激活"}

        try:
            if paper.is_paper_user(u):
                r = paper.market_close(u["id"], symbol, position_side, close_ratio)
                if r.get("error"):
                    return {"alias": alias, "symbol": symbol, "position_side": position_side, "error": r["error"]}
                return {
                    "alias": alias,
                    "symbol": symbol,
                    "position_side": position_side,
                    "close_amt": r.get("close_amt"),
                    "status_code": r.get("status_code", 200),
                    "result": r.get("result"),
                }

            headers = {"X-MBX-APIKEY": api_key}

            # 获取仓位列表
            ts = int(time.time() * 1000)
            query = sign_request(secret_key, {"timestamp": ts})
            pos_url = f"{base_url}/fapi/v2/positionRisk?{query}"
            r = requests.get(pos_url, headers=headers, timeout=10)
            r.raise_for_status()
            positions = r.json()

            # 查找目标仓位
            pos = next((p for p in positions if p["symbol"] == symbol and p["positionSide"].upper() == position_side), None)
            if not pos or abs(float(pos["positionAmt"])) < 1e-12:
                return {"alias": alias, "symbol": symbol, "position_side": position_side, "result": "未找到仓位或仓位为0"}

            amt = abs(float(pos["positionAmt"]))
            close_amt = round(amt * (close_ratio / 100.0), 8)
            print(f"close_amt: {close_amt}")

            step_size = get_symbol_step_size(base_url, symbol)
            close_amt = adjust_to_step_decimal(close_amt, step_size)
            print(f"close_amt: {close_amt}")

            if close_amt <= 0:
                return {"alias": alias, "symbol": symbol, "position_side": position_side, "result": "平仓数量为0"}

            # 双向持仓：根据方向选择反向操作
            side = "SELL" if position_side == "LONG" else "BUY"

            order_params = {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": str(close_amt),
                "positionSide": position_side,
                "timestamp": int(time.time() * 1000)
            }

            query2 = sign_request(secret_key, order_params)
            order_url = f"{base_url}/fapi/v1/order?{query2}"
            resp = requests.post(order_url, headers=headers, timeout=10)
            result_json = resp.json()

            return {
                "alias": alias,
                "symbol": symbol,
                "position_side": position_side,
                "close_amt": str(close_amt),
                "status_code": resp.status_code,
                "result": result_json
            }

        except Exception as e:
            return {"alias": alias, "symbol": symbol, "position_side": position_side, "error": str(e)}

    # 并发执行
    with ThreadPoolExecutor(max_workers=min(10, len(users))) as executor:
        futures = [executor.submit(close_user_position, u) for u in users]
        for f in as_completed(futures):
            results.append(f.result())

    return jsonify({
        "success": True,
        "data": {
            "symbol": symbol,
            "position_side": position_side,
            "close_ratio": close_ratio,
            "results": results,
            "timestamp": int(time.time() * 1000)
        }
    })


@positions_bp.route("/tp-sl", methods=["POST"])
def set_take_profit_stop_loss():
    """
    为多个用户批量设置止盈止损单（双向持仓模式，数量单位：USDT）
    """
    data = request.get_json(force=True)
    symbol = data.get("symbol")
    position_side = data.get("position_side", "").upper()
    user_orders = data.get("user_orders", {})
    use_testnet = data.get("use_testnet", False)

    if not symbol or not position_side or not user_orders:
        return jsonify({"success": False, "msg": "缺少参数 symbol / position_side / user_orders"}), 400

    base_url = BINANCE_TESTNET if use_testnet else BINANCE_BASE

    # === 工具函数 ===
    def get_symbol_info(base_url, symbol):
        """获取symbol对应的stepSize、tickSize"""
        try:
            r = requests.get(f"{base_url}/fapi/v1/exchangeInfo", timeout=10)
            data = r.json()
            symbol_info = next((s for s in data["symbols"] if s["symbol"] == symbol), None)
            if not symbol_info:
                return Decimal("0.001"), Decimal("0.1")
            lot_size = next((f for f in symbol_info["filters"] if f["filterType"] == "LOT_SIZE"), None)
            price_filter = next((f for f in symbol_info["filters"] if f["filterType"] == "PRICE_FILTER"), None)
            step = Decimal(lot_size["stepSize"]) if lot_size else Decimal("0.001")
            tick = Decimal(price_filter["tickSize"]) if price_filter else Decimal("0.1")
            return step, tick
        except Exception:
            return Decimal("0.001"), Decimal("0.1")

    def adjust_to_step_decimal(value, step):
        """数量精度修正"""
        value = Decimal(str(value))
        step = Decimal(str(step))
        return (value // step * step).quantize(step, rounding=ROUND_DOWN)

    def get_mark_price(base_url, symbol):
        """获取当前标记价格"""
        try:
            r = requests.get(f"{base_url}/fapi/v1/premiumIndex?symbol={symbol}", timeout=5)
            return Decimal(str(r.json().get("markPrice", "0")))
        except Exception:
            return Decimal("0")

    # === 每个用户逻辑 ===
    def place_tp_sl_for_user(u):
        alias = u.get("alias")
        uid = u.get("id")
        if uid not in user_orders:
            return {"alias": alias, "uid": uid, "result": "未提供订单参数"}

        api_key = u.get("api_key")
        secret_key = u.get("secret_key")
        headers = {"X-MBX-APIKEY": api_key}
        user_cfg = user_orders[uid]

        step_size, tick_size = get_symbol_info(base_url, symbol)
        mark_price = get_mark_price(base_url, symbol)
        if mark_price <= 0:
            return {"alias": alias, "uid": uid, "error": "获取标记价格失败"}

        reverse_side = "SELL" if position_side == "LONG" else "BUY"
        results_list = []

        try:
            if paper.is_paper_user(u):
                step_sz, tick_sz = get_symbol_info(BINANCE_BASE, symbol)
                mark_px = get_mark_price(BINANCE_BASE, symbol)
                if mark_px <= 0:
                    return {"alias": alias, "uid": uid, "error": "获取标记价格失败"}
                tp_kw = {}
                sl_kw = {}
                if user_cfg.get("take_profit_price") and user_cfg.get("take_profit_amount"):
                    tp_price = Decimal(str(user_cfg["take_profit_price"]))
                    tp_usdt = Decimal(str(user_cfg["take_profit_amount"]))
                    tp_qty = adjust_to_step_decimal(tp_usdt / mark_px, step_sz)
                    if tp_qty > 0:
                        tp_kw["take_profit_price"] = str(tp_price.quantize(tick_sz, rounding=ROUND_DOWN))
                        tp_kw["take_profit_qty"] = str(tp_qty)
                if user_cfg.get("stop_loss_price") and user_cfg.get("stop_loss_amount"):
                    sl_price = Decimal(str(user_cfg["stop_loss_price"]))
                    sl_usdt = Decimal(str(user_cfg["stop_loss_amount"]))
                    sl_qty = adjust_to_step_decimal(sl_usdt / mark_px, step_sz)
                    if sl_qty > 0:
                        sl_kw["stop_loss_price"] = str(sl_price.quantize(tick_sz, rounding=ROUND_DOWN))
                        sl_kw["stop_loss_qty"] = str(sl_qty)
                plist = paper.place_tp_sl_orders(
                    u["id"],
                    symbol,
                    position_side,
                    take_profit_price=tp_kw.get("take_profit_price"),
                    take_profit_qty=tp_kw.get("take_profit_qty"),
                    stop_loss_price=sl_kw.get("stop_loss_price"),
                    stop_loss_qty=sl_kw.get("stop_loss_qty"),
                )
                return {"alias": alias, "uid": uid, "orders": plist} if plist else {"alias": alias, "uid": uid, "result": "未挂单"}

            # === 止盈单 ===
            if user_cfg.get("take_profit_price") and user_cfg.get("take_profit_amount"):
                tp_price = Decimal(str(user_cfg["take_profit_price"]))
                tp_usdt = Decimal(str(user_cfg["take_profit_amount"]))

                # ⚙️ USDT → 币数量
                tp_qty = tp_usdt / mark_price
                tp_qty = adjust_to_step_decimal(tp_qty, step_size)

                if tp_qty > 0:
                    tp_params = {
                        "algoType": "CONDITIONAL",
                        "symbol": symbol,
                        "side": reverse_side,
                        "type": "TAKE_PROFIT_MARKET",
                        "triggerPrice": str(tp_price.quantize(tick_size, rounding=ROUND_DOWN)),
                        "positionSide": position_side,
                        "quantity": str(tp_qty),
                        "timestamp": int(time.time() * 1000),
                        "workingType": "CONTRACT_PRICE"
                    }
                    tp_query = sign_request(secret_key, tp_params)
                    tp_url = f"{base_url}/fapi/v1/algoOrder?{tp_query}"
                    tp_resp = requests.post(tp_url, headers=headers, timeout=10)
                    results_list.append({
                        "alias": alias,
                        "order_type": "TAKE_PROFIT",
                        "qty_usdt": str(tp_usdt),
                        "qty_coin": str(tp_qty),
                        "price": str(tp_price),
                        "status_code": tp_resp.status_code,
                        "result": tp_resp.json()
                    })

            # === 止损单 ===
            if user_cfg.get("stop_loss_price") and user_cfg.get("stop_loss_amount"):
                sl_price = Decimal(str(user_cfg["stop_loss_price"]))
                sl_usdt = Decimal(str(user_cfg["stop_loss_amount"]))
                sl_qty = sl_usdt / mark_price
                sl_qty = adjust_to_step_decimal(sl_qty, step_size)

                if sl_qty > 0:
                    sl_params = {
                        "algoType": "CONDITIONAL",
                        "symbol": symbol,
                        "side": reverse_side,
                        "type": "STOP_MARKET",
                        "triggerPrice": str(sl_price.quantize(tick_size, rounding=ROUND_DOWN)),
                        "positionSide": position_side,
                        "quantity": str(sl_qty),
                        "timestamp": int(time.time() * 1000),
                        "workingType": "CONTRACT_PRICE"
                    }
                    sl_query = sign_request(secret_key, sl_params)
                    sl_url = f"{base_url}/fapi/v1/algoOrder?{sl_query}"
                    sl_resp = requests.post(sl_url, headers=headers, timeout=10)
                    results_list.append({
                        "alias": alias,
                        "order_type": "STOP_LOSS",
                        "qty_usdt": str(sl_usdt),
                        "qty_coin": str(sl_qty),
                        "price": str(sl_price),
                        "status_code": sl_resp.status_code,
                        "result": sl_resp.json()
                    })

            return {"alias": alias, "uid": uid, "orders": results_list} if results_list else {"alias": alias, "uid": uid, "result": "未挂单"}

        except Exception as e:
            return {"alias": alias, "uid": uid, "error": str(e)}

    # === 执行所有用户 ===
    all_users = load_api_keys()
    target_users = [u for u in all_users if u.get("id") in user_orders and u.get("is_active", True)]

    if not target_users:
        return jsonify({"success": False, "msg": "没有匹配的用户"}), 400

    results = []
    with ThreadPoolExecutor(max_workers=min(10, len(target_users))) as executor:
        futures = [executor.submit(place_tp_sl_for_user, u) for u in target_users]
        for f in as_completed(futures):
            results.append(f.result())

    return jsonify({
        "success": True,
        "data": {
            "symbol": symbol,
            "position_side": position_side,
            "user_count": len(target_users),
            "results": results,
            "timestamp": int(time.time() * 1000)
        }
    })
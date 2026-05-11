"""
模拟合约交易（use_testnet 用户）：不接币安测试网，本地撮合 + 主网行情/精度。
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

# 展示用固定东八区（不参与交易所时区逻辑）
_TZ_UTC8 = timezone(timedelta(hours=8))

BINANCE_MAINNET = "https://fapi.binance.com"
PAPER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_trading")
_FEE_RATE = 0.0004
_MAX_OPERATIONS = 5000


def _fee_notional(price: float, qty: float, rate: float = _FEE_RATE) -> float:
    return abs(float(price) * float(qty) * rate)


def _ts_iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ts_cst8(ms: int) -> str:
    """操作记录、仓位历史等展示用时间（UTC+8）。"""
    return datetime.fromtimestamp(ms / 1000.0, tz=_TZ_UTC8).strftime("%Y-%m-%d %H:%M:%S")


def _enrich_op_cst8(row: dict) -> dict:
    """补全 time_cst8，供叙述与导出统一用东八区。"""
    out = dict(row)
    ts = int(out.get("ts") or 0)
    if not out.get("time_cst8") and ts:
        out["time_cst8"] = _ts_cst8(ts)
    elif not out.get("time_cst8"):
        out["time_cst8"] = ""
    return out


def _append_operation(state: dict, doc: dict) -> dict:
    seq = _next_id(state, "next_operation_seq")
    op_id = f"op-{seq}"
    now = int(time.time() * 1000)
    d = dict(doc)
    ts = int(d.pop("ts", now))
    row = {"id": op_id, "seq": seq, "ts": ts, "time_cst8": _ts_cst8(ts), **d}
    row["narrative_zh"] = operation_narrative_zh(row)
    state.setdefault("operations", []).append(row)
    if len(state["operations"]) > _MAX_OPERATIONS:
        state["operations"] = state["operations"][-_MAX_OPERATIONS:]
    return row


def _close_reason_zh(code: str | None) -> str:
    if not code:
        return "平仓"
    m = {
        "CONDITIONAL_TAKE_PROFIT": "止盈条件单触发（挂单/条件单平仓）",
        "CONDITIONAL_STOP_LOSS": "止损条件单触发（挂单/条件单平仓）",
        "MARKET_CLOSE_FULL": "市价全平（/market-close 接口）",
        "MARKET_CLOSE_PARTIAL": "市价部分平仓（/market-close 接口）",
        "UNSPECIFIED_CLOSE": "平仓成交",
    }
    return m.get(code, code)


def _algo_ack_summary(ack: dict | None) -> dict:
    if not ack:
        return {"placed": False}
    return {
        "placed": True,
        "algo_id": str(ack.get("algoId", "")),
        "order_type": ack.get("orderType"),
        "trigger_price": str(ack.get("triggerPrice", "")),
        "close_position": bool(ack.get("closePosition")),
        "quantity": str(ack.get("quantity", "") or ""),
    }


def operation_narrative_zh(op: dict) -> str:
    """将单条操作记录转写为中文自然叙述，便于非技术用户阅读。"""
    ev = op.get("event") or "UNKNOWN"
    sym = op.get("symbol") or "未知交易对"
    ps = op.get("position_side") or op.get("positionSide") or ""
    if ps == "LONG":
        ps_w = "多仓（LONG）"
    elif ps == "SHORT":
        ps_w = "空仓（SHORT）"
    else:
        ps_w = ps or "未区分方向"

    ts_ms = int(op.get("ts") or 0)
    t_show = op.get("time_cst8") or (_ts_cst8(ts_ms) if ts_ms else "")
    head = f"{t_show} " if t_show else ""

    if ev == "OPEN_MARKET":
        fill = op.get("fill") or {}
        lev = op.get("leverage", "—")
        tp, sl = op.get("take_profit") or {}, op.get("stop_loss") or {}
        pa = op.get("position_after") or {}
        tp_t = (
            f"同时挂了止盈条件单，触发价约 {tp.get('trigger_price')} USDT（编号 {tp.get('algo_id')}）。"
            if tp.get("placed")
            else "本次没有挂止盈条件单。"
        )
        sl_t = (
            f"同时挂了止损条件单，触发价约 {sl.get('trigger_price')} USDT（编号 {sl.get('algo_id')}）。"
            if sl.get("placed")
            else "本次没有挂止损条件单。"
        )
        return (
            f"{head}您以市价首次开仓：{sym}，{ps_w}，杠杆约 {lev} 倍。"
            f"成交数量 {fill.get('qty', '—')}，参考成交价 {fill.get('price', '—')} USDT，"
            f"大约占用保证金 {fill.get('margin_usdt_est', '—')} USDT。"
            f"{tp_t}{sl_t}"
            f"成交后持仓约 {pa.get('qty', '—')}，持仓均价约 {pa.get('entry_avg', '—')} USDT。"
        )

    if ev == "ADD_MARKET":
        fill = op.get("fill") or {}
        lev = op.get("leverage", "—")
        tp, sl = op.get("take_profit") or {}, op.get("stop_loss") or {}
        pa = op.get("position_after") or {}
        tp_t = (
            f"并新挂止盈（触发价约 {tp.get('trigger_price')}，编号 {tp.get('algo_id')}）。"
            if tp.get("placed")
            else "未新挂止盈。"
        )
        sl_t = (
            f"并新挂止损（触发价约 {sl.get('trigger_price')}，编号 {sl.get('algo_id')}）。"
            if sl.get("placed")
            else "未新挂止损。"
        )
        return (
            f"{head}您以市价加仓：{sym}，{ps_w}，杠杆约 {lev} 倍。"
            f"本次成交 {fill.get('qty', '—')}，价 {fill.get('price', '—')} USDT。"
            f"{tp_t}{sl_t}"
            f"加仓后总持仓约 {pa.get('qty', '—')}，均价约 {pa.get('entry_avg', '—')} USDT。"
        )

    if ev == "OPEN_LIMIT_SUBMIT":
        lp = op.get("limit_pending") or {}
        return (
            f"{head}您提交了限价开仓挂单（尚未成交）：{sym}，{ps_w}，杠杆约 {op.get('leverage', '—')} 倍。"
            f"挂单价格 {lp.get('price', '—')} USDT，数量 {lp.get('qty', '—')}。"
            f"限价单成交前，本次请求不会附带止盈止损；成交后需再单独挂条件单。"
        )

    if ev in ("OPEN_LIMIT_FILLED", "ADD_LIMIT_FILLED"):
        fill = op.get("fill") or {}
        tr = op.get("trigger") or {}
        pa = op.get("position_after") or {}
        act = "限价单首次成交开仓" if ev == "OPEN_LIMIT_FILLED" else "限价单加仓成交"
        return (
            f"{head}{act}：{sym}，{ps_w}。"
            f"成交数量 {fill.get('qty', '—')}，成交价 {fill.get('price', '—')} USDT。"
            f"当时标记价约 {tr.get('mark_price_at_fill', '—')}，限价 {tr.get('limit_price', '—')}。"
            f"成交后持仓约 {pa.get('qty', '—')}，均价约 {pa.get('entry_avg', '—')} USDT。"
            f"（限价成交记录本身不含止盈止损。）"
        )

    if ev == "CLOSE":
        pnl = op.get("realized_pnl_net", "—")
        fee = op.get("fee", "—")
        qty = op.get("qty", "—")
        ep = op.get("entry_price_avg", "—")
        xp = op.get("exit_price", "—")
        reason = op.get("close_reason_zh") or op.get("label_zh") or "平仓"
        full = "已全部平掉该方向仓位。" if op.get("is_full_close") else f"仍有剩余持仓约 {op.get('remaining_qty_after', '—')}。"
        td = op.get("trigger_detail") or {}
        extra = ""
        if td.get("trigger_channel") == "FRONTEND_WEBSOCKET_API":
            extra = "本次由前端根据行情主动请求触发，交易所端未再校验触发价。"
        elif td.get("algo_id"):
            extra = f"关联条件单编号 {td.get('algo_id')}，类型 {td.get('algo_order_type', '—')}，配置触发价 {td.get('trigger_price', '—')}。"
        return (
            f"{head}平仓：{sym}，{ps_w}。"
            f"原因说明：{reason}。"
            f"本次平仓数量 {qty}，持仓均价约 {ep} USDT，平仓价约 {xp} USDT；"
            f"手续费约 {fee} USDT，本次净盈亏约 {pnl} USDT（已扣手续费）。{full}{extra}"
        )

    if ev == "SESSION_CLOSED":
        tot = op.get("total_realized_pnl_net") or op.get("total_realized_pnl") or "—"
        return (
            f"{head}本轮 {sym} {ps_w} 持仓已彻底结束（会话关闭）。"
            f"该段持仓从开仓到全部平完，累计已实现盈亏约 {tot} USDT。"
        )

    if ev == "CLIENT_REQUEST_TRIGGER_ALGO":
        return (
            f"{head}前端发来指令：要求按当前提交的标记价 {op.get('mark_price_submitted', '—')} USDT "
            f"去触发条件单（编号 {op.get('algo_id', '—')}），类型 {op.get('algo_order_type', '—')}，"
            f"合约 {sym}，{ps_w}，原配置触发价 {op.get('configured_trigger_price', '—')}。"
            f"是否触及触发价由前端判断，服务器按提交价格执行后续平仓。"
        )

    if ev == "ATTACH_TP_SL":
        parts = [f"{head}您在持有 {sym} {ps_w} 期间，补充挂了止盈/止损条件单："]
        for x in op.get("orders") or []:
            role = x.get("role", "条件单")
            parts.append(
                f"{role}，触发价约 {x.get('trigger_price', '—')}，数量 {x.get('quantity', '—')}，编号 {x.get('algo_id', '—')}。"
            )
        return "".join(parts) if len(parts) > 1 else (parts[0] + "（无明细）")

    if ev == "CANCEL_OPEN_ORDER":
        return (
            f"{head}您撤销了一张普通/限价挂单：{sym}，订单号 {op.get('order_id', '—')}，"
            f"方向 {op.get('side', '—')}，{ps_w}。"
        )

    if ev == "CANCEL_ALGO_ORDER":
        return (
            f"{head}您撤销了一张条件单：{sym}，编号 {op.get('algo_id', '—')}，"
            f"类型 {op.get('order_type', '—')}，触发价约 {op.get('trigger_price', '—')}，{ps_w}。"
        )

    if ev == "BULK_CANCEL_SYMBOL":
        return (
            f"{head}一键批量撤单：在 {sym} 上共撤销普通挂单 {op.get('cancelled_open_orders', 0)} 笔、"
            f"条件单 {op.get('cancelled_algo_orders', 0)} 笔。"
        )

    if ev == "BULK_CANCEL_ALGO_SYMBOL":
        return (
            f"{head}一键仅撤条件单：在 {sym} 上撤销条件单 {op.get('cancelled_algo_orders', 0)} 笔（普通限价单保留）。"
        )

    label = op.get("label_zh") or ""
    return f"{head}操作记录（类型 {ev}）：{sym} {ps_w}。{label}".strip()


_lock = threading.RLock()


def is_paper_user(user: dict) -> bool:
    return bool(user.get("use_testnet", False))


def _ensure_dir():
    os.makedirs(PAPER_DIR, exist_ok=True)


def _path(user_id: str) -> str:
    _ensure_dir()
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(user_id))
    return os.path.join(PAPER_DIR, f"{safe}.json")


def _default_state() -> dict:
    return {
        "virtual_wallet_usdt": "1000000",
        "positions": {},  # symbol -> { LONG|SHORT: {...} }
        "open_orders": [],
        "algo_orders": [],
        "next_order_id": 1,
        "next_algo_id": 1,
        "next_ledger_id": 1,
        "ledger": [],
        "sessions": [],
        "operations": [],
        "next_operation_seq": 1,
    }


def _load(user_id: str) -> dict:
    p = _path(user_id)
    if not os.path.isfile(p):
        return _default_state()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in _default_state().items():
            if k not in data:
                data[k] = v
        return data
    except Exception:
        return _default_state()


def _save(user_id: str, state: dict):
    p = _path(user_id)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def get_latest_price(symbol: str) -> float:
    try:
        r = requests.get(f"{BINANCE_MAINNET}/fapi/v1/ticker/price?symbol={symbol}", timeout=5)
        return float(r.json().get("price", 0))
    except Exception:
        return 0.0


def get_mark_price(symbol: str) -> float:
    try:
        r = requests.get(f"{BINANCE_MAINNET}/fapi/v1/premiumIndex?symbol={symbol}", timeout=5)
        return float(r.json().get("markPrice", 0))
    except Exception:
        return 0.0


def get_symbol_precision(symbol: str):
    try:
        r = requests.get(f"{BINANCE_MAINNET}/fapi/v1/exchangeInfo", timeout=8)
        r.raise_for_status()
        for s in r.json()["symbols"]:
            if s["symbol"] == symbol:
                lot_filter = next(f for f in s["filters"] if f["filterType"] == "LOT_SIZE")
                price_filter = next(f for f in s["filters"] if f["filterType"] == "PRICE_FILTER")
                return float(lot_filter["stepSize"]), float(price_filter["tickSize"])
    except Exception:
        pass
    return 0.001, 0.1


def round_step(value, step):
    if step == 0:
        return value
    return math.floor(value / step) * step


def round_tick(value, tick):
    if tick == 0:
        return value
    decimals = int(abs(math.log10(tick)))
    return round(round(value / tick) * tick, decimals)


def format_with_precision(value, step):
    if step == 0:
        return str(value)
    precision = abs(int(math.log10(step)))
    fmt = f"{{:.{precision}f}}"
    return fmt.format(round(value, precision))


def _next_id(state: dict, key: str) -> int:
    n = int(state.get(key, 1))
    state[key] = n + 1
    return n


def _pos_cell(state: dict, symbol: str, position_side: str) -> dict | None:
    return state["positions"].get(symbol, {}).get(position_side)


def _set_pos(state: dict, symbol: str, position_side: str, cell: dict | None):
    if symbol not in state["positions"]:
        state["positions"][symbol] = {}
    if cell is None:
        state["positions"][symbol][position_side] = {
            "positionAmt": "0",
            "entryPrice": "0",
            "leverage": 1,
            "firstOpenTime": None,
            "lastUpdateTime": None,
            "session_id": None,
        }
        # prune empty symbol
        if all(float(state["positions"][symbol].get(ps, {}).get("positionAmt", 0) or 0) == 0 for ps in ("LONG", "SHORT")):
            state["positions"].pop(symbol, None)
        return
    state["positions"][symbol][position_side] = cell


def _append_ledger(
    state: dict,
    *,
    symbol: str,
    position_side: str,
    side: str,
    event: str,
    qty: float,
    price: float,
    fee: float,
    realized_pnl: float,
    order_id: str,
    session_id: str | None,
):
    lid = str(_next_id(state, "next_ledger_id"))
    entry = {
        "id": lid,
        "ts": int(time.time() * 1000),
        "symbol": symbol,
        "position_side": position_side,
        "side": side,
        "event": event,
        "qty": str(round(qty, 8)),
        "price": str(price),
        "fee": str(round(fee, 8)),
        "realized_pnl": str(round(realized_pnl, 8)),
        "order_id": order_id,
        "session_id": session_id or "",
    }
    state["ledger"].append(entry)
    # 控制文件体积：保留最近 2000 条
    if len(state["ledger"]) > 2000:
        state["ledger"] = state["ledger"][-2000:]


def _append_closed_session(state: dict, symbol: str, position_side: str, session_id: str | None, opened_at: int | None):
    if not session_id or not opened_at:
        return
    realized_sum = 0.0
    for row in state["ledger"]:
        if row.get("session_id") == session_id and row.get("event") == "REDUCE":
            realized_sum += float(row.get("realized_pnl", 0) or 0)
    closed_ms = int(time.time() * 1000)
    state["sessions"].append(
        {
            "session_id": session_id,
            "symbol": symbol,
            "positionSide": position_side,
            "opened_at": opened_at,
            "closed_at": closed_ms,
            "total_realized_pnl": str(round(realized_sum, 8)),
        }
    )
    _append_operation(
        state,
        {
            "event": "SESSION_CLOSED",
            "label_zh": "持仓会话结束（该方向已完全平仓，可复盘汇总）",
            "category": "session",
            "symbol": symbol,
            "position_side": position_side,
            "session_id": session_id,
            "opened_at_ts": opened_at,
            "closed_at_ts": closed_ms,
            "opened_at_cst8": _ts_cst8(opened_at) if opened_at else "",
            "closed_at_cst8": _ts_cst8(closed_ms),
            "total_realized_pnl_net": str(round(realized_sum, 8)),
        },
    )
    if len(state["sessions"]) > 500:
        state["sessions"] = state["sessions"][-500:]


def _fmt_pos_qty(q: float, qty_step: float | None) -> str:
    if qty_step and qty_step > 0:
        return format_with_precision(round_step(q, qty_step), qty_step)
    return str(round(q, 8))


def _apply_trade(
    state: dict,
    *,
    symbol: str,
    position_side: str,
    side: str,
    qty: float,
    price: float,
    leverage: int,
    order_id: str,
    qty_step: float | None = None,
    close_reason: str | None = None,
    close_detail: dict | None = None,
) -> None:
    """
    position_side: LONG/SHORT
    side: BUY/SELL — 与币安 hedge 一致：BUY+LONG 加多，SELL+LONG 减多；SELL+SHORT 加空，BUY+SHORT 减空。
    """
    cell = _pos_cell(state, symbol, position_side) or {
        "positionAmt": "0",
        "entryPrice": "0",
        "leverage": leverage,
        "firstOpenTime": None,
        "lastUpdateTime": None,
        "session_id": None,
    }
    cur_amt = float(cell["positionAmt"] or 0)
    entry = float(cell["entryPrice"] or 0)

    opening = (side == "BUY" and position_side == "LONG") or (side == "SELL" and position_side == "SHORT")
    closing = (side == "SELL" and position_side == "LONG") or (side == "BUY" and position_side == "SHORT")

    fee = _fee_notional(price, qty)
    now = int(time.time() * 1000)

    if opening:
        if cur_amt < 0:
            raise ValueError("内部错误：开仓方向持仓为负")
        new_amt = cur_amt + qty
        if cur_amt == 0:
            new_entry = price
            session_id = uuid.uuid4().hex[:12]
            first_open = now
        else:
            new_entry = (cur_amt * entry + qty * price) / new_amt if new_amt > 0 else entry
            session_id = cell.get("session_id") or uuid.uuid4().hex[:12]
            first_open = cell.get("firstOpenTime") or now
        cell.update(
            {
                "positionAmt": _fmt_pos_qty(new_amt, qty_step),
                "entryPrice": str(round(new_entry, 8)),
                "leverage": leverage,
                "firstOpenTime": first_open,
                "lastUpdateTime": now,
                "session_id": session_id,
            }
        )
        evt = "OPEN" if cur_amt == 0 else "ADD"
        _append_ledger(
            state,
            symbol=symbol,
            position_side=position_side,
            side=side,
            event=evt,
            qty=qty,
            price=price,
            fee=fee,
            realized_pnl=0.0,
            order_id=order_id,
            session_id=session_id,
        )
        _set_pos(state, symbol, position_side, cell)
        return

    if closing:
        if cur_amt <= 0:
            raise ValueError("无持仓可平")
        close_qty = min(qty, cur_amt)
        if position_side == "LONG":
            realized = (price - entry) * close_qty - fee
        else:
            realized = (entry - price) * close_qty - fee
        new_amt = cur_amt - close_qty
        sid = cell.get("session_id")
        _append_ledger(
            state,
            symbol=symbol,
            position_side=position_side,
            side=side,
            event="REDUCE",
            qty=close_qty,
            price=price,
            fee=fee,
            realized_pnl=realized,
            order_id=order_id,
            session_id=sid,
        )
        cr_code = close_reason or "UNSPECIFIED_CLOSE"
        _append_operation(
            state,
            {
                "event": "CLOSE",
                "label_zh": _close_reason_zh(cr_code),
                "category": "close",
                "symbol": symbol,
                "position_side": position_side,
                "side": side,
                "qty": str(round(close_qty, 8)),
                "entry_price_avg": str(round(entry, 8)),
                "exit_price": str(round(price, 8)),
                "fee": str(round(fee, 8)),
                "realized_pnl_net": str(round(realized, 8)),
                "pnl_display_note": "含手续费后的本次平仓净盈亏（USDT）",
                "close_reason_code": cr_code,
                "close_reason_zh": _close_reason_zh(cr_code),
                "trigger_detail": close_detail or {},
                "order_ref": str(order_id),
                "session_id": sid or "",
                "is_full_close": new_amt <= 1e-12,
                "remaining_qty_after": "0" if new_amt <= 1e-12 else _fmt_pos_qty(new_amt, qty_step),
            },
        )
        if new_amt <= 1e-12:
            opened_at = cell.get("firstOpenTime")
            _append_closed_session(state, symbol, position_side, sid, opened_at)
            cell = {
                "positionAmt": "0",
                "entryPrice": "0",
                "leverage": leverage,
                "firstOpenTime": None,
                "lastUpdateTime": now,
                "session_id": None,
            }
            _set_pos(state, symbol, position_side, cell)
        else:
            cell["positionAmt"] = _fmt_pos_qty(new_amt, qty_step)
            cell["entryPrice"] = str(round(entry, 8))
            cell["lastUpdateTime"] = now
            _set_pos(state, symbol, position_side, cell)
        return

    raise ValueError(f"不支持的 side/positionSide 组合: {side}/{position_side}")


def _binance_order_ack(
    order_id: int, symbol: str, side: str, otype: str, qty_str: str, position_side: str, price: float | None, status: str
):
    ms = int(time.time() * 1000)
    return {
        "orderId": order_id,
        "symbol": symbol,
        "status": status,
        "clientOrderId": "",
        "price": str(price if price is not None else 0),
        "avgPrice": str(price or 0),
        "origQty": qty_str,
        "executedQty": qty_str if status == "FILLED" else "0",
        "cumQty": qty_str if status == "FILLED" else "0",
        "cumQuote": "0",
        "timeInForce": "GTC",
        "type": otype,
        "reduceOnly": False,
        "closePosition": False,
        "side": side,
        "positionSide": position_side,
        "stopPrice": "0",
        "workingType": "CONTRACT_PRICE",
        "priceProtect": False,
        "origType": otype,
        "updateTime": ms,
        "selfTradePreventionMode": "EXPIRE_MAKER",
    }


def _binance_algo_ack(algo_id: int, params: dict, status: str = "NEW"):
    ms = int(time.time() * 1000)
    return {
        "algoId": algo_id,
        "clientAlgoId": "",
        "algoStatus": status,
        "algoType": "CONDITIONAL",
        "orderType": params["type"],
        "symbol": params["symbol"],
        "side": params["side"],
        "positionSide": params["positionSide"],
        "timeInForce": "GTC",
        "quantity": params.get("quantity", ""),
        "triggerPrice": str(params.get("triggerPrice", "")),
        "price": "",
        "workingType": params.get("workingType", "CONTRACT_PRICE"),
        "closePosition": params.get("closePosition", False),
        "createTime": ms,
        "updateTime": ms,
        "reduceOnly": True,
    }


def place_order_for_user(user: dict, quantity, payload: dict) -> dict:
    """与 routes.orders_routes.place_order_for_user 的返回结构兼容。"""
    uid = user["id"]
    alias = user["alias"]
    symbol = payload["symbol"].upper()
    side = payload["side"].upper()
    leverage = int(payload.get("leverage", 10))
    usdt_amount = float(quantity)
    take_profit = payload.get("take_profit_price")
    stop_loss = payload.get("stop_loss_price")
    order_type = payload.get("type", "MARKET").upper()
    limit_price = payload.get("price")
    is_fast_order = payload.get("is_fast_order", False)
    fast_order_tp_percentage = float(payload.get("fast_order_tp_percentage", 0) or 0)
    fast_order_sl_percentage = float(payload.get("fast_order_sl_percentage", 0) or 0)

    try:
        with _lock:
            state = _load(uid)
            step_size, tick_size = get_symbol_precision(symbol)

            if order_type == "LIMIT":
                if not limit_price:
                    raise ValueError("限价单缺少 price 参数")
                limit_price_float = round_tick(float(limit_price), tick_size)
                price_for_qty = limit_price_float
                price = limit_price_float
            else:
                price = get_latest_price(symbol)
                if price == 0:
                    raise ValueError(f"获取 {symbol} 最新价格失败")
                price_for_qty = price

            raw_qty = usdt_amount / price_for_qty * leverage
            qty = round_step(raw_qty, step_size)
            if qty <= 0:
                raise ValueError(f"{symbol} 计算数量无效: {qty}")
            qty_str = format_with_precision(qty, step_size)

            position_side = "LONG" if side == "BUY" else "SHORT"

            cell_before = _pos_cell(state, symbol, position_side)
            prev_amt = float((cell_before or {}).get("positionAmt", 0) or 0)

            result = {
                "alias": alias,
                "symbol": symbol,
                "side": side,
                "positionSide": position_side,
                "leverage": leverage,
                "quantity": qty_str,
                "price_used": price,
                "main_order": None,
                "tp_order": None,
                "sl_order": None,
            }

            if order_type == "MARKET":
                oid = _next_id(state, "next_order_id")
                _apply_trade(
                    state,
                    symbol=symbol,
                    position_side=position_side,
                    side=side,
                    qty=float(qty_str),
                    price=price,
                    leverage=leverage,
                    order_id=str(oid),
                    qty_step=step_size,
                )
                result["main_order"] = _binance_order_ack(oid, symbol, side, "MARKET", qty_str, position_side, price, "FILLED")
            else:
                oid = _next_id(state, "next_order_id")
                oo = {
                    "orderId": oid,
                    "symbol": symbol,
                    "side": side,
                    "type": "LIMIT",
                    "positionSide": position_side,
                    "price": str(limit_price_float),
                    "origQty": qty_str,
                    "executedQty": "0",
                    "status": "NEW",
                    "timeInForce": "GTC",
                    "leverage": leverage,
                    "created_ms": int(time.time() * 1000),
                }
                state["open_orders"].append(oo)
                result["main_order"] = _binance_order_ack(oid, symbol, side, "LIMIT", qty_str, position_side, limit_price_float, "NEW")

            # 止盈止损（模拟条件单，需 evaluate 或前端触发时可走 evaluate）
            if take_profit or (is_fast_order and fast_order_tp_percentage > 0):
                if is_fast_order and position_side == "LONG":
                    take_profit = price * (1 + fast_order_tp_percentage / 100)
                elif is_fast_order and position_side == "SHORT":
                    take_profit = price * (1 - fast_order_tp_percentage / 100)
                take_profit = round_tick(float(take_profit), tick_size)
                aid = _next_id(state, "next_algo_id")
                tp_params = {
                    "symbol": symbol,
                    "side": "SELL" if side == "BUY" else "BUY",
                    "type": "TAKE_PROFIT_MARKET",
                    "triggerPrice": take_profit,
                    "closePosition": True,
                    "positionSide": position_side,
                    "workingType": "CONTRACT_PRICE",
                    "quantity": "",
                }
                state["algo_orders"].append(
                    {
                        "algoId": aid,
                        "params": tp_params,
                        "status": "NEW",
                        "createTime": int(time.time() * 1000),
                    }
                )
                result["tp_order"] = _binance_algo_ack(aid, {**tp_params, "algoType": "CONDITIONAL"}, "NEW")

            if stop_loss or (is_fast_order and fast_order_sl_percentage > 0):
                if is_fast_order and position_side == "LONG":
                    stop_loss = price * (1 - fast_order_sl_percentage / 100)
                elif is_fast_order and position_side == "SHORT":
                    stop_loss = price * (1 + fast_order_sl_percentage / 100)
                stop_loss = round_tick(float(stop_loss), tick_size)
                aid = _next_id(state, "next_algo_id")
                sl_params = {
                    "symbol": symbol,
                    "side": "SELL" if side == "BUY" else "BUY",
                    "type": "STOP_MARKET",
                    "triggerPrice": stop_loss,
                    "closePosition": True,
                    "positionSide": position_side,
                    "workingType": "CONTRACT_PRICE",
                    "quantity": "",
                }
                state["algo_orders"].append(
                    {
                        "algoId": aid,
                        "params": sl_params,
                        "status": "NEW",
                        "createTime": int(time.time() * 1000),
                    }
                )
                result["sl_order"] = _binance_algo_ack(aid, {**sl_params, "algoType": "CONDITIONAL"}, "NEW")

            if order_type == "MARKET":
                cell_after = _pos_cell(state, symbol, position_side)
                ev = "ADD_MARKET" if prev_amt > 0 else "OPEN_MARKET"
                label = "加仓（市价成交）" if prev_amt > 0 else "开仓（市价成交）"
                margin_est = round(float(qty_str) * float(price) / max(leverage, 1), 8)
                _append_operation(
                    state,
                    {
                        "event": ev,
                        "label_zh": label,
                        "category": "open",
                        "symbol": symbol,
                        "position_side": position_side,
                        "side": side,
                        "leverage": leverage,
                        "order_type": "MARKET",
                        "main_order_id": str(result["main_order"]["orderId"]),
                        "fill": {
                            "qty": qty_str,
                            "price": str(price),
                            "margin_usdt_est": str(margin_est),
                        },
                        "take_profit": _algo_ack_summary(result.get("tp_order")),
                        "stop_loss": _algo_ack_summary(result.get("sl_order")),
                        "position_after": {
                            "session_id": cell_after.get("session_id") if cell_after else None,
                            "qty": cell_after.get("positionAmt") if cell_after else None,
                            "entry_avg": cell_after.get("entryPrice") if cell_after else None,
                            "first_open_ts": cell_after.get("firstOpenTime") if cell_after else None,
                        },
                    },
                )
            else:
                _append_operation(
                    state,
                    {
                        "event": "OPEN_LIMIT_SUBMIT",
                        "label_zh": "限价开仓挂单已提交（未成交）",
                        "category": "open_order",
                        "symbol": symbol,
                        "position_side": position_side,
                        "side": side,
                        "leverage": leverage,
                        "order_type": "LIMIT",
                        "main_order_id": str(result["main_order"]["orderId"]),
                        "limit_pending": {"price": str(limit_price_float), "qty": qty_str},
                        "take_profit": {"placed": False, "note": "当前请求仅挂限价主单；止盈止损需在成交后另挂（或成交后再调 tp-sl 接口）"},
                        "stop_loss": {"placed": False, "note": "同上"},
                        "position_after": None,
                    },
                )

            _save(uid, state)
        return {"success": True, "alias": alias, "result": result}
    except Exception as e:
        return {"success": False, "alias": alias, "error": str(e)}


def _match_limit_order(state: dict, o: dict, mark: float) -> bool:
    symbol = o["symbol"]
    side = o["side"]
    position_side = o["positionSide"]
    limit_p = float(o["price"])
    qty = float(o["origQty"])
    leverage = int(o.get("leverage", 10))
    oid = o["orderId"]

    filled = False
    if side == "BUY" and mark <= limit_p:
        filled = True
        fill_price = limit_p
    elif side == "SELL" and mark >= limit_p:
        filled = True
        fill_price = limit_p
    if not filled:
        return False

    cell_before = _pos_cell(state, symbol, position_side)
    prev_amt = float((cell_before or {}).get("positionAmt", 0) or 0)
    step_size, _ = get_symbol_precision(symbol)
    _apply_trade(
        state,
        symbol=symbol,
        position_side=position_side,
        side=side,
        qty=qty,
        price=fill_price,
        leverage=leverage,
        order_id=str(oid),
        qty_step=step_size,
    )
    cell_after = _pos_cell(state, symbol, position_side)
    ev = "ADD_LIMIT_FILLED" if prev_amt > 0 else "OPEN_LIMIT_FILLED"
    label = "限价加仓成交（撮合）" if prev_amt > 0 else "限价开仓成交（撮合）"
    _append_operation(
        state,
        {
            "event": ev,
            "label_zh": label,
            "category": "open",
            "symbol": symbol,
            "position_side": position_side,
            "side": side,
            "leverage": leverage,
            "order_type": "LIMIT",
            "main_order_id": str(oid),
            "fill": {"qty": str(qty), "price": str(fill_price)},
            "trigger": {
                "kind": "MARK_PRICE_VS_LIMIT",
                "mark_price_at_fill": str(mark),
                "limit_price": str(limit_p),
            },
            "take_profit": {"placed": False},
            "stop_loss": {"placed": False},
            "position_after": {
                "session_id": cell_after.get("session_id") if cell_after else None,
                "qty": cell_after.get("positionAmt") if cell_after else None,
                "entry_avg": cell_after.get("entryPrice") if cell_after else None,
                "first_open_ts": cell_after.get("firstOpenTime") if cell_after else None,
            },
        },
    )
    o["status"] = "FILLED"
    o["executedQty"] = o["origQty"]
    o["updateTime"] = int(time.time() * 1000)
    o["avgPrice"] = str(fill_price)
    return True


def _algo_should_fire(position_side: str, order_type: str, trigger: float, mark: float) -> bool:
    if position_side == "LONG":
        if order_type == "TAKE_PROFIT_MARKET":
            return mark >= trigger
        if order_type == "STOP_MARKET":
            return mark <= trigger
    else:
        if order_type == "TAKE_PROFIT_MARKET":
            return mark <= trigger
        if order_type == "STOP_MARKET":
            return mark >= trigger
    return False


def _fill_algo_order_core(state: dict, rec: dict, mark: float, extra_detail: dict | None = None) -> str:
    """
    按给定标记价执行条件单平仓。不校验价格是否「达标」——由调用方负责。
    返回: FILLED | CANCELED
    """
    params = rec["params"]
    symbol = params["symbol"]
    pos_side = params["positionSide"]
    close_side = params["side"]
    otype = params["type"]
    trigger = float(params["triggerPrice"])
    ms = int(time.time() * 1000)

    cell = _pos_cell(state, symbol, pos_side)
    if not cell:
        rec["status"] = "CANCELED"
        rec["updateTime"] = ms
        return "CANCELED"
    amt = float(cell.get("positionAmt", 0) or 0)
    if amt <= 0:
        rec["status"] = "CANCELED"
        rec["updateTime"] = ms
        return "CANCELED"

    close_qty = amt
    if params.get("quantity") not in (None, "", "0"):
        try:
            q = float(params["quantity"])
            if q > 0:
                close_qty = min(q, amt)
        except Exception:
            pass

    lev = int(cell.get("leverage", 1) or 1)
    aid = rec["algoId"]
    step_size, _ = get_symbol_precision(symbol)
    close_qty = round_step(close_qty, step_size)
    if close_qty <= 0:
        rec["status"] = "CANCELED"
        rec["updateTime"] = ms
        return "CANCELED"
    cr = "CONDITIONAL_TAKE_PROFIT" if otype == "TAKE_PROFIT_MARKET" else "CONDITIONAL_STOP_LOSS"
    detail = {
        "algo_id": str(aid),
        "algo_order_type": otype,
        "trigger_price": str(trigger),
        "mark_price_at_trigger": str(mark),
        "close_position": params.get("closePosition"),
        "quantity_param": str(params.get("quantity", "")),
    }
    if extra_detail:
        detail.update(extra_detail)
    _apply_trade(
        state,
        symbol=symbol,
        position_side=pos_side,
        side=close_side,
        qty=close_qty,
        price=mark,
        leverage=lev,
        order_id=f"algo_{aid}",
        qty_step=step_size,
        close_reason=cr,
        close_detail=detail,
    )
    rec["status"] = "FILLED"
    rec["updateTime"] = ms
    return "FILLED"


def _execute_algo(state: dict, rec: dict, mark: float) -> bool:
    params = rec["params"]
    pos_side = params["positionSide"]
    otype = params["type"]
    trigger = float(params["triggerPrice"])
    if not _algo_should_fire(pos_side, otype, trigger, mark):
        return False
    _fill_algo_order_core(state, rec, mark, None)
    return True


def trigger_conditional_order_by_client(user_id: str, algo_id, mark_price: float) -> dict:
    """
    前端 WebSocket 判定标记价达标后，携带条件单 algoId 与当前标记价请求触发。
    服务端不校验是否触及 triggerPrice，按传入 mark_price 作为成交价平仓并记账。
    """
    try:
        aid = int(algo_id)
    except (TypeError, ValueError):
        return {"success": False, "error": "algo_id 无效"}
    try:
        mark = float(mark_price)
    except (TypeError, ValueError):
        return {"success": False, "error": "mark_price 无效"}
    if mark <= 0:
        return {"success": False, "error": "mark_price 须为正数"}

    with _lock:
        state = _load(user_id)
        rec = None
        for r in state["algo_orders"]:
            if r.get("algoId") == aid and r.get("status") == "NEW":
                rec = r
                break
        if not rec:
            return {"success": False, "error": "未找到状态为 NEW 的条件单（algoId 不存在或已成交/已撤销）"}

        p = rec["params"]
        otype = p["type"]
        symbol = p["symbol"]
        pos_side = p["positionSide"]
        trigger = float(p["triggerPrice"])

        _append_operation(
            state,
            {
                "event": "CLIENT_REQUEST_TRIGGER_ALGO",
                "label_zh": "前端请求触发条件单（依据 WebSocket 标记价，提交成交价）",
                "category": "conditional",
                "algo_id": str(aid),
                "symbol": symbol,
                "position_side": pos_side,
                "algo_order_type": otype,
                "configured_trigger_price": str(trigger),
                "mark_price_submitted": str(mark),
                "note": "是否达标由前端判断；本接口按 mark_price 执行平仓并写入 CLOSE 流水",
            },
        )

        extra = {
            "trigger_channel": "FRONTEND_WEBSOCKET_API",
            "server_validates_trigger": False,
        }
        st = _fill_algo_order_core(state, rec, mark, extra)
        state["algo_orders"] = [r for r in state["algo_orders"] if r.get("algoId") != aid]
        _save(user_id, state)

    if st == "FILLED":
        return {
            "success": True,
            "data": {
                "algo_id": aid,
                "symbol": symbol,
                "position_side": pos_side,
                "order_type": otype,
                "configured_trigger_price": trigger,
                "mark_price_used": mark,
                "status": "FILLED",
            },
        }
    return {
        "success": False,
        "error": "无对应持仓或平仓数量过小，条件单已取消",
        "data": {"algo_id": aid, "symbol": symbol, "status": "CANCELED"},
    }


def evaluate_triggers(user_id: str, symbol: str, mark_price: float | None = None) -> dict:
    """根据主网标记价撮合限价单与条件单。返回本用户本次触发摘要。"""
    symbol = symbol.upper()
    mark = float(mark_price) if mark_price is not None else get_mark_price(symbol)
    if mark <= 0:
        return {"user_id": user_id, "error": "标记价格无效", "filled_limits": 0, "filled_algos": 0}

    summary = {"user_id": user_id, "symbol": symbol, "mark": mark, "filled_limits": 0, "filled_algos": 0}
    with _lock:
        state = _load(user_id)
        new_open = []
        for o in state["open_orders"]:
            if o.get("status") != "NEW":
                continue
            if o["symbol"] != symbol:
                new_open.append(o)
                continue
            if _match_limit_order(state, o, mark):
                summary["filled_limits"] += 1
            if o.get("status") == "NEW":
                new_open.append(o)
        state["open_orders"] = new_open

        new_algos = []
        for rec in state["algo_orders"]:
            if rec.get("status") != "NEW":
                continue
            if rec["params"]["symbol"] != symbol:
                new_algos.append(rec)
                continue
            fired = _execute_algo(state, rec, mark)
            if not fired:
                new_algos.append(rec)
            elif rec.get("status") == "FILLED":
                summary["filled_algos"] += 1
        state["algo_orders"] = new_algos

        _save(user_id, state)
    return summary


def cancel_order(user_id: str, symbol: str, order_id) -> dict:
    with _lock:
        state = _load(user_id)
        sym = symbol.upper()
        oid = int(order_id) if str(order_id).isdigit() else None
        found = None
        for o in state["open_orders"]:
            if o["symbol"] == sym and oid == o["orderId"]:
                found = o
                break
        if found:
            state["open_orders"] = [o for o in state["open_orders"] if o is not found]
            _append_operation(
                state,
                {
                    "event": "CANCEL_OPEN_ORDER",
                    "label_zh": "撤销限价/普通挂单",
                    "category": "cancel",
                    "symbol": sym,
                    "order_id": str(oid),
                    "side": found.get("side"),
                    "position_side": found.get("positionSide"),
                },
            )
            _save(user_id, state)
            found["status"] = "CANCELED"
            return {"success": True, "result": found}
        aid = int(order_id) if str(order_id).isdigit() else None
        for i, rec in enumerate(state["algo_orders"]):
            if rec["params"]["symbol"] == sym and rec["algoId"] == aid:
                p = rec["params"]
                state["algo_orders"].pop(i)
                _append_operation(
                    state,
                    {
                        "event": "CANCEL_ALGO_ORDER",
                        "label_zh": "撤销止盈/止损条件单",
                        "category": "cancel",
                        "symbol": sym,
                        "algo_id": str(aid),
                        "order_type": p.get("type"),
                        "trigger_price": str(p.get("triggerPrice", "")),
                        "position_side": p.get("positionSide"),
                    },
                )
                _save(user_id, state)
                return {"success": True, "result": {"algoId": aid, "status": "CANCELED"}}
    return {"success": False, "error": "订单不存在"}


def cancel_all_symbol(user_id: str, symbol: str) -> dict:
    symbol = symbol.upper()
    with _lock:
        state = _load(user_id)
        c1 = len([o for o in state["open_orders"] if o["symbol"] == symbol])
        c2 = len([a for a in state["algo_orders"] if a["params"]["symbol"] == symbol])
        state["open_orders"] = [o for o in state["open_orders"] if o["symbol"] != symbol]
        state["algo_orders"] = [a for a in state["algo_orders"] if a["params"]["symbol"] != symbol]
        if c1 or c2:
            _append_operation(
                state,
                {
                    "event": "BULK_CANCEL_SYMBOL",
                    "label_zh": f"批量撤单：{symbol} 的普通挂单与条件单",
                    "category": "cancel",
                    "symbol": symbol,
                    "cancelled_open_orders": c1,
                    "cancelled_algo_orders": c2,
                },
            )
        _save(user_id, state)
    return {"cancelled_orders": c1, "cancelled_algos": c2}


def cancel_algo_only_for_symbol(user_id: str, symbol: str) -> dict:
    """仅撤销该 symbol 下条件单，保留普通限价挂单。"""
    symbol = symbol.upper()
    with _lock:
        state = _load(user_id)
        c2 = len([a for a in state["algo_orders"] if a["params"]["symbol"] == symbol])
        state["algo_orders"] = [a for a in state["algo_orders"] if a["params"]["symbol"] != symbol]
        if c2:
            _append_operation(
                state,
                {
                    "event": "BULK_CANCEL_ALGO_SYMBOL",
                    "label_zh": f"批量撤销条件单：{symbol}（未动普通挂单）",
                    "category": "cancel",
                    "symbol": symbol,
                    "cancelled_open_orders": 0,
                    "cancelled_algo_orders": c2,
                },
            )
        _save(user_id, state)
    return {"cancelled_orders": 0, "cancelled_algos": c2}


def list_open_orders(user_id: str) -> list:
    with _lock:
        state = _load(user_id)
        out = []
        for o in state["open_orders"]:
            if o.get("status") != "NEW":
                continue
            oid = str(o["orderId"])
            out.append(
                {
                    "symbol": o["symbol"],
                    "orderId": oid,
                    "clientOrderId": "",
                    "price": str(o["price"]),
                    "origQty": str(o["origQty"]),
                    "executedQty": str(o.get("executedQty", "0")),
                    "reduceOnly": False,
                    "status": o["status"],
                    "stopPrice": "0",
                    "closePosition": False,
                    "side": o["side"],
                    "type": o["type"],
                    "timeInForce": o.get("timeInForce", "GTC"),
                    "positionSide": o["positionSide"],
                    "workingType": "CONTRACT_PRICE",
                    "priceProtect": False,
                    "updateTime": o.get("created_ms", int(time.time() * 1000)),
                    "isIsolated": False,
                    "type_label": "contract_order",
                }
            )
        return out


def list_algo_orders(user_id: str) -> list:
    with _lock:
        state = _load(user_id)
        out = []
        for rec in state["algo_orders"]:
            if rec.get("status") != "NEW":
                continue
            p = rec["params"]
            aid = str(rec["algoId"])
            out.append(
                {
                    "symbol": p["symbol"],
                    "algoId": aid,
                    "clientAlgoId": "",
                    "side": p["side"],
                    "orderType": p["type"],
                    "type": p["type"],
                    "positionSide": p["positionSide"],
                    "reduceOnly": True,
                    "triggerPrice": str(p.get("triggerPrice", "")),
                    "price": "",
                    "quantity": str(p.get("quantity", "")),
                    "closePosition": p.get("closePosition", False),
                    "workingType": p.get("workingType", "CONTRACT_PRICE"),
                    "timeInForce": "GTC",
                    "status": rec["status"],
                    "createTime": rec.get("createTime"),
                    "updateTime": rec.get("createTime"),
                    "type_label": "algo_order",
                }
            )
        return out


def market_close(user_id: str, symbol: str, position_side: str, close_ratio: float) -> dict:
    symbol = symbol.upper()
    position_side = position_side.upper()
    with _lock:
        state = _load(user_id)
        cell = _pos_cell(state, symbol, position_side)
        if not cell:
            return {"result": "未找到仓位或仓位为0"}
        amt = float(cell.get("positionAmt", 0) or 0)
        if amt <= 0:
            return {"result": "未找到仓位或仓位为0"}
        close_amt = round_step(amt * (close_ratio / 100.0), get_symbol_precision(symbol)[0])
        if close_amt <= 0:
            return {"result": "平仓数量为0"}
        price = get_latest_price(symbol)
        if price == 0:
            return {"error": "获取价格失败"}
        side = "SELL" if position_side == "LONG" else "BUY"
        lev = int(cell.get("leverage", 1) or 1)
        oid = _next_id(state, "next_order_id")
        step_size, _ = get_symbol_precision(symbol)
        cr = "MARKET_CLOSE_FULL" if close_ratio >= 99.999 else "MARKET_CLOSE_PARTIAL"
        _apply_trade(
            state,
            symbol=symbol,
            position_side=position_side,
            side=side,
            qty=close_amt,
            price=price,
            leverage=lev,
            order_id=str(oid),
            qty_step=step_size,
            close_reason=cr,
            close_detail={"close_ratio": close_ratio, "source": "POST /api/positions/market-close"},
        )
        _save(user_id, state)
        return {
            "close_amt": str(close_amt),
            "status_code": 200,
            "result": _binance_order_ack(oid, symbol, side, "MARKET", str(close_amt), position_side, price, "FILLED"),
        }


def place_tp_sl_orders(
    user_id: str,
    symbol: str,
    position_side: str,
    *,
    take_profit_price=None,
    take_profit_qty=None,
    stop_loss_price=None,
    stop_loss_qty=None,
) -> list:
    """take_profit_qty / stop_loss_qty 为币数量（与现有路由计算后一致）。"""
    symbol = symbol.upper()
    position_side = position_side.upper()
    reverse_side = "SELL" if position_side == "LONG" else "BUY"
    results = []
    _, tick_size = get_symbol_precision(symbol)
    with _lock:
        state = _load(user_id)
        attached = []
        if take_profit_price and take_profit_qty and float(take_profit_qty) > 0:
            tp_price = round_tick(float(take_profit_price), tick_size)
            aid = _next_id(state, "next_algo_id")
            tp_params = {
                "symbol": symbol,
                "side": reverse_side,
                "type": "TAKE_PROFIT_MARKET",
                "triggerPrice": tp_price,
                "positionSide": position_side,
                "quantity": str(take_profit_qty),
                "closePosition": False,
                "workingType": "CONTRACT_PRICE",
            }
            state["algo_orders"].append({"algoId": aid, "params": tp_params, "status": "NEW", "createTime": int(time.time() * 1000)})
            results.append({"order_type": "TAKE_PROFIT", "status_code": 200, "result": _binance_algo_ack(aid, {**tp_params, "algoType": "CONDITIONAL"}, "NEW")})
            attached.append(
                {
                    "role": "TAKE_PROFIT",
                    "algo_id": str(aid),
                    "order_type": "TAKE_PROFIT_MARKET",
                    "trigger_price": str(tp_price),
                    "quantity": str(take_profit_qty),
                }
            )
        if stop_loss_price and stop_loss_qty and float(stop_loss_qty) > 0:
            sl_price = round_tick(float(stop_loss_price), tick_size)
            aid = _next_id(state, "next_algo_id")
            sl_params = {
                "symbol": symbol,
                "side": reverse_side,
                "type": "STOP_MARKET",
                "triggerPrice": sl_price,
                "positionSide": position_side,
                "quantity": str(stop_loss_qty),
                "closePosition": False,
                "workingType": "CONTRACT_PRICE",
            }
            state["algo_orders"].append({"algoId": aid, "params": sl_params, "status": "NEW", "createTime": int(time.time() * 1000)})
            results.append({"order_type": "STOP_LOSS", "status_code": 200, "result": _binance_algo_ack(aid, {**sl_params, "algoType": "CONDITIONAL"}, "NEW")})
            attached.append(
                {
                    "role": "STOP_LOSS",
                    "algo_id": str(aid),
                    "order_type": "STOP_MARKET",
                    "trigger_price": str(sl_price),
                    "quantity": str(stop_loss_qty),
                }
            )
        if attached:
            _append_operation(
                state,
                {
                    "event": "ATTACH_TP_SL",
                    "label_zh": "挂止盈/止损条件单（持仓后补挂或分批）",
                    "category": "conditional",
                    "symbol": symbol,
                    "position_side": position_side,
                    "orders": attached,
                },
            )
        _save(user_id, state)
    return results


def build_positions_view(user_id: str) -> tuple[list, list]:
    """返回 (wallet_rows, contract_rows) 与 positions_routes 形状接近。"""
    with _lock:
        state = _load(user_id)

    unrealized_total = 0.0
    contract_rows = []
    for symbol, sides in list(state.get("positions", {}).items()):
        for pos_side, cell in sides.items():
            amt = float(cell.get("positionAmt", 0) or 0)
            if amt == 0:
                continue
            mark = get_mark_price(symbol)
            entry = float(cell.get("entryPrice", 0) or 0)
            lev = int(cell.get("leverage", 1) or 1)
            if pos_side == "LONG":
                u_pnl = (mark - entry) * amt
            else:
                u_pnl = (entry - mark) * amt
            unrealized_total += u_pnl
            notional = abs(amt * mark)
            init_margin = notional / lev if lev else notional
            signed_amt = amt if pos_side == "LONG" else -amt
            contract_rows.append(
                {
                    "type": "contract",
                    "symbol": symbol,
                    "positionSide": pos_side,
                    "positionAmt": str(signed_amt),
                    "notional": str(round(amt * mark, 8)),
                    "initialMargin": f"{init_margin:.8f}",
                    "isolatedMargin": "0",
                    "isolatedWallet": "0",
                    "unrealizedProfit": str(round(u_pnl, 8)),
                    "markPrice": f"{mark:.8f}",
                    "maintMargin": f"{notional * 0.004:.8f}",
                    "liquidation_price_usdt": "0",
                    "updateTime": cell.get("lastUpdateTime"),
                    "price_update_time": int(time.time() * 1000),
                    "leverage": lev,
                    "entryPrice": cell.get("entryPrice"),
                    "firstOpenTime": cell.get("firstOpenTime"),
                }
            )

    vw = float(state.get("virtual_wallet_usdt", 1_000_000))
    wallet_rows = [
        {
            "type": "wallet",
            "asset": "USDT",
            "availableBalance": str(vw),
            "walletBalance": str(vw),
            "crossWalletBalance": str(vw),
            "marginBalance": str(vw),
            "unrealizedProfit": str(round(unrealized_total, 8)),
            "crossUnPnl": str(round(unrealized_total, 8)),
            "initialMargin": "0",
            "maintMargin": "0",
        }
    ]
    return wallet_rows, contract_rows


def get_history(user_id: str, ledger_limit: int = 200, operations_limit: int | None = None) -> dict:
    with _lock:
        state = _load(user_id)
        ledger = state["ledger"][-ledger_limit:]
        sessions = state["sessions"][-100:]
        ops = None
        if operations_limit is not None and operations_limit > 0:
            ops = state.get("operations", [])[-operations_limit:]
    out = {"ledger": ledger, "sessions": sessions}
    if ops is not None:
        out["operations"] = []
        for row in ops:
            er = _enrich_op_cst8(row)
            out["operations"].append({**er, "narrative_zh": operation_narrative_zh(er)})
    return out


def get_position_session_history(
    user_id: str,
    *,
    symbol: str | None = None,
    position_side: str | None = None,
    limit: int = 100,
    offset: int = 0,
    order: str = "desc",
    include_open: bool = False,
) -> dict:
    """
    已平仓会话列表（双向持仓下按 symbol + positionSide 维度）。
    数据来自 sessions + ledger 统计平仓笔数。
    """
    with _lock:
        state = _load(user_id)
        all_sess = list(state.get("sessions", []))
        ledger = list(state.get("ledger", []))

    sessions = all_sess
    if symbol:
        su = symbol.upper().strip()
        sessions = [s for s in sessions if s.get("symbol") == su]
    if position_side:
        psu = position_side.upper().strip()
        sessions = [s for s in sessions if (s.get("positionSide") or "").upper() == psu]

    reverse = (order or "desc").lower() == "desc"
    sessions.sort(key=lambda x: int(x.get("closed_at", 0) or 0), reverse=reverse)
    total = len(sessions)
    page = sessions[offset : offset + limit]

    items = []
    for s in page:
        sid = s.get("session_id")
        reduce_legs = sum(
            1 for row in ledger if row.get("session_id") == sid and row.get("event") == "REDUCE"
        )
        oa, ca = s.get("opened_at"), s.get("closed_at")
        dur = None
        oa_cst8 = ""
        ca_cst8 = ""
        try:
            if oa is not None:
                oai = int(oa)
                oa_cst8 = _ts_cst8(oai)
            if ca is not None:
                cai = int(ca)
                ca_cst8 = _ts_cst8(cai)
            if oa is not None and ca is not None:
                dur = int(ca) - int(oa)
        except (TypeError, ValueError):
            pass
        items.append(
            {
                "session_id": s.get("session_id"),
                "symbol": s.get("symbol"),
                "positionSide": s.get("positionSide"),
                "opened_at": oa,
                "closed_at": ca,
                "opened_at_cst8": oa_cst8,
                "closed_at_cst8": ca_cst8,
                "duration_ms": dur,
                "total_realized_pnl_net": s.get("total_realized_pnl"),
                "close_legs_count": reduce_legs,
                "status": "CLOSED",
            }
        )

    out: dict = {
        "total": total,
        "limit": limit,
        "offset": offset,
        "order": "desc" if reverse else "asc",
        "sessions": items,
    }
    if include_open:
        _, contracts = build_positions_view(user_id)
        out["open_positions"] = contracts
    return out


def list_operations(user_id: str, limit: int = 500, offset: int = 0, order: str = "asc") -> dict:
    """order: asc 时间正序（适合复盘时间轴）；desc 最新在前。"""
    with _lock:
        state = _load(user_id)
        ops = list(state.get("operations", []))
    reverse = (order or "asc").lower() == "desc"
    ops_sorted = sorted(ops, key=lambda x: (x.get("ts", 0), x.get("seq", 0)), reverse=reverse)
    total = len(ops_sorted)
    raw_items = ops_sorted[offset : offset + limit]
    items = []
    for row in raw_items:
        er = _enrich_op_cst8(row)
        items.append({**er, "narrative_zh": operation_narrative_zh(er)})
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "order": "desc" if reverse else "asc",
        "items": items,
    }


def export_operations_bytes(user_id: str, fmt: str) -> tuple[bytes, str, str]:
    fmt = (fmt or "json").lower().strip()
    with _lock:
        state = _load(user_id)
        ops = list(state.get("operations", []))
    now_ms = int(time.time() * 1000)
    meta = {
        "exported_at_ts": now_ms,
        "exported_at_cst8": _ts_cst8(now_ms),
        "schema_version": 1,
        "record_count": len(ops),
    }
    if fmt == "json":
        ops_out = []
        for r in ops:
            er = _enrich_op_cst8(r)
            ops_out.append({**er, "narrative_zh": operation_narrative_zh(er)})
        payload = json.dumps({"meta": meta, "operations": ops_out}, ensure_ascii=False, indent=2)
        return payload.encode("utf-8"), "application/json; charset=utf-8", ".json"
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["操作说明"])
        for r in sorted(ops, key=lambda x: (x.get("ts", 0), x.get("seq", 0))):
            er = _enrich_op_cst8(r)
            w.writerow([operation_narrative_zh(er)])
        return buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", ".csv"
    raise ValueError(f"不支持的导出格式: {fmt}")

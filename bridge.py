from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


LOG = logging.getLogger("watch-trader-bridge")
ROOT = Path(__file__).resolve().parent
_DIRECT_OPENER = build_opener(ProxyHandler({}))


def _state_dir() -> Path:
    explicit = os.getenv("WATCH_TRADER_BRIDGE_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    app_data = os.getenv("STOCK_ANALYZER_DATA_DIR", "").strip()
    if app_data:
        return Path(app_data).expanduser().resolve() / "data" / "watch_trader_bridge"
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "StockAnalyzer" / "data" / "watch_trader_bridge"
    return Path.home() / ".local" / "share" / "StockAnalyzer" / "data" / "watch_trader_bridge"


DB_PATH = _state_dir() / "bridge.db"
PACKAGE = "com.hexin.plat.android"
DEFAULT_PORT = 8899
DEFAULT_FULL_SYNC_INTERVAL_SECONDS = 600
THS_RESTART_COOLDOWN_SECONDS = 30.0

# 同花顺会周期性弹出营销弹层（“金豆会员日”“9月值得投”等），以全屏 WebView/浮层
# 盖住模拟盘页面：买入入口的点击被弹层吞掉后，桥会误判页面导航异常并反复重启应用。
# 清理规则是标记门控——层级里没有已知广告标记时绝不点击任何关闭控件，委托确认框、
# 错误提示框等真实对话框因此不受影响。
_AD_MARKER_PATTERN = re.compile(
    r"(?:立即参与|点击查看|不再展示|领金豆|兑好礼|会员日|值得投|限时福利|"
    r"福利活动|宣传活动|营销活动|弹窗广告)"
)
_AD_RESOURCE_HINTS = ("pop-layer", "pop_layer", "popup", "pop_up", "advert", "marketing")
_AD_CTA_PATTERN = re.compile(
    r"(?:立即参与|点击查看|立即领取|马上参与|立即抢购|查看详情|立即购买|去参与|领取)"
)
_AD_CLOSE_TEXTS = {"关闭", "close", "Close", "CLOSE", "✕", "×"}
_AD_CLOSE_RESOURCE_HINTS = ("close", "dismiss")
_AD_DONT_SHOW_TEXTS = {"不再展示", "今日不再显示", "不再提示", "下次不再显示"}
_AD_OVERLAY_MIN_COVERAGE = 0.8
_AD_POPUP_RESOURCE_MIN_COVERAGE = 0.25
_DEFAULT_SCREEN_AREA = 1080 * 1920


def _node_bounds(node) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(
        r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", str(node.attrib.get("bounds") or "")
    )
    return tuple(int(value) for value in match.groups()) if match else None


def _bounds_center(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    left, top, right, bottom = bounds
    return (left + right) // 2, (top + bottom) // 2


def _node_label(node) -> str:
    for key in ("text", "content-desc", "resource-id"):
        value = str(node.attrib.get(key) or "").strip()
        if value:
            return value[:60]
    return "<无标签节点>"


def _screen_area(root) -> int:
    bounds = _node_bounds(root)
    if bounds:
        return max(1, (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]))
    widest = max(
        (_node_bounds(node) for node in root.iter("node") if _node_bounds(node)),
        key=lambda item: (item[2] - item[0]) * (item[3] - item[1]),
        default=None,
    )
    if widest:
        return max(1, (widest[2] - widest[0]) * (widest[3] - widest[1]))
    return _DEFAULT_SCREEN_AREA


def _is_ad_overlay_candidate(node, screen: int) -> bool:
    bounds = _node_bounds(node)
    if bounds is None:
        return False
    left, top, right, bottom = bounds
    coverage = max(0, right - left) * max(0, bottom - top) / screen
    text = str(node.attrib.get("text") or "")
    desc = str(node.attrib.get("content-desc") or "")
    resource_id = str(node.attrib.get("resource-id") or "")
    marker = (
        _AD_MARKER_PATTERN.search(text)
        or _AD_MARKER_PATTERN.search(desc)
        or any(hint in resource_id for hint in _AD_RESOURCE_HINTS)
    )
    if not marker:
        return False
    if coverage >= _AD_OVERLAY_MIN_COVERAGE:
        return True
    # 原生广告对话框可能只有卡片大小，但只要资源 id 带弹层特征仍按弹层处理。
    return any(hint in resource_id for hint in _AD_RESOURCE_HINTS) and (
        coverage >= _AD_POPUP_RESOURCE_MIN_COVERAGE
    )


def _marketing_overlay(root):
    """返回盖住页面的营销弹层节点；没有已知广告标记时返回 None。"""
    screen = _screen_area(root)
    best = None
    best_area = 0
    for node in root.iter("node"):
        if not _is_ad_overlay_candidate(node, screen):
            continue
        bounds = _node_bounds(node)
        area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
        if area > best_area:
            best, best_area = node, area
    return best


def _any_ad_marker_node(root):
    """不限尺寸的广告文案节点，仅供“页面入口缺失”的症状判断使用。"""
    best = None
    best_area = 0
    for node in root.iter("node"):
        text = str(node.attrib.get("text") or "")
        desc = str(node.attrib.get("content-desc") or "")
        resource_id = str(node.attrib.get("resource-id") or "")
        if not (
            _AD_MARKER_PATTERN.search(text)
            or _AD_MARKER_PATTERN.search(desc)
            or any(hint in resource_id for hint in _AD_RESOURCE_HINTS)
        ):
            continue
        bounds = _node_bounds(node)
        if bounds is None:
            continue
        area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
        if area > best_area:
            best, best_area = node, area
    return best


def _within(bounds: tuple[int, int, int, int], outer: tuple[int, int, int, int] | None) -> bool:
    if outer is None:
        return True
    center_x, center_y = _bounds_center(bounds)
    return outer[0] <= center_x <= outer[2] and outer[1] <= center_y <= outer[3]


def _largest_descendant(root, overlay):
    """弹层内面积最大的节点，即广告卡片本体（与浮层等大的包裹节点不算卡片）。"""
    overlay_bounds = _node_bounds(overlay)
    if overlay_bounds is None:
        return None
    overlay_area = max(
        1, (overlay_bounds[2] - overlay_bounds[0]) * (overlay_bounds[3] - overlay_bounds[1])
    )
    best = None
    best_area = 0
    for node in overlay.iter("node"):
        if node is overlay:
            continue
        bounds = _node_bounds(node)
        if bounds is None:
            continue
        area = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
        if area >= overlay_area * 0.9:
            continue
        if area > best_area:
            best, best_area = bounds, area
    return best


def _marketing_dont_show_point(root, overlay, *, already_clicked: set[tuple[int, int]]):
    """“不再展示”勾选项：关窗前先点上，避免同一广告反复弹出。"""
    overlay_bounds = _node_bounds(overlay)
    for node in root.iter("node"):
        text = str(node.attrib.get("text") or "").strip()
        desc = str(node.attrib.get("content-desc") or "").strip()
        if text not in _AD_DONT_SHOW_TEXTS and desc not in _AD_DONT_SHOW_TEXTS:
            continue
        if str(node.attrib.get("checked") or "").lower() == "true":
            continue
        if str(node.attrib.get("selected") or "").lower() == "true":
            continue
        bounds = _node_bounds(node)
        if bounds is None or not _within(bounds, overlay_bounds):
            continue
        point = _bounds_center(bounds)
        if point in already_clicked:
            continue
        return point
    return None


def _marketing_close_point(root, overlay):
    """带文字或资源 id 的关闭控件；活动主按钮（立即参与/点击查看）永远跳过。"""
    overlay_bounds = _node_bounds(overlay)
    for node in root.iter("node"):
        text = str(node.attrib.get("text") or "").strip()
        desc = str(node.attrib.get("content-desc") or "").strip()
        if _AD_CTA_PATTERN.search(text) or _AD_CTA_PATTERN.search(desc):
            continue
        resource_id = str(node.attrib.get("resource-id") or "")
        is_close = (
            text in _AD_CLOSE_TEXTS
            or desc in _AD_CLOSE_TEXTS
            or any(hint in resource_id for hint in _AD_CLOSE_RESOURCE_HINTS)
        )
        if not is_close:
            continue
        bounds = _node_bounds(node)
        if bounds is not None and _within(bounds, overlay_bounds):
            return _bounds_center(bounds)
    return None


def _marketing_below_card_close_point(root, overlay):
    """H5 弹层的关闭图标常是贴在卡片下沿居中的无文字小节点。"""
    overlay_bounds = _node_bounds(overlay)
    card = _largest_descendant(root, overlay)
    if overlay_bounds is None or card is None:
        return None
    left, top, right, bottom = overlay_bounds
    overlay_center_x = (left + right) // 2
    card_bottom = card[3]
    card_area = max(1, (card[2] - card[0]) * (card[3] - card[1]))
    best = None
    for node in overlay.iter("node"):
        if node is overlay:
            continue
        bounds = _node_bounds(node)
        if bounds is None:
            continue
        node_left, node_top, node_right, node_bottom = bounds
        if bounds == card:
            continue
        if str(node.attrib.get("text") or "").strip():
            continue
        if str(node.attrib.get("content-desc") or "").strip():
            continue
        area = (node_right - node_left) * (node_bottom - node_top)
        if area > card_area * 0.25:
            continue
        center_x = (node_left + node_right) // 2
        if abs(center_x - overlay_center_x) > (right - left) * 0.12:
            continue
        if not card_bottom - 8 <= node_top <= card_bottom + 300:
            continue
        if best is None or node_top < best[1]:
            best = bounds
    return _bounds_center(best) if best else None


def _marketing_dim_area_point(root, overlay):
    """没有可识别关闭控件时，点击卡片外的遮罩空白区收起弹层。"""
    overlay_bounds = _node_bounds(overlay)
    if overlay_bounds is None:
        return None
    left, top, right, bottom = overlay_bounds
    center_x = (left + right) // 2
    card = _largest_descendant(root, overlay)
    if card is None:
        return center_x, top + 40
    if card[1] - 60 > top + 20:
        return center_x, card[1] - 60
    return center_x, min(bottom - 20, card[3] + 80)


# 历史委托在模拟盘不受支持（页面提示“不支持历史委托查询.”），当日委托又只保留
# 当天记录，因此隔天未决订单唯一还能查到的痕迹是“历史成交”列表；该面板默认
# 只展示近7日成交，超出窗口的事件直接本地收口。
_HISTORY_DEAL_DATE_PATTERN = re.compile(r"^(\d{8}) \d{2}:\d{2}:\d{2}$")
_HISTORY_DEAL_MAX_AGE_DAYS = 7
# 历史成交行的成交时间不可能早于委托提交；放宽此余量吸收 PC 与柜台时钟误差。
_HISTORY_DEAL_CLOCK_MARGIN = timedelta(seconds=120)


def _parse_number(text: str) -> float | None:
    value = str(text or "").strip().replace(",", "").replace("¥", "")
    if not value or value in {"--", "-"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_history_deal_rows(root) -> list[dict[str, Any]]:
    """解析历史成交页可见行：名称/成交时间/成交价/数量/方向/金额，按 bounds 列分组。

    首列（名称+日期时间）的右边界即列分隔起点，其余三列（价格、数量、方向+金额）
    在剩余宽度里均分——与真机 1080 宽下的 [0,669][669,806][806,943][943,1080] 一致。
    每行以“YYYYMMDD HH:MM:SS”成交时间节点锚定，名称是它上一行的首列文本。
    注意该列是"成交时间"而非委托时间（面板列头即"成交时间"；2026-11-02 真机对照
    核实：2026-10-27 埃斯顿卖出行 09:34:24，而后端订单创建于 09:32:48、委托应在
    约 09:33:08——成交时间不可能早于委托提交，这一硬约束用于隔天撮合消歧）。
    """
    items: list[tuple[str, tuple[int, int, int, int]]] = []
    screen_right = 0
    for node in root.iter("node"):
        text = str(node.attrib.get("text") or "").strip()
        bounds = _node_bounds(node)
        if text and bounds:
            items.append((text, bounds))
            screen_right = max(screen_right, bounds[2])
    rows: list[dict[str, Any]] = []
    for text, (left, top, right, bottom) in items:
        match = _HISTORY_DEAL_DATE_PATTERN.fullmatch(text)
        if not match:
            continue
        first_col_right = right
        rest = max(1, screen_right - first_col_right)
        price_right = first_col_right + rest // 3
        qty_right = first_col_right + 2 * rest // 3

        name = next(
            (
                value
                for value, (l, tp, r, bt) in items
                if r <= first_col_right + 2 and tp < top and top - 8 <= bt <= top + 4
                and re.search(r"[一-鿿]", value)
            ),
            "",
        )

        def column_text(lo: int, hi: int, y_lo: int, y_hi: int) -> str:
            for value, (l, tp, r, bt) in items:
                if lo <= l < hi and y_lo <= (tp + bt) // 2 <= y_hi:
                    return value
            return ""

        rows.append({
            "trading_day": match.group(1),
            "time": text,
            "name": name,
            "price": _parse_number(column_text(first_col_right, price_right, top - 50, bottom + 15)),
            "quantity": int(_parse_number(column_text(price_right, qty_right, top - 50, bottom + 15)) or 0),
            "side": column_text(qty_right, screen_right + 1, top - 45, top + 5),
            "amount": _parse_number(column_text(qty_right, screen_right + 1, top - 5, bottom + 5)),
        })
    return rows


def _event_trading_date(event: dict[str, Any], fallback_received_at: str = "") -> str | None:
    """委托的交易日（本地时区 YYYY-MM-DD），当日委托只在当天保留，隔天事件据此收口。"""
    decision = event.get("decision") or {}
    paper_order = decision.get("paper_order") or {}
    for raw in (
        event.get("trading_date"),
        paper_order.get("created_at"),
        decision.get("order_created_at"),
        event.get("completed_at"),
        fallback_received_at,
    ):
        text = str(raw or "").strip()
        if not text:
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
            # events.received_at 来自 SQLite CURRENT_TIMESTAMP，是 UTC naive 时间，
            # 必须先按 UTC 解释再换算本地日期；fromisoformat 会把它当本地时间。
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            return parsed.astimezone().date().isoformat()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone().date().isoformat()
        except ValueError:
            continue
    return None


def _stale_event_expectation(event: dict[str, Any], decision: dict[str, Any]):
    """返回（股票名, 方向文案, 委托数量, 限价）用于在历史成交中匹配；缺关键信息时返回 None。"""
    stock_name = str(event.get("stock_name") or "").strip()
    action = decision.get("original_action") or decision.get("action")
    quantity = decision.get("quantity")
    limit_price = decision.get("limit_price")
    if action == "cancel":
        paper_order = decision.get("paper_order") or {}
        action = paper_order.get("side")
        quantity = paper_order.get("quantity") or quantity
        limit_price = paper_order.get("execution_limit_price") or limit_price
    if action not in ("buy", "sell"):
        return None
    if not isinstance(quantity, int) or quantity <= 0 or not stock_name:
        return None
    if not isinstance(limit_price, (int, float)) or limit_price <= 0:
        limit_price = None
    return stock_name, "买入" if action == "buy" else "卖出", quantity, (
        round(float(limit_price), 3) if limit_price is not None else None
    )


def _event_submitted_at(event: dict[str, Any], decision: dict[str, Any]) -> datetime | None:
    """订单提交时刻的下界：后端订单创建时间（桥在其后才执行点击提交）。

    撤单事件的成交属于原订单，因此锚点同样取原订单的创建时间，而非撤单时刻。
    """
    paper_order = decision.get("paper_order") or {}
    for raw in (paper_order.get("created_at"), decision.get("order_created_at")):
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed
    return None


def _history_row_fill_time(row: dict[str, Any]) -> datetime | None:
    """历史成交行的成交时间（本地时区）；解析失败返回 None，由调用方保守保留。"""
    text = str(row.get("time") or "").strip()
    try:
        return datetime.strptime(text, "%Y%m%d %H:%M:%S").astimezone()
    except ValueError:
        return None


def _match_history_fills(
    rows: list[dict[str, Any]],
    *,
    stock_name: str,
    side_text: str,
    quantity: int,
    trading_day: str,
    limit_price: float | None = None,
    claimed_quantity: int = 0,
    submitted_at: datetime | None = None,
) -> dict[str, Any] | None:
    """按交易日+名称+方向归集历史成交，返回终态回报；无法唯一归属时返回 None。

    模拟柜台的限价委托当日有效：隔天在历史成交中查无成交即视为委托失效。
    价格可行性过滤（买入成交价不高于限价、卖出不低于限价）用于排除同日同股
    其他委托的成交——例如限价 15.9 的买入不可能以 16.45 成交。

    claimed_quantity 是同一交易日/同股/同方向已被其他订单回报占用的成交量
    （来自本地事件账本的终态成交回报）。历史成交面板不带委托号，同日同向的
    多笔委托会命中同一批成交行；不扣减占用就会把别的订单成交再认领一遍
    （2026-10-27 订单87/95 幻影成交的根因）。

    submitted_at 是本订单提交时刻的下界（后端订单创建时间）。面板"成交时间"
    列不可能早于委托提交（2026-11-02 真机对照核实该列确为成交时间），据此
    再做一道只删不猜的硬过滤：成交时间早于 提交时刻-时钟余量 的行物理上不可
    能属于本订单。该过滤只删行、永不误删真实成交行；无锚点时不生效，行为
    与旧版完全一致。
    """
    day = trading_day.replace("-", "")
    matched = [
        row
        for row in rows
        if row.get("trading_day") == day
        and row.get("name") == stock_name
        and row.get("side") == side_text
    ]
    if submitted_at is not None:
        earliest = submitted_at - _HISTORY_DEAL_CLOCK_MARGIN
        matched = [
            row
            for row in matched
            if (row_time := _history_row_fill_time(row)) is None or row_time >= earliest
        ]
    if limit_price is not None:
        if side_text == "买入":
            matched = [row for row in matched if (row.get("price") or 0.0) <= limit_price + 0.011]
        else:
            matched = [row for row in matched if (row.get("price") or 0.0) >= limit_price - 0.011]
    remaining_claim = max(0, int(claimed_quantity or 0))
    available: list[dict[str, Any]] = []
    for row in matched:
        qty = int(row.get("quantity") or 0)
        if remaining_claim >= qty:
            remaining_claim -= qty
            continue
        if remaining_claim > 0:
            kept = qty - remaining_claim
            amount = float(row.get("amount") or 0.0)
            available.append({
                **row,
                "quantity": kept,
                "amount": round(amount * kept / qty, 2) if amount else amount,
            })
            remaining_claim = 0
            continue
        available.append(row)
    filled = sum(int(row.get("quantity") or 0) for row in available)
    if filled == 0:
        return {
            "status": "cancelled",
            "filled_quantity": 0,
            "average_fill_price": None,
            "rejection_reason": (
                "委托已过当日：当日委托不再保留、历史委托不受支持，历史成交无匹配成交记录"
            ),
        }
    amount = sum(float(row.get("amount") or 0.0) for row in available)
    average = round(amount / filled, 3) if amount > 0 else None
    if filled == quantity:
        return {
            "status": "filled",
            "filled_quantity": filled,
            "average_fill_price": average,
            "rejection_reason": None,
        }
    if filled < quantity:
        return {
            "status": "partially_filled",
            "filled_quantity": filled,
            "average_fill_price": average,
            "rejection_reason": None,
        }
    # 同日同向成交多于委托数量：可能混入其他委托的成交，不能强行归属。
    return None


class BridgeError(RuntimeError):
    pass


class SubmissionRejected(BridgeError):
    pass


class ExecutionUncertain(BridgeError):
    pass


UI_LOCK = threading.Lock()
THS_RESTART_LOCK = threading.Lock()
_LAST_THS_RESTART_AT = 0.0


@dataclass(frozen=True)
class Config:
    base_url: str = os.getenv("WATCH_TRADER_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    token: str = os.getenv("WATCH_TRADER_TOKEN", "")
    webhook_secret: str = os.getenv("WATCH_TRADER_WEBHOOK_SECRET", "")
    adb_serial: str = os.getenv("BLUESTACKS_SERIAL", "")
    allow_orders: bool = os.getenv("ALLOW_SIMULATED_ORDERS", "true").lower() == "true"
    expected_account_suffix: str = os.getenv("THS_SIM_ACCOUNT_SUFFIX", "")
    executable_statuses: frozenset[str] = frozenset(
        x.strip() for x in os.getenv(
            "EXECUTABLE_VALIDATION_STATUSES",
            "validated_manual_execution_disabled,accepted,partially_filled,filled",
        ).split(",") if x.strip()
    )


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                turn_id TEXT,
                symbol TEXT,
                action TEXT,
                validation_status TEXT,
                state TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                error TEXT,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                executed_at TEXT
            )
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
        if "result_json" not in columns:
            db.execute("ALTER TABLE events ADD COLUMN result_json TEXT")
        if "last_checked_at" not in columns:
            db.execute("ALTER TABLE events ADD COLUMN last_checked_at TEXT")


def execution_decision(event: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Convert a market intent to the protected counterparty-price limit captured upstream."""
    normalized = dict(decision)
    if normalized.get("action") == "cancel":
        paper_order = normalized.get("paper_order")
        if not isinstance(paper_order, dict):
            raise BridgeError("撤单意图缺少原始委托信息")
        side = str(paper_order.get("side") or "")
        price = paper_order.get("execution_limit_price")
        quantity = paper_order.get("quantity")
        if side not in {"buy", "sell"}:
            raise BridgeError("撤单意图缺少原始买卖方向")
        if not isinstance(price, (int, float)) or price <= 0:
            raise BridgeError("撤单意图缺少原始委托价格")
        if not isinstance(quantity, int) or quantity <= 0:
            raise BridgeError("撤单意图缺少原始委托数量")
        normalized.update({
            "original_action": side,
            "price_type": "limit",
            "limit_price": round(float(price), 2),
            "quantity": quantity,
            "order_created_at": paper_order.get("created_at") or paper_order.get("accepted_at"),
        })
        return normalized
    normalized.setdefault("stock_name", str(event.get("stock_name") or "").strip())
    if normalized.get("price_type") != "market":
        return normalized
    paper_order = normalized.get("paper_order")
    price = paper_order.get("execution_limit_price") if isinstance(paper_order, dict) else None
    if not isinstance(price, (int, float)) or price <= 0:
        raise BridgeError("市价意图缺少由实时卖一/买一生成的保护限价")
    normalized["original_price_type"] = "market"
    normalized["price_type"] = "limit"
    normalized["limit_price"] = round(float(price), 2)
    return normalized


def validate_event(event: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise BridgeError("缺少 event_id")
    if event.get("event_type") != "analysis.completed":
        raise BridgeError("仅接受 analysis.completed")
    decision = event.get("decision")
    if event.get("status") != "completed" or not isinstance(decision, dict):
        return "analysis_failed", None
    status = decision.get("validation_status")
    states = {
        "accepted_hold": "hold",
        "rejected_by_market_rule": "intent_rejected",
        "rejected_by_account_or_risk": "intent_rejected",
        "validated_manual_execution_disabled": "signal_only",
        "accepted": "order_open",
        "partially_filled": "order_partially_filled",
        "cancel_requested": "cancel_requested",
        "filled": "order_filled",
        "cancelled": "order_cancelled",
    }
    return states.get(status, "needs_confirmation"), decision


def validate_order(decision: dict[str, Any], cfg: Config) -> None:
    action = decision.get("action")
    symbol = str(decision.get("symbol", ""))
    quantity = decision.get("quantity")
    price_type = decision.get("price_type")
    price = decision.get("limit_price")
    status = decision.get("validation_status")
    if status not in cfg.executable_statuses:
        raise BridgeError(f"validation_status={status!r} 不在执行白名单")
    if action not in {"buy", "sell"}:
        raise BridgeError(f"当前版本只执行买入/卖出，不执行 {action!r}")
    if not re.fullmatch(r"\d{6}", symbol):
        raise BridgeError("股票代码必须是 6 位数字")
    if not isinstance(quantity, int) or quantity <= 0 or quantity % 100:
        raise BridgeError("数量必须是正整数且为 100 股的整数倍")
    if price_type != "limit":
        raise BridgeError("当前 UI 执行器仅支持限价单")
    if not isinstance(price, (int, float)) or price <= 0 or round(float(price), 2) != float(price):
        raise BridgeError("限价必须为正数且最多两位小数")


class ThsSimulator:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def connect(self):
        import uiautomator2 as u2
        return u2.connect(self.cfg.adb_serial)

    def check(self) -> dict[str, Any]:
        d = self.connect()
        # A marketing popup left over from the last session would make every
        # visibility probe below report a blocked page.
        self._dismiss_marketing_popups(d)
        info = d.app_current()
        dialog_title = d(resourceId=f"{PACKAGE}:id/dialog_title")
        return {
            "serial": self.cfg.adb_serial,
            "device": d.info,
            "app": info,
            "ths_running": info.get("package") == PACKAGE,
            "simulation_home_visible": bool(d(text="模拟练习区").exists),
            # 查询面板（历史成交等）的标题同样是“模拟炒股”，委托页要用买入按钮判别。
            "simulation_order_page_visible": bool(self._order_page_anchor(d).exists),
            "blocking_dialog": (
                str(dialog_title.get_text() or "").strip()
                if dialog_title.exists
                else None
            ),
        }

    def _activate_ths_for_read(self, d) -> None:
        """Bring THS to the foreground for a read-only account snapshot."""
        current = d.app_current()
        if current.get("package") == PACKAGE:
            return
        d.app_start(PACKAGE, wait=True)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if d.app_current().get("package") == PACKAGE:
                # 冷启动常直接带出营销弹层，先关掉再让调用方导航页面。
                self._dismiss_marketing_popups(d)
                return
            time.sleep(0.25)
        raise BridgeError("无法将同花顺切换到前台，账户同步未执行")

    @staticmethod
    def _read_text(node, resource_name: str) -> str:
        item = node.child(resourceId=f"{PACKAGE}:id/{resource_name}")
        return str(item.get_text() or "").strip() if item.exists else ""

    @staticmethod
    def _dismiss_blocking_error_dialogs(d) -> int:
        """Dismiss known non-transaction error dialogs that hide the trading UI."""
        dismissed = 0
        safe_error_titles = {"请求失败", "网络异常", "网络错误", "加载失败"}
        for _ in range(3):
            title = d(resourceId=f"{PACKAGE}:id/dialog_title")
            title_text = str(title.get_text() or "").strip() if title.exists else ""
            if title_text in safe_error_titles:
                button = d(resourceId=f"{PACKAGE}:id/left_btn")
                button_text = str(button.get_text() or "").strip() if button.exists else ""
                if not button.exists or button_text not in {"确定", "我知道了"}:
                    break
                button.click()
                dismissed += 1
                LOG.warning("已关闭阻塞同花顺页面的错误弹窗：%s", title_text)
                time.sleep(0.4)
                continue
            # A real-broker login overlay (e.g. expired 平安证券 session) hides
            # the 模拟 tab on the trade page. Closing it only drops the login
            # prompt; it never enters credentials or submits anything.
            if d(resourceId=f"{PACKAGE}:id/weituo_edit_trade_password").exists:
                close_button = d(resourceId=f"{PACKAGE}:id/close_button")
                if not close_button.exists:
                    break
                close_button.click()
                dismissed += 1
                LOG.warning("已关闭券商登录弹层，返回交易页继续寻找模拟入口")
                time.sleep(0.4)
                continue
            break
        return dismissed

    @staticmethod
    def _tap(d, point: tuple[int, int]) -> None:
        try:
            d.click(point[0], point[1])
        except Exception as exc:
            LOG.warning("点击界面控件失败：%s", exc)

    def _dismiss_marketing_popups(self, d, *, max_rounds: int = 3, symptom: bool = False) -> int:
        """Close marketing popups that cover the simulation trading UI.

        The popups (e.g. "金豆会员日", "9月值得投") render as full-screen WebView
        overlays that swallow every tap on the trading entries below, which the
        navigation code would otherwise read as a broken page and answer with
        repeated app restarts.  Dismissal is marker-gated: when the hierarchy
        carries no known ad marker nothing is ever clicked, so genuine THS
        dialogs (order confirmations, error prompts) stay untouched.  This never
        raises — ad handling must not break an order flow.

        ``symptom`` is for the caller that already failed to reach the trading
        page: a native ad dialog whose marker text sits on small nodes has no
        full-screen overlay to click, so one Back press is spent trying to fold
        it before the caller escalates to an app restart.
        """
        actions = 0
        clicked_dont_show: set[tuple[int, int]] = set()
        for _ in range(max_rounds):
            try:
                root = ET.fromstring(d.dump_hierarchy(compressed=False))
            except Exception as exc:
                LOG.warning("读取界面层级失败，跳过营销弹层清理：%s", exc)
                return actions
            overlay = _marketing_overlay(root)
            if overlay is None:
                if not symptom:
                    return actions
                marker = _any_ad_marker_node(root)
                if marker is None:
                    return actions
                LOG.warning(
                    "交易入口缺失且检测到广告文案（%s），先用返回键收起弹层", _node_label(marker)
                )
                d.press("back")
                actions += 1
                time.sleep(0.5)
                return actions
            LOG.warning("检测到盖住模拟盘页面的营销弹层（%s），正在关闭", _node_label(overlay))
            dont_show = _marketing_dont_show_point(
                root, overlay, already_clicked=clicked_dont_show
            )
            if dont_show is not None:
                # H5 勾选框的 checked 状态不一定回写层级，按坐标去重避免反复来回点。
                clicked_dont_show.add(dont_show)
                LOG.warning("已勾选营销弹层的“不再展示”")
                self._tap(d, dont_show)
                actions += 1
                time.sleep(0.4)
                continue
            point = _marketing_close_point(root, overlay) or _marketing_below_card_close_point(
                root, overlay
            )
            if point is not None:
                self._tap(d, point)
            else:
                point = _marketing_dim_area_point(root, overlay)
                if point is not None:
                    LOG.warning("营销弹层没有可识别的关闭按钮，改为点击遮罩空白区")
                    self._tap(d, point)
                else:
                    LOG.warning("营销弹层没有可点击区域，已改用返回键")
                    d.press("back")
            actions += 1
            time.sleep(0.7)
        return actions

    def _clear_blocking_overlays(self, d) -> None:
        """Dismiss every known non-transaction overlay before touching the UI."""
        self._dismiss_blocking_error_dialogs(d)
        self._dismiss_marketing_popups(d)

    @staticmethod
    def _transaction_sensitive_dialog(d) -> str | None:
        """Return a dialog title when restarting could lose an order outcome."""
        title = d(resourceId=f"{PACKAGE}:id/dialog_title")
        title_text = str(title.get_text() or "").strip() if title.exists else ""
        if re.search(r"(?:委托.*确认|确认.*委托|撤单.*确认|确认.*撤单)", title_text):
            return title_text
        prompt = d(resourceId=f"{PACKAGE}:id/prompt_content")
        if prompt.exists:
            return "模拟柜台结果"
        return None

    def _restart_ths(self, d) -> None:
        """Restart THS once before order submission and wait for a stable foreground."""
        global _LAST_THS_RESTART_AT
        sensitive = self._transaction_sensitive_dialog(d)
        if sensitive:
            raise ExecutionUncertain(f"同花顺停留在{sensitive}页面，禁止自动重启")
        with THS_RESTART_LOCK:
            now = time.monotonic()
            remaining = THS_RESTART_COOLDOWN_SECONDS - (now - _LAST_THS_RESTART_AT)
            if remaining > 0:
                raise BridgeError(f"同花顺刚刚重启，{remaining:.0f} 秒内不重复重启")
            _LAST_THS_RESTART_AT = now
            LOG.warning("同花顺页面导航异常，正在自动重启应用")
            try:
                d.app_stop(PACKAGE)
                time.sleep(0.6)
                d.app_start(PACKAGE, wait=True)
            except Exception as exc:
                raise BridgeError(f"自动重启同花顺失败：{exc}") from exc
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if d.app_current().get("package") == PACKAGE:
                    time.sleep(1.0)
                    return
                time.sleep(0.25)
            raise BridgeError("自动重启后同花顺没有进入前台")

    def _open_simulation_order_entry(self, d) -> None:
        """Open the simulation order shell, retrying one swallowed home click."""
        for attempt in range(2):
            # 进入模拟页本身就可能弹出营销层，点买入入口前先清一遍。
            self._clear_blocking_overlays(d)
            buy_menu = d(resourceId=f"{PACKAGE}:id/menu_buy")
            if buy_menu.wait(timeout=5):
                buy_menu.click()
            else:
                buy_text = d(text="买入", resourceId=f"{PACKAGE}:id/menu_buy_text")
                if not buy_text.wait(timeout=3):
                    raise BridgeError("找不到模拟练习区的买入入口")
                buy_text.click()
            time.sleep(0.5)
            self._clear_blocking_overlays(d)
            if self._order_page_anchor(d).wait(timeout=5):
                return
            if attempt == 0 and d(text="模拟练习区").exists:
                LOG.warning("同花顺模拟入口首次点击未生效，正在重试")
                time.sleep(0.8)
                continue
            break
        raise BridgeError("进入模拟委托页失败")

    @staticmethod
    def _order_page_anchor(d):
        """模拟委托页独有锚点：按钮行里的“买入”。查询面板标题同样是“模拟炒股”，
        不能作为已在委托页的判据，否则快照会在查询面板里找不到持仓入口。"""
        return d(text="买入", resourceId=f"{PACKAGE}:id/btn")

    def _ensure_simulation_order_page_once(self, d) -> None:
        current = d.app_current()
        if current.get("package") != PACKAGE:
            raise BridgeError("同花顺当前不在前台")
        self._clear_blocking_overlays(d)
        if self._order_page_anchor(d).exists:
            return
        # Read-only query panes have their own page titles. Walk back only
        # while the masked simulation account marker is still present.
        if d(resourceId=f"{PACKAGE}:id/account_info_view").exists:
            for _ in range(3):
                d.press("back")
                time.sleep(0.35)
                self._clear_blocking_overlays(d)
                if self._order_page_anchor(d).exists:
                    return
                if not d(resourceId=f"{PACKAGE}:id/account_info_view").exists:
                    break
        home_tab = d(description="首页")
        if home_tab.wait(timeout=3):
            home_tab.click()
        trade_tab = d(description="交易")
        if not trade_tab.wait(timeout=3):
            raise BridgeError("找不到同花顺交易入口")
        trade_tab.click()
        time.sleep(0.4)
        self._clear_blocking_overlays(d)
        simulation_tab = d(text="模拟", resourceId=f"{PACKAGE}:id/tab_mn")
        if not simulation_tab.wait(timeout=5):
            raise BridgeError("找不到交易页的模拟入口")
        simulation_tab.click()
        time.sleep(0.4)
        self._clear_blocking_overlays(d)
        if not d(text="模拟练习区").wait(timeout=5):
            raise BridgeError("无法进入模拟练习区首页")
        # Directly after an app restart the simulation home may be visible
        # while its first menu click is still swallowed by page animation.
        # Retry only when the page definitely remains on the read-only home;
        # this never touches the actual order submit button.
        self._open_simulation_order_entry(d)

    def _ensure_simulation_order_page(self, d) -> None:
        try:
            self._ensure_simulation_order_page_once(d)
            return
        except ExecutionUncertain:
            raise
        except BridgeError as first_error:
            sensitive = self._transaction_sensitive_dialog(d)
            if sensitive:
                raise ExecutionUncertain(
                    f"同花顺停留在{sensitive}页面，禁止自动重启"
                ) from first_error
            LOG.warning("同花顺页面导航失败，准备自恢复：%s", first_error)

        # 营销弹层可能在页面就绪后才弹出（进入模拟页本身就会触发），把入口点击
        # 全部吞掉。先按“导航已失败”的症状清一次弹层并重试，避免把弹窗误判成
        # 应用异常而重启同花顺——重启后弹窗往往再次出现，只会循环重启。
        LOG.warning("同花顺页面导航首次失败，先清理遮挡弹层并重试导航")
        self._dismiss_marketing_popups(d, symptom=True)
        try:
            self._ensure_simulation_order_page_once(d)
            return
        except ExecutionUncertain:
            raise
        except BridgeError:
            pass

        self._restart_ths(d)
        try:
            self._ensure_simulation_order_page_once(d)
        except ExecutionUncertain:
            raise
        except BridgeError as second_error:
            raise BridgeError(
                f"同花顺重启后仍无法进入模拟委托页：{second_error}"
            ) from second_error

    def _account_guard(self, d) -> None:
        suffix = self.cfg.expected_account_suffix
        if suffix and not d(textContains=suffix).exists:
            raise BridgeError(f"模拟账号尾号不是预期的 {suffix}")

    def _result_rows(self, d) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        containers = d(resourceId=f"{PACKAGE}:id/container")
        for index in range(containers.count):
            node = containers[index]
            row = {
                key: self._read_text(node, key)
                for key in ("result0", "result1", "result2", "result3", "result6", "result7", "first_tv", "second_tv")
            }
            # Empty-state panes still expose one placeholder container.  Do
            # not turn it into a fake external order/trade row.
            if not any(str(value or "").strip() for value in row.values()):
                continue
            row["_index"] = index
            rows.append(row)
        return rows

    def _wait_for_account_values(self, d, *, timeout: float = 6.0) -> dict[str, float | None]:
        """Wait until the holdings page has rendered its authoritative totals."""
        title_id = f"{PACKAGE}:id/capital_cell_title"
        value_id = f"{PACKAGE}:id/capital_cell_value"
        required = ("总资产", "总市值", "可用")
        deadline = time.monotonic() + timeout
        account_values: dict[str, float | None] = {}
        while True:
            account_root = ET.fromstring(d.dump_hierarchy(compressed=False))
            titles = [
                str(node.attrib.get("text") or "").strip()
                for node in account_root.iter("node")
                if node.attrib.get("resource-id") == title_id
            ]
            values = [
                str(node.attrib.get("text") or "").strip()
                for node in account_root.iter("node")
                if node.attrib.get("resource-id") == value_id
            ]
            account_values = {
                title: self._number(value)
                for title, value in zip(titles, values)
                if title
            }
            missing = [name for name in required if account_values.get(name) is None]
            if not missing:
                return account_values
            if time.monotonic() >= deadline:
                raise BridgeError(
                    "同花顺持仓页尚未加载完整账户数据，缺少：" + "、".join(missing)
                )
            time.sleep(0.25)

    def _live_market_value(self, d) -> float:
        """Read the holdings pane total as it stands right now."""
        try:
            values = self._wait_for_account_values(d, timeout=2.0)
        except BridgeError:
            return 0.0
        try:
            return float(values.get("总市值") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _read_complete_positions(
        self,
        d,
        *,
        market_value: float,
        timeout: float = 6.0,
    ) -> list[dict[str, Any]]:
        """Read all holdings and reject a half-rendered or half-scrolled list."""
        positions_by_code: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + timeout

        # Account totals can render slightly before the first holding row.
        while market_value > 0.01 and not positions_by_code:
            positions_by_code.update({
                (row["code"] or row["name"]): row for row in self._position_rows(d)
            })
            if positions_by_code or time.monotonic() >= deadline:
                break
            time.sleep(0.25)

        stagnant = 0
        for _ in range(12):
            visible_positions = self._position_rows(d)
            previous_count = len(positions_by_code)
            positions_by_code.update({
                (row["code"] or row["name"]): row for row in visible_positions
            })
            stagnant = stagnant + 1 if len(positions_by_code) == previous_count else 0
            observed = sum(float(row.get("market_value") or 0.0) for row in positions_by_code.values())
            if abs(observed - market_value) < 0.1:
                return list(positions_by_code.values())
            # 行情在读取过程中实时跳动，页面总市值随之变化；用当前实时总值再比对
            # 一次，避免把价格漂移误判成持仓列表不完整。
            live_market_value = self._live_market_value(d)
            if live_market_value and abs(observed - live_market_value) < 0.1:
                return list(positions_by_code.values())
            if stagnant >= 2 and time.monotonic() >= deadline:
                break
            d.swipe(720, 2180, 720, 900, duration=0.25)
            time.sleep(0.25)

        observed = sum(float(row.get("market_value") or 0.0) for row in positions_by_code.values())
        live_market_value = self._live_market_value(d)
        reference = live_market_value or market_value
        if abs(observed - reference) < 0.1:
            return list(positions_by_code.values())
        raise BridgeError(
            f"同花顺持仓读取不完整：页面总市值 {reference:.2f}，"
            f"持仓明细合计 {observed:.2f}"
        )

    @staticmethod
    def _order_identity(account_suffix: str, trading_date: str, row: dict[str, Any]) -> str:
        # THS does not expose its entrust number in every Android build.  The
        # immutable columns below form a stable contract identity; mutable
        # status/fill columns are intentionally excluded.
        identity = {
            "account_suffix": str(account_suffix or "").strip(),
            "trading_date": trading_date,
            "name": str(row.get("result0") or "").strip(),
            "time": str(row.get("result1") or "").strip(),
            "order_price": str(row.get("result2") or "").strip(),
            "side": str(row.get("result6") or "").strip(),
            "quantity": str(row.get("first_tv") or "").strip(),
        }
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return f"ths-sim:{digest}"

    def _annotate_order_identities(
        self,
        rows: list[dict[str, Any]],
        *,
        account_suffix: str,
        trading_date: str,
    ) -> None:
        identities = [self._order_identity(account_suffix, trading_date, row) for row in rows]
        counts = {identity: identities.count(identity) for identity in set(identities)}
        for row, identity in zip(rows, identities):
            if counts[identity] == 1:
                row["external_order_id"] = identity
            else:
                # Two indistinguishable THS rows must never be guessed into a
                # single internal order.
                row["identity_ambiguous"] = True

    def _position_rows(self, d) -> list[dict[str, Any]]:
        """Parse visible holdings by their stable table columns and row bounds."""
        root = ET.fromstring(d.dump_hierarchy(compressed=False))
        items: list[tuple[str, tuple[int, int, int, int]]] = []
        for node in root.iter("node"):
            value = str(node.attrib.get("text") or "").strip()
            match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.attrib.get("bounds", ""))
            if value and match:
                items.append((value, tuple(map(int, match.groups()))))
        positions: list[dict[str, Any]] = []
        for code, (_, top, _, bottom) in items:
            if not re.fullmatch(r"\d{6}", code):
                continue
            columns: dict[str, list[tuple[int, str]]] = {"name": [], "quantity": [], "price": []}
            for text, (left, item_top, right, item_bottom) in items:
                center_y = (item_top + item_bottom) // 2
                if not top <= center_y <= bottom or text == code:
                    continue
                if right <= 180:
                    columns["name"].append((item_top, text))
                elif 340 <= left < 525:
                    columns["quantity"].append((item_top, text))
                elif 520 <= left < 715:
                    columns["price"].append((item_top, text))
            for values in columns.values():
                values.sort(key=lambda item: item[0])
            names = [value for _, value in columns["name"]]
            quantities = [value for _, value in columns["quantity"]]
            prices = [value for _, value in columns["price"]]
            quantity = int(self._number(quantities[0]) or 0) if quantities else 0
            available = int(self._number(quantities[1]) or 0) if len(quantities) > 1 else quantity
            avg_cost = self._number(prices[0]) if prices else None
            last_price = self._number(prices[1]) if len(prices) > 1 else None
            if names and quantity > 0 and avg_cost is not None:
                positions.append({
                    "code": code,
                    "name": names[0],
                    "market_value": self._number(names[1]) if len(names) > 1 else None,
                    "quantity": quantity,
                    "available_quantity": available,
                    "average_cost": avg_cost,
                    "last_price": last_price,
                })
        if positions:
            return positions
        return self._position_rows_by_name(items)

    # 新版同花顺持仓表首列是「名称/市值」，行内不出现 6 位代码；
    # 此时按名称锚定行，代码留空由后端名称搜索解析。
    _POSITION_HEADER_LABELS = {
        "市值", "名称", "代码", "盈亏", "持仓/可用", "持仓", "可用",
        "成本/现价", "成本", "现价", "当日盈亏", "个股仓位", "止盈/止损",
        "盈亏比例", "持仓盈亏", "证券名称", "持仓分时",
    }

    def _position_rows_by_name(self, items: list[tuple[str, tuple[int, int, int, int]]]) -> list[dict[str, Any]]:
        header_bottom = max(
            (bounds[3] for text, bounds in items if text in self._POSITION_HEADER_LABELS),
            default=0,
        )
        # 数据行是全宽容器内首列（x<161）带「名称+数值」双行的那一行。
        rows: list[tuple[int, str, str | None, float | None]] = []
        first_col = sorted(
            ((text, bounds) for text, bounds in items if bounds[0] < 161 and bounds[2] <= 161),
            key=lambda item: item[1][1],
        )
        for index, (name, bounds) in enumerate(first_col):
            if bounds[1] <= header_bottom:
                continue
            if not re.search(r"[一-鿿]", name) or name in self._POSITION_HEADER_LABELS:
                continue
            # 同一行的第二行位于名称下方约一行高度的位置。
            second = next(
                (
                    (t, b) for t, b in first_col[index + 1:]
                    if b[1] - bounds[1] < bounds[3] - bounds[1] + 60
                    and b[1] > bounds[1] - 5
                ),
                None,
            )
            if second is None:
                continue
            second_text, second_bounds = second
            code = second_text if re.fullmatch(r"\d{6}", second_text) else None
            market_value = None if code else self._number(second_text)
            if code is None and market_value is None:
                continue
            row_top = bounds[1]
            row_bottom = second_bounds[3] + max(bounds[3] - bounds[1], 20)
            rows.append((row_top, row_bottom, name, code, market_value))

        positions: list[dict[str, Any]] = []
        for row_top, row_bottom, name, code, market_value in rows:
            columns: dict[str, list[tuple[int, str]]] = {"quantity": [], "price": []}
            for text, (left, item_top, right, item_bottom) in items:
                center_y = (item_top + item_bottom) // 2
                if not row_top <= center_y <= row_bottom or right <= 161:
                    continue
                if 312 <= left < 463:
                    columns["quantity"].append((item_top, text))
                elif 463 <= left < 627:
                    columns["price"].append((item_top, text))
            for values in columns.values():
                values.sort(key=lambda item: item[0])
            quantities = [value for _, value in columns["quantity"]]
            prices = [value for _, value in columns["price"]]
            quantity = int(self._number(quantities[0]) or 0) if quantities else 0
            available = int(self._number(quantities[1]) or 0) if len(quantities) > 1 else quantity
            avg_cost = self._number(prices[0]) if prices else None
            last_price = self._number(prices[1]) if len(prices) > 1 else None
            if quantity > 0 and avg_cost is not None:
                positions.append({
                    "code": code,
                    "name": name,
                    "market_value": market_value,
                    "quantity": quantity,
                    "available_quantity": available,
                    "average_cost": avg_cost,
                    "last_price": last_price,
                })
        return positions

    @staticmethod
    def _number(value: str) -> float | None:
        text = str(value or "").strip().replace(",", "").replace("¥", "")
        if not text or text in {"--", "-"}:
            return None
        try:
            return float(text.rstrip("%"))
        except ValueError:
            return None

    def _open_query_tab(self, d, *, resource_name: str, menu_name: str) -> None:
        """Open a read-only query pane without touching an order action."""
        # 营销弹层会吞掉“查询”入口的点击，打开查询面板前先清掉。
        self._clear_blocking_overlays(d)

        def open_visible_target() -> bool:
            # Some THS versions expose query panes as stable resource-id tabs;
            # others only expose text menu entries below the Query button.
            tab = d(resourceId=f"{PACKAGE}:id/{resource_name}")
            if tab.wait(timeout=0.5):
                tab.click()
                time.sleep(0.4)
                return True
            for selector in (
                {"text": menu_name, "resourceId": f"{PACKAGE}:id/menu_name"},
                {"text": menu_name, "resourceId": f"{PACKAGE}:id/btn"},
                {"text": menu_name},
            ):
                menu = d(**selector)
                if menu.wait(timeout=0.5):
                    menu.click()
                    time.sleep(0.5)
                    return True
            return False

        # Reuse an already visible tab/menu first. Clicking Query while its menu
        # is open can toggle the menu closed on current Android THS releases.
        if open_visible_target():
            return
        query = d(text="查询", resourceId=f"{PACKAGE}:id/btn")
        if not query.wait(timeout=3):
            raise BridgeError("找不到同花顺查询入口")
        query.click()
        time.sleep(0.3)
        if not open_visible_target():
            raise BridgeError(f"找不到{menu_name}入口")

    def _open_history_deal_pane(self, d) -> None:
        """打开模拟盘“历史成交”查询页，并以列头“成交时间”为就绪标志。

        查询面板会记住上次的页签，且日期筛选条在快捷窗口/自定义区间两种布局间
        变化，所以就绪标志用稳定的列头文本；必要时再点一次面板内的历史成交页签。
        面板默认展示近7日成交，本次收口只依赖这个默认窗口，不做日期选择器自动化。
        """
        self._clear_blocking_overlays(d)
        pane_tab = d(text="历史成交", resourceId=f"{PACKAGE}:id/btn")
        if not pane_tab.wait(timeout=1):
            query = d(text="查询", resourceId=f"{PACKAGE}:id/btn")
            if not query.wait(timeout=3):
                raise BridgeError("找不到同花顺查询入口")
            query.click()
            time.sleep(0.5)
            self._clear_blocking_overlays(d)
            menu = d(text="历史成交", resourceId=f"{PACKAGE}:id/menu_name")
            if not menu.wait(timeout=3):
                raise BridgeError("找不到历史成交查询入口")
            menu.click()
            time.sleep(1.0)
        header = d(text="成交时间")
        if not header.wait(timeout=4):
            retry_tab = d(text="历史成交", resourceId=f"{PACKAGE}:id/btn")
            if retry_tab.exists:
                retry_tab.click()
                time.sleep(1.0)
            if not header.wait(timeout=5):
                raise BridgeError("历史成交面板未加载出成交列表列头")

    def read_recent_trade_history(self) -> list[dict[str, Any]]:
        """读取历史成交面板（默认近7日窗口）并滚动收集全部可见行。"""
        d = self.connect()
        self._ensure_simulation_order_page(d)
        self._account_guard(d)
        self._open_history_deal_pane(d)
        collected: dict[str, dict[str, Any]] = {}
        stagnant = 0
        for _ in range(30):
            before = len(collected)
            root = ET.fromstring(d.dump_hierarchy(compressed=False))
            for row in _parse_history_deal_rows(root):
                key = (
                    f"{row['trading_day']}|{row['time']}|{row['name']}"
                    f"|{row['side']}|{row['quantity']}"
                )
                collected.setdefault(key, row)
            if len(collected) == before:
                stagnant += 1
                if stagnant >= 3:
                    break
            else:
                stagnant = 0
            d.swipe(540, 1500, 540, 600, duration=0.25)
            time.sleep(0.35)
        return list(collected.values())

    def snapshot(self, *, activate_app: bool = False) -> dict[str, Any]:
        """Read account, positions, today's orders and trades; never submit/cancel."""
        d = self.connect()
        if activate_app:
            self._activate_ths_for_read(d)
        self._ensure_simulation_order_page(d)
        self._account_guard(d)
        # 弹层可能在页面就绪后我才弹出，读持仓前再确认一次。
        self._dismiss_marketing_popups(d)
        suffix_node = d(resourceId=f"{PACKAGE}:id/account_info_view")
        account_suffix = str(suffix_node.get_text() or "").strip() if suffix_node.exists else ""

        holding = d(text="持仓", resourceId=f"{PACKAGE}:id/btn")
        if not holding.wait(timeout=3):
            raise BridgeError("找不到模拟持仓入口")
        holding.click()
        try:
            account_values = self._wait_for_account_values(d)
        except BridgeError:
            # Directly after entering the simulation order page, THS can expose
            # the tab before its click handler is ready. Retry only when the
            # holdings tab is still visibly unselected; do not mask a genuine
            # account-data loading failure after the tab has opened.
            holding = d(text="持仓", resourceId=f"{PACKAGE}:id/btn")
            selected = bool(holding.info.get("selected")) if holding.exists else False
            if not holding.exists or selected:
                raise
            holding.click()
            account_values = self._wait_for_account_values(d)
        market_value = float(account_values["总市值"] or 0.0)
        positions = self._read_complete_positions(d, market_value=market_value)
        positions_complete = True

        # Holding opens a separate account pane where the query controls are
        # unavailable. Return to the simulation order page before reading the
        # order and trade tabs; otherwise a one-click sync fails after it has
        # successfully read positions.
        self._ensure_simulation_order_page(d)
        self._account_guard(d)
        self._open_query_tab(d, resource_name="weituo", menu_name="当日委托")
        orders = self._result_rows(d)
        # The order query pane also replaces the main trading controls. Reset
        # it before opening the trade query pane.
        self._ensure_simulation_order_page(d)
        self._account_guard(d)
        self._open_query_tab(d, resource_name="chengjiao", menu_name="当日成交")
        trades = self._result_rows(d)
        captured_at = datetime.now().astimezone()
        self._annotate_order_identities(
            orders,
            account_suffix=account_suffix,
            trading_date=captured_at.date().isoformat(),
        )
        return {
            "captured_at": captured_at.isoformat(timespec="microseconds"),
            "account_suffix": account_suffix,
            "account": {
                "total_assets": account_values.get("总资产"),
                "market_value": account_values.get("总市值"),
                "available_cash": account_values.get("可用"),
                "withdrawable_cash": account_values.get("可取"),
                "floating_pnl": account_values.get("浮动盈亏"),
            },
            "positions": positions,
            "positions_complete": positions_complete,
            "orders": orders,
            "trades": trades,
        }

    def reconcile(self, event: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        """Read today's THS order/trade panes and return the authoritative state."""
        d = self.connect()
        self._ensure_simulation_order_page(d)
        self._account_guard(d)
        suffix_node = d(resourceId=f"{PACKAGE}:id/account_info_view")
        account_suffix = (
            str(suffix_node.get_text() or "").strip()
            if getattr(suffix_node, "exists", False)
            else ""
        )
        stock_name = str(event.get("stock_name") or "").strip()
        if not stock_name:
            raise BridgeError("事件缺少股票名称，无法可靠匹配同花顺委托")
        original_action = decision.get("original_action") or decision["action"]
        expected_side = "买入" if original_action == "buy" else "卖出"
        expected_quantity = int(decision["quantity"])
        expected_price = float(decision["limit_price"])
        event_time = None
        try:
            reference_time = decision.get("order_created_at") or event.get("completed_at") or ""
            event_time = datetime.fromisoformat(str(reference_time).replace("Z", "+00:00"))
        except ValueError:
            pass

        self._open_query_tab(d, resource_name="weituo", menu_name="当日委托")
        matches: list[tuple[float, dict[str, str]]] = []
        for row in self._result_rows(d):
            try:
                requested = int(float(row["first_tv"]))
                price = float(row["result2"])
            except (TypeError, ValueError):
                continue
            if (
                row["result0"] == stock_name
                and row["result6"] == expected_side
                and requested == expected_quantity
                and abs(price - expected_price) < 0.011
            ):
                distance = float("inf")
                if event_time and re.fullmatch(r"\d{2}:\d{2}:\d{2}", row["result1"]):
                    order_time = datetime.combine(event_time.date(), datetime.strptime(row["result1"], "%H:%M:%S").time())
                    if event_time.tzinfo is not None:
                        order_time = order_time.replace(tzinfo=event_time.tzinfo)
                    distance = abs((order_time - event_time).total_seconds())
                    if distance > 300:
                        continue
                matches.append((distance, row))
        if not matches:
            return {
                "status": "unknown",
                "filled_quantity": 0,
                "average_fill_price": None,
                "rejection_reason": "同花顺当日委托中尚未找到匹配记录",
            }

        matches.sort(key=lambda item: item[0])
        if len(matches) > 1 and matches[0][0] == matches[1][0]:
            return {
                "status": "unknown",
                "filled_quantity": 0,
                "average_fill_price": None,
                "rejection_reason": "同花顺存在多笔无法唯一识别的相似委托",
            }
        row = matches[0][1]
        status_text = row["result7"]
        try:
            filled_quantity = int(float(row["second_tv"] or 0))
        except ValueError:
            filled_quantity = 0
        try:
            average_fill_price = float(row["result3"]) if filled_quantity else None
        except ValueError:
            average_fill_price = None
        if "全部成交" in status_text or (filled_quantity == expected_quantity and filled_quantity > 0):
            status = "filled"
        elif filled_quantity > 0 or "部成" in status_text:
            status = "partially_filled"
        elif any(word in status_text for word in ("已撤", "撤单", "已取消")):
            status = "cancelled"
        elif any(word in status_text for word in ("废单", "拒绝", "无效")):
            status = "rejected"
        else:
            status = "accepted"
        return {
            "status": status,
            "filled_quantity": filled_quantity,
            "average_fill_price": average_fill_price,
            "rejection_reason": status_text if status == "rejected" else None,
            "external_order_id": self._order_identity(
                account_suffix,
                (event_time or datetime.now().astimezone()).date().isoformat(),
                row,
            ),
            "ths_order": row,
        }

    def cancel(self, event: dict[str, Any], decision: dict[str, Any], *, live: bool) -> dict[str, Any]:
        """Cancel the exact matching THS order and return its post-cancel state."""
        if decision.get("action") != "cancel":
            raise BridgeError("撤单执行器只接受 cancel 动作")
        if live and not self.cfg.allow_orders:
            raise BridgeError("实点被锁定：还需设置 ALLOW_SIMULATED_ORDERS=true")
        current = self.reconcile(event, decision)
        if current["status"] in {"filled", "cancelled", "rejected"}:
            return current
        satisfied = _cancel_satisfied_by_absent_order(current)
        if satisfied is not None:
            return satisfied
        if not live:
            return {**current, "mode": "dry-run", "result": "cancel_validated"}
        row = current.get("ths_order")
        if not isinstance(row, dict) or not isinstance(row.get("_index"), int):
            raise ExecutionUncertain("同花顺当日委托中未定位到可撤销记录")

        d = self.connect()
        self._ensure_simulation_order_page(d)
        self._account_guard(d)
        self._open_query_tab(d, resource_name="weituo", menu_name="当日委托")
        containers = d(resourceId=f"{PACKAGE}:id/container")
        if row["_index"] >= containers.count:
            raise ExecutionUncertain("同花顺委托列表已变化，撤单前需要重新确认")
        container = containers[row["_index"]]
        cancel_button = container.child(resourceId=f"{PACKAGE}:id/chedan_layout")
        if not cancel_button.exists:
            raise ExecutionUncertain("匹配委托当前没有可用的撤单按钮")
        # 列表滚动或页面切换都可能带出营销层，点撤单前再确认一次。
        self._dismiss_marketing_popups(d)
        cancel_button.click()
        title = d(resourceId=f"{PACKAGE}:id/dialog_title")
        if not title.wait(timeout=3) or "撤" not in str(title.get_text() or ""):
            raise ExecutionUncertain("点击撤单后未出现预期确认框")
        confirm = d(resourceId=f"{PACKAGE}:id/ok_btn", textMatches=r"(?:确认撤单|确定)")
        if not confirm.wait(timeout=3):
            raise ExecutionUncertain("撤单确认框中没有确认按钮")
        self._dismiss_marketing_popups(d)
        confirm.click()
        prompt = d(resourceId=f"{PACKAGE}:id/prompt_content")
        if not prompt.wait(timeout=5):
            raise ExecutionUncertain("确认撤单后未显示柜台结果")
        message = str(prompt.get_text() or "")
        ok = d(resourceId=f"{PACKAGE}:id/ok_btn", text="确定")
        if not any(word in message for word in ("成功", "已提交", "已撤")):
            if ok.exists:
                ok.click()
            raise SubmissionRejected(f"模拟柜台撤单拒绝：{message}")
        if ok.exists:
            ok.click()
            time.sleep(0.5)
        update = self.reconcile(event, decision)
        if update["status"] not in {"cancelled", "filled", "partially_filled"}:
            update["status"] = "unknown"
            update["rejection_reason"] = "撤单已提交，等待同花顺委托状态确认"
        update["message"] = message
        return update

    def execute(self, decision: dict[str, Any], *, live: bool) -> dict[str, Any]:
        validate_order(decision, self.cfg)
        if live and not self.cfg.allow_orders:
            raise BridgeError("实点被锁定：还需设置 ALLOW_SIMULATED_ORDERS=true")
        d = self.connect()
        self._ensure_simulation_order_page(d)
        self._account_guard(d)
        # 进入模拟委托页的瞬间常带出营销弹层，点买入/卖出前先清掉。
        self._dismiss_marketing_popups(d)

        action_text = "买入" if decision["action"] == "buy" else "卖出"
        expected_submit = r"(?:买\s*入|卖\s*出)\(模拟炒股\)"
        d(text=action_text, resourceId=f"{PACKAGE}:id/btn").click()

        symbol = str(decision["symbol"])
        quantity = str(decision["quantity"])
        price = f"{float(decision['limit_price']):.2f}"
        if not live:
            return {"mode": "dry-run", "action": decision["action"], "symbol": symbol,
                    "quantity": int(quantity), "limit_price": float(price)}

        stock_name = str(decision.get("stock_name") or "").strip()
        self._select_stock(d, symbol, stock_name)
        # 选股搜索期间弹出的营销层会吞掉表单输入与提交点击。
        self._dismiss_marketing_popups(d)
        price_input = d(resourceId=f"{PACKAGE}:id/stockprice").child(className="android.widget.EditText")
        quantity_input = d(resourceId=f"{PACKAGE}:id/stockvolume").child(className="android.widget.EditText")
        price_input.set_text(price)
        quantity_input.set_text(quantity)
        # 委托表单的证券代码框是 auto_stockcode；stock_code_value 只存在于
        # 提交后的确认弹窗里，此处读取它会抛 UiObjectNotFoundError。
        code = d(resourceId=f"{PACKAGE}:id/auto_stockcode")
        actual = [code.get_text(), price_input.get_text(), quantity_input.get_text()]
        if actual != [symbol, price, quantity]:
            raise BridgeError(f"表单回读不一致：{actual!r}")
        submit = d(textMatches=expected_submit)
        if not submit.wait(timeout=3):
            raise BridgeError("找不到模拟委托按钮")
        submit.click()
        expected_title = "委托买入确认" if decision["action"] == "buy" else "委托卖出确认"
        if not d(resourceId=f"{PACKAGE}:id/dialog_title", text=expected_title).wait(timeout=3):
            raise BridgeError("提交后未出现预期的委托确认框")
        dialog_values = {
            "symbol": d(resourceId=f"{PACKAGE}:id/stock_code_value").get_text(),
            "quantity": d(resourceId=f"{PACKAGE}:id/number_value").get_text(),
            "price": d(resourceId=f"{PACKAGE}:id/price_value").get_text(),
        }
        if dialog_values != {"symbol": symbol, "quantity": quantity, "price": price}:
            raise BridgeError(f"确认页订单内容与指令不一致：{dialog_values!r}")
        confirm_text = "确认买入" if decision["action"] == "buy" else "确认卖出"
        confirm = d(resourceId=f"{PACKAGE}:id/ok_btn", text=confirm_text)
        if not confirm.wait(timeout=3):
            raise BridgeError("未找到确认按钮")
        confirm.click()
        prompt = d(resourceId=f"{PACKAGE}:id/prompt_content")
        if not prompt.wait(timeout=5):
            raise ExecutionUncertain("确认委托后未显示柜台结果，必须读取当日委托后才能确认")
        message = prompt.get_text()
        ok = d(resourceId=f"{PACKAGE}:id/ok_btn", text="确定")
        if "成功" not in message and "委托已提交" not in message:
            if ok.exists:
                ok.click()
            raise SubmissionRejected(f"模拟柜台拒绝：{message}")
        contract = re.search(r"合同号(?:为)?[：:]\s*(\d+)", message)
        if ok.exists:
            ok.click()
            time.sleep(0.4)
        return {"mode": "live-simulation", "action": decision["action"], "symbol": symbol,
                "quantity": int(quantity), "limit_price": float(price), "result": "submitted",
                "message": message, "contract_no": contract.group(1) if contract else None}

    @staticmethod
    def _select_stock(d, symbol: str, stock_name: str) -> None:
        """Resolve the order-form security code field to ``symbol``.

        The THS autocomplete debounces per keystroke and can silently keep a
        previous query when ``set_text`` races the IME, so the typed content is
        verified before trusting the result list.  Every click target is
        matched on the numeric code shown by ``stockcode_tv`` — including the
        name-query fallback — so a fuzzy name hit can never select the wrong
        security.
        """
        code = d(resourceId=f"{PACKAGE}:id/auto_stockcode")
        if not code.wait(timeout=5):
            raise BridgeError("找不到股票代码输入框")
        result_code = d(resourceId=f"{PACKAGE}:id/stockcode_tv", text=symbol)
        title = d(resourceId=f"{PACKAGE}:id/title_text", text="搜索结果")

        def typed() -> bool:
            try:
                return str(code.get_text() or "").strip() == query
            except Exception:
                return False

        queries = [symbol, symbol]
        if stock_name:
            queries.append(stock_name)
        for query in queries:
            # The buy/sell tab swap can briefly destroy the form views, so the
            # field may vanish between wait() and click(). Re-wait each round
            # and treat a transient lookup failure as "try the next query".
            if not code.wait(timeout=3):
                continue
            try:
                code.click()
                time.sleep(0.2)
                code.clear_text()
                code.set_text(query)
            except Exception:
                continue
            if not typed():
                # Stale text survived clear_text (e.g. it concatenated onto the
                # previous symbol); purge the field with delete keys and retry
                # once — otherwise the search can never return this symbol.
                for _ in range(24):
                    d.press("delete")
                try:
                    code.set_text(query)
                except Exception:
                    continue
                typed()
            if result_code.wait(timeout=4):
                result_code.click()
                title.wait_gone(timeout=5)
                return
            # Some devices need the IME action to fire the search overlay.
            d.press("enter")
            if result_code.wait(timeout=3):
                result_code.click()
                title.wait_gone(timeout=5)
                return
        raise BridgeError(f"搜索结果中找不到证券代码 {symbol}")


def _persist_event_result(
    event_id: str,
    *,
    state: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    submitted: bool = False,
) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "UPDATE events SET state=?,error=?,result_json=?,last_checked_at=CURRENT_TIMESTAMP,"
            "executed_at=CASE WHEN ? THEN COALESCE(executed_at,CURRENT_TIMESTAMP) ELSE executed_at END "
            "WHERE event_id=?",
            (state, error, json.dumps(result, ensure_ascii=False) if result else None, submitted, event_id),
        )


def report_execution(event: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    decision = event.get("decision") or {}
    paper_order_id = decision.get("paper_order_id")
    if not isinstance(paper_order_id, int) or paper_order_id <= 0:
        raise BridgeError("事件缺少可回写的 paper_order_id")
    payload = {
        "event_id": event["event_id"],
        "paper_order_id": paper_order_id,
        "status": update["status"],
        "filled_quantity": int(update.get("filled_quantity") or 0),
        "average_fill_price": update.get("average_fill_price"),
        "rejection_reason": update.get("rejection_reason"),
        "external_order_id": update.get("external_order_id"),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    secret = Config().webhook_secret
    signature = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    req = Request(
        Config().base_url + "/api/internal/watch-trader/android-bridge/execution",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Watch-Trader-Bridge-Signature": signature,
        },
        method="POST",
    )
    with _DIRECT_OPENER.open(req, timeout=15) as response:
        return json.load(response)


def _has_reportable_order(event: dict[str, Any]) -> bool:
    paper_order_id = (event.get("decision") or {}).get("paper_order_id")
    return isinstance(paper_order_id, int) and paper_order_id > 0


def _cancel_satisfied_by_absent_order(update: dict[str, Any]) -> dict[str, Any] | None:
    """Cancel-only reconciliation: the THS daily-order list retains every
    submitted order (filled, cancelled and rejected alike), so a complete
    absence of matches means the order never existed on the broker side.
    The cancel goal is therefore already met and must be reported as
    cancelled — reporting "unknown" leaves the backend order stuck in
    cancel_requested forever."""
    if update.get("status") == "unknown" and "尚未找到匹配记录" in str(
        update.get("rejection_reason") or ""
    ):
        return {
            "status": "cancelled",
            "filled_quantity": 0,
            "average_fill_price": None,
            "rejection_reason": None,
            "message": "同花顺当日委托无匹配记录，按订单不存在处理",
        }
    return None


def report_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Send a signed, read-only full account snapshot to the backend."""
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    secret = Config().webhook_secret
    signature = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    req = Request(
        Config().base_url + "/api/internal/watch-trader/android-bridge/snapshot",
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Watch-Trader-Bridge-Signature": signature,
        },
        method="POST",
    )
    with _DIRECT_OPENER.open(req, timeout=20) as response:
        return json.load(response)


def process_event(event: dict[str, Any], *, live: bool = False) -> dict[str, Any]:
    cfg = Config()
    state, decision = validate_event(event)
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    with sqlite3.connect(DB_PATH) as db:
        try:
            db.execute(
                "INSERT INTO events(event_id,turn_id,symbol,action,validation_status,state,payload_json) VALUES(?,?,?,?,?,?,?)",
                (event["event_id"], event.get("turn_id"), event.get("symbol"),
                 decision.get("action") if decision else None,
                 decision.get("validation_status") if decision else None, state, payload),
            )
        except sqlite3.IntegrityError:
            return {"event_id": event["event_id"], "state": "duplicate", "executed": False}
    result: dict[str, Any] = {"event_id": event["event_id"], "state": state, "executed": False}
    if decision and decision.get("action") in {"buy", "sell", "cancel"}:
        try:
            normalized = execution_decision(event, decision)
            with UI_LOCK:
                simulator = ThsSimulator(cfg)
                if normalized["action"] == "cancel":
                    update = simulator.cancel(event, normalized, live=live)
                    order = {"mode": "live-simulation" if live else "dry-run", "result": "cancel_requested"}
                else:
                    order = simulator.execute(normalized, live=live)
                    update = simulator.reconcile(event, normalized) if live else None
            if not live:
                result.update(state="dry_run", order=order, ths=update)
                _persist_event_result(event["event_id"], state="dry_run", result=order)
                return result
            assert update is not None
            result.update(
                submitted=True,
                executed=update["status"] == "filled",
                state=update["status"],
                order=order,
                ths=update,
            )
            _persist_event_result(
                event["event_id"], state=update["status"], result=result, submitted=True,
            )
            try:
                result["callback"] = report_execution(event, update)
                _persist_event_result(
                    event["event_id"], state=update["status"], result=result, submitted=True,
                )
            except Exception as callback_exc:
                result["callback_error"] = str(callback_exc)
                _persist_event_result(
                    event["event_id"],
                    state="callback_pending",
                    result=result,
                    error=str(callback_exc),
                    submitted=True,
                )
        except SubmissionRejected as exc:
            rejected_status = "unknown" if decision.get("action") == "cancel" else "rejected"
            update = {
                "status": rejected_status,
                "filled_quantity": 0,
                "average_fill_price": None,
                "rejection_reason": str(exc),
            }
            try:
                result["callback"] = report_execution(event, update)
            except Exception as callback_exc:
                result["callback_error"] = str(callback_exc)
            result.update(ths=update, state=rejected_status, blocked=str(exc))
            persisted_state = "callback_pending" if "callback_error" in result else rejected_status
            _persist_event_result(event["event_id"], state=persisted_state, result=result, error=str(exc))
        except ExecutionUncertain as exc:
            update = {
                "status": "unknown",
                "filled_quantity": 0,
                "average_fill_price": None,
                "rejection_reason": str(exc),
            }
            try:
                result["callback"] = report_execution(event, update)
            except Exception as callback_exc:
                result["callback_error"] = str(callback_exc)
            result.update(state="unknown", blocked=str(exc))
            _persist_event_result(event["event_id"], state="unknown", result=result, error=str(exc), submitted=True)
        except BridgeError as exc:
            # BridgeError is raised before the final submit confirmation (for
            # example, when the security search cannot find the requested
            # symbol).  Persisting it only in the bridge DB leaves the backend
            # order falsely "accepted" forever, so report a terminal rejection.
            update = {
                "status": "rejected",
                "filled_quantity": 0,
                "average_fill_price": None,
                "rejection_reason": str(exc),
            }
            if _has_reportable_order(event):
                try:
                    result["callback"] = report_execution(event, update)
                except Exception as callback_exc:
                    result["callback_error"] = str(callback_exc)
            result.update(ths=update, state="rejected", blocked=str(exc))
            persisted_state = "callback_pending" if "callback_error" in result else "rejected"
            _persist_event_result(event["event_id"], state=persisted_state, result=result, error=str(exc))
        except Exception as exc:
            # An unexpected exception cannot prove that no tap reached the
            # broker UI.  Report an unknown state so the backend stops treating
            # the order as a healthy accepted order and requires reconciliation.
            update = {
                "status": "unknown",
                "filled_quantity": 0,
                "average_fill_price": None,
                "rejection_reason": str(exc),
            }
            if _has_reportable_order(event):
                try:
                    result["callback"] = report_execution(event, update)
                except Exception as callback_exc:
                    result["callback_error"] = str(callback_exc)
            result.update(ths=update, state="unknown", blocked=str(exc))
            persisted_state = "callback_pending" if "callback_error" in result else "unknown"
            _persist_event_result(
                event["event_id"], state=persisted_state, result=result, error=str(exc), submitted=True,
            )
    return result


def _park_stale_event(event_id: str, reason: str, *, state: str = "stale_abandoned") -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "UPDATE events SET state=?,error=?,last_checked_at=CURRENT_TIMESTAMP WHERE event_id=?",
            (state, reason, event_id),
        )
    LOG.warning("隔天事件 %s 停止对账重试：%s", event_id[:8], reason)


def _load_cancelled_order_ids() -> dict[int, dict[str, Any]]:
    """汇总本地事件账本里已终态收口且零成交的撤单事件。

    返回 {paper_order_id: {"source_event_id": 撤单事件id}}。撤单带成交
    （撤单-成交竞争）的事件不进映射——那类订单的提交事件仍需走面板/历史
    成交路径如实上报成交量，不能按"零成交撤单"一笔勾销。

    背景：THS 模拟盘"当日委托"只显示在途委托，撤单后该行立即从面板消失
    （2026-11-02 订单115 全天实证，收盘后空面板复核）。因此已撤单订单的
    提交事件永远无法在当日委托中匹配到行，对账器会每 15 秒导航一次界面
    空转直到隔天；提交事件改由本映射本地收口。
    """
    try:
        with sqlite3.connect(DB_PATH) as db:
            rows = db.execute(
                "SELECT event_id, payload_json, result_json FROM events "
                "WHERE action='cancel' AND state='cancelled'"
            ).fetchall()
    except sqlite3.Error:
        return {}
    cancelled: dict[int, dict[str, Any]] = {}
    for event_id, payload_json, result_json in rows:
        try:
            event = json.loads(payload_json or "{}")
            result = json.loads(result_json or "{}")
        except (TypeError, ValueError):
            continue
        decision = event.get("decision") or {}
        order_id = decision.get("paper_order_id")
        if not isinstance(order_id, int) or order_id <= 0:
            continue
        ths = result.get("ths") if isinstance(result, dict) else None
        if not isinstance(ths, dict) or ths.get("status") != "cancelled":
            continue
        if int(ths.get("filled_quantity") or 0) > 0:
            continue
        cancelled.setdefault(order_id, {"source_event_id": event_id})
    return cancelled


def _load_claimed_fills() -> dict[tuple[str, str, str], int]:
    """汇总本地事件账本里已被其他订单终态回报占用的历史成交。

    返回 {(交易日YYYYMMDD, 股票名, 方向文案): 已占用股数}。同一订单的多个事件
    （提交/撤单）按 paper_order_id 去重取最大成交量，避免同一笔成交被重复扣减。
    """
    try:
        with sqlite3.connect(DB_PATH) as db:
            rows = db.execute(
                "SELECT event_id, payload_json, result_json, received_at FROM events "
                "WHERE result_json LIKE '%filled_quantity%'"
            ).fetchall()
    except sqlite3.Error:
        return {}
    best_by_order: dict[tuple[int, str], tuple[tuple[str, str, str], int]] = {}
    fallback: dict[tuple[str, str, str], int] = {}
    claimed: dict[tuple[str, str, str], int] = {}
    for event_id, payload_json, result_json, received_at in rows:
        try:
            event = json.loads(payload_json or "{}")
            result = json.loads(result_json or "{}")
        except (TypeError, ValueError):
            continue
        ths = result.get("ths") if isinstance(result, dict) else None
        if not isinstance(ths, dict) or ths.get("status") not in ("filled", "partially_filled"):
            continue
        filled = int(ths.get("filled_quantity") or 0)
        if filled <= 0:
            continue
        decision = event.get("decision") or {}
        expectation = _stale_event_expectation(event, decision)
        if expectation is None:
            continue
        trading_date = _event_trading_date(event, str(received_at or ""))
        if not trading_date:
            continue
        key = (trading_date.replace("-", ""), expectation[0], expectation[1])
        order_id = decision.get("paper_order_id")
        if isinstance(order_id, int) and order_id > 0:
            prev = best_by_order.get((order_id, expectation[1]))
            if prev is None or filled > prev[1]:
                best_by_order[(order_id, expectation[1])] = (key, filled)
        else:
            fallback[key] = fallback.get(key, 0) + filled
    for key, filled in best_by_order.values():
        claimed[key] = claimed.get(key, 0) + filled
    for key, filled in fallback.items():
        claimed[key] = claimed.get(key, 0) + filled
    return claimed


def _settle_stale_events(jobs: list[tuple[str, dict[str, Any], dict[str, Any], str]]) -> int:
    """隔天未决订单收口，整批只导航一次界面。

    当日委托只在当天保留、历史委托不受支持，隔天唯一还能查到的是“历史成交”
    面板（默认近7日窗口）。扫描一次成交记录，按 交易日+名称+方向 归集：
    查到足额成交按成交上报；查无成交按撤销收口（模拟柜台限价委托当日有效，
    隔天必然失效）；无法唯一归属、页面读不到或超过7日可查窗口时，本地落库
    停止重试，绝不过度上报或反复导航。
    """
    settled = 0
    today = datetime.now().astimezone().date()
    scanable: list[tuple[str, dict[str, Any], dict[str, Any], str]] = []
    for job in jobs:
        event_id, _event, _original, trading_date = job
        age = (today - datetime.strptime(trading_date, "%Y-%m-%d").date()).days
        if age > _HISTORY_DEAL_MAX_AGE_DAYS:
            _park_stale_event(
                event_id,
                f"委托日 {trading_date} 已超过历史成交面板的可查窗口（{_HISTORY_DEAL_MAX_AGE_DAYS}日），停止对账",
            )
            settled += 1
        else:
            scanable.append(job)
    history: list[dict[str, Any]] | None = None
    history_error = ""
    if scanable:
        try:
            with UI_LOCK:
                history = ThsSimulator(Config()).read_recent_trade_history()
        except Exception as exc:
            history_error = str(exc)[:150]
            LOG.warning("读取历史成交失败，隔天事件将本地收口停止重试：%s", exc)
    claimed_fills = _load_claimed_fills() if history is not None else {}
    # 同键多单按提交时刻先后认领成交：先提交的订单先认领较早的成交行，与
    # “成交不可能早于委托”的物理顺序一致；无锚点的事件排最后兜底。
    scanable.sort(key=lambda job: (
        _event_submitted_at(job[1], job[2]) or datetime(9999, 12, 31, tzinfo=timezone.utc),
        job[0],
    ))
    for event_id, event, original, trading_date in scanable:
        update: dict[str, Any] | None = None
        if history is not None:
            expectation = _stale_event_expectation(event, original)
            if expectation is not None:
                claim_key = (trading_date.replace("-", ""), expectation[0], expectation[1])
                update = _match_history_fills(
                    history,
                    stock_name=expectation[0],
                    side_text=expectation[1],
                    quantity=expectation[2],
                    trading_day=trading_date,
                    limit_price=expectation[3],
                    claimed_quantity=claimed_fills.get(claim_key, 0),
                    submitted_at=_event_submitted_at(event, original),
                )
                if update is not None and int(update.get("filled_quantity") or 0) > 0:
                    # 同批内的后续隔天事件不得再认领本次占用的成交。
                    claimed_fills[claim_key] = claimed_fills.get(claim_key, 0) + int(
                        update["filled_quantity"]
                    )
        try:
            if update is None:
                if history is None:
                    reason = f"读取历史成交失败（{history_error}），停止对账"
                elif _stale_event_expectation(event, original) is None:
                    reason = "事件缺少股票名称/方向/数量，无法在历史成交中核对，停止对账"
                else:
                    reason = "历史成交中同日同向成交量与委托数量不一致，无法唯一归属，停止对账"
                _park_stale_event(event_id, reason)
                settled += 1
                continue
            callback = report_execution(event, update)
            _persist_event_result(
                event_id,
                state=update["status"],
                result={"ths": update, "callback": callback},
            )
            LOG.warning(
                "隔天事件 %s（%s）已按 %s 收口", event_id[:8], trading_date, update["status"],
            )
            settled += 1
        except HTTPError as exc:
            # 4xx 表示后端认定该回报永久无效（订单已终态等），同样停止重试。
            if exc.code in (400, 401, 404, 409):
                _park_stale_event(
                    event_id,
                    f"backend rejected stale settlement: HTTP {exc.code}",
                    state="report_rejected",
                )
            else:
                _park_stale_event(event_id, f"隔天事件上报失败（HTTP {exc.code}），停止对账")
            settled += 1
        except Exception as exc:
            _park_stale_event(event_id, f"隔天事件上报失败（{exc}），停止对账")
            settled += 1
    return settled


def reconcile_pending_once() -> int:
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute(
            "SELECT event_id,payload_json,state,error,result_json,received_at FROM events "
            "WHERE state IN ('accepted','partially_filled','unknown','submitted','callback_pending','execution_failed') "
            "ORDER BY received_at LIMIT 20"
        ).fetchall()
    reconciled = 0
    today = datetime.now().astimezone().date().isoformat()
    stale_jobs: list[tuple[str, dict[str, Any], dict[str, Any], str]] = []
    fresh_rows: list[tuple[Any, ...]] = []
    cancelled_orders = _load_cancelled_order_ids()
    for row in rows:
        event_id, payload_json, persisted_state, _persisted_error, _persisted_result, received_at = row
        try:
            event = json.loads(payload_json)
        except (TypeError, ValueError):
            continue
        original = event.get("decision")
        if not isinstance(original, dict):
            continue
        trading_date = _event_trading_date(event, str(received_at or ""))
        if persisted_state != "execution_failed" and trading_date and trading_date < today:
            # 当日委托只在当天保留：隔天事件走历史成交收口，不再走当日对账路径。
            stale_jobs.append((event_id, event, original, trading_date))
        else:
            fresh_rows.append(row)
    if stale_jobs:
        reconciled += _settle_stale_events(stale_jobs)
    for event_id, payload_json, persisted_state, persisted_error, persisted_result_json, _received in fresh_rows:
        try:
            event = json.loads(payload_json)
            original = event.get("decision")
            if not isinstance(original, dict):
                continue
            if persisted_state == "execution_failed":
                # Older bridge builds recorded pre-submit lookup failures only
                # in the local DB.  Repair the known-safe case on startup so the
                # backend does not retain a false accepted order indefinitely.
                if not isinstance(original.get("paper_order_id"), int):
                    continue
                error_text = str(persisted_error or "")
                try:
                    persisted_result = json.loads(persisted_result_json or "{}")
                except (TypeError, ValueError):
                    persisted_result = {}
                original_failure = str(persisted_result.get("blocked") or "")
                if "搜索结果中找不到证券代码" in original_failure:
                    error_text = original_failure
                if "搜索结果中找不到证券代码" not in error_text:
                    continue
                update = {
                    "status": "rejected",
                    "filled_quantity": 0,
                    "average_fill_price": None,
                    "rejection_reason": error_text,
                }
                callback = report_execution(event, update)
                result = {"ths": update, "callback": callback, "legacy_repair": True}
                _persist_event_result(event_id, state="rejected", result=result, error=error_text)
                reconciled += 1
                continue
            with sqlite3.connect(DB_PATH) as db:
                stored = db.execute(
                    "SELECT state,result_json FROM events WHERE event_id=?", (event_id,),
                ).fetchone()
            stored_result = json.loads(stored[1]) if stored and stored[1] else {}
            update = stored_result.get("ths") if stored and stored[0] == "callback_pending" else None
            if not isinstance(update, dict):
                # 同订单已有零成交的终态撤单事件时，提交事件直接按撤单本地收口，
                # 不再导航当日委托——THS 该面板只显示在途委托，已撤单的行永远
                # 匹配不到（2026-11-02 订单115），空转只会反复打开查询页。
                cancelled_by = cancelled_orders.get(original.get("paper_order_id")) \
                    if isinstance(original.get("paper_order_id"), int) else None
                if cancelled_by and original.get("action") in ("buy", "sell"):
                    update = {
                        "status": "cancelled",
                        "filled_quantity": 0,
                        "average_fill_price": None,
                        "rejection_reason": (
                            "同订单撤单事件已终态收口（当日委托只显示在途委托，"
                            "已撤单不再展示），按零成交撤单本地结算"
                        ),
                    }
                    callback = report_execution(event, update)
                    result = {
                        "ths": update,
                        "callback": callback,
                        "cancelled_by_event": cancelled_by.get("source_event_id"),
                    }
                    _persist_event_result(event_id, state="cancelled", result=result)
                    reconciled += 1
                    LOG.info(
                        "提交事件 %s 按同订单撤单终态本地收口（cancel 事件 %s）",
                        event_id[:8], str(cancelled_by.get("source_event_id"))[:8],
                    )
                    continue
                decision = execution_decision(event, original)
                with UI_LOCK:
                    update = ThsSimulator(Config()).reconcile(event, decision)
            if original.get("action") == "cancel":
                satisfied = _cancel_satisfied_by_absent_order(update)
                if satisfied is not None:
                    update = satisfied
            callback = report_execution(event, update)
            result = {"ths": update, "callback": callback}
            _persist_event_result(event_id, state=update["status"], result=result)
            reconciled += 1
        except HTTPError as exc:
            LOG.warning("Reconcile report rejected for %s: HTTP %s", event_id, exc.code)
            with sqlite3.connect(DB_PATH) as db:
                if exc.code in (400, 401, 404, 409):
                    # A 4xx means the backend considers this report permanently
                    # invalid (order already terminal, stale decision link,
                    # signature/config issue).  Retrying can never succeed, so
                    # park the event outside the pending set.
                    db.execute(
                        "UPDATE events SET state='report_rejected',error=?,"
                        "last_checked_at=CURRENT_TIMESTAMP WHERE event_id=?",
                        (f"backend rejected execution report: HTTP {exc.code}", event_id),
                    )
                else:
                    db.execute(
                        "UPDATE events SET error=?,last_checked_at=CURRENT_TIMESTAMP WHERE event_id=?",
                        (str(exc), event_id),
                    )
        except Exception as exc:
            LOG.warning("Reconcile failed for %s: %s", event_id, exc)
            with sqlite3.connect(DB_PATH) as db:
                db.execute(
                    "UPDATE events SET error=?,last_checked_at=CURRENT_TIMESTAMP WHERE event_id=?",
                    (str(exc), event_id),
                )
    return reconciled


def reconcile_forever() -> None:
    while True:
        time.sleep(15)
        try:
            reconcile_pending_once()
        except Exception:
            LOG.exception("Periodic THS reconciliation failed")


def _watchdog_device_alive(serial: str = "") -> bool:
    """A device counts as alive only when a real shell round-trip succeeds —
    a stale adb listing after the emulator died must not pass."""
    try:
        import adbutils

        client = adbutils.AdbClient(host="127.0.0.1", port=5037)
        devices = client.device_list()
    except Exception:
        return False
    for device in devices:
        if serial and device.serial != serial:
            continue
        try:
            device.shell("echo ok")
            return True
        except Exception:
            continue
    return False


def _watchdog_emulator_running() -> bool:
    if os.name != "nt":
        return True  # no process probing on non-Windows; rely on adb reconnect
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq HD-Player.exe", "/NH"],
            timeout=10,
            stderr=subprocess.DEVNULL,
        )
        return b"HD-Player.exe" in out
    except Exception:
        return False


def _watchdog_recover_device(cfg: Config) -> None:
    import adbutils

    client = adbutils.AdbClient(host="127.0.0.1", port=5037)
    endpoint = os.getenv("BLUESTACKS_ADB_ENDPOINT", "127.0.0.1:5555")
    if not _watchdog_emulator_running():
        exe = os.getenv("BLUESTACKS_EXE", r"C:\Program Files\BlueStacks_nxt\HD-Player.exe")
        instance = os.getenv("BLUESTACKS_INSTANCE", "Pie64")
        if os.name == "nt" and Path(exe).exists():
            LOG.warning("watchdog: emulator process missing; starting %s --instance %s", exe, instance)
            subprocess.Popen(
                [exe, "--instance", instance, "--cmd", "launchApp", "--package", PACKAGE],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            LOG.warning("watchdog: device unreachable and no emulator to restart")
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        try:
            client.connect(endpoint)
        except Exception:
            pass
        try:
            devices = client.device_list()
        except Exception:
            devices = []
        for device in devices:
            if cfg.adb_serial and device.serial != cfg.adb_serial:
                continue
            try:
                if str(device.shell("getprop sys.boot_completed") or "").strip() == "1":
                    LOG.warning("watchdog: device %s recovered", device.serial)
                    return
            except Exception:
                continue
        time.sleep(3.0)
    LOG.warning("watchdog: recovery timed out; will retry on next tick")


def device_watchdog_forever() -> None:
    """Restart the BlueStacks emulator and re-attach adb when the device drops.

    Orders pushed while the device is dark can never reach THS, so this loop
    keeps the execution channel alive instead of leaving orders stranded.
    """
    cfg = Config()
    while True:
        time.sleep(30)
        try:
            if _watchdog_device_alive(cfg.adb_serial):
                continue
            LOG.warning("watchdog: adb device unreachable; attempting recovery")
            _watchdog_recover_device(cfg)
        except Exception:
            LOG.exception("device watchdog tick failed")


def full_sync_forever() -> None:
    """Periodically synchronize funds, all positions, orders and trades."""
    try:
        interval = int(os.getenv(
            "PAPER_FULL_SYNC_INTERVAL_SECONDS",
            str(DEFAULT_FULL_SYNC_INTERVAL_SECONDS),
        ))
    except ValueError:
        interval = DEFAULT_FULL_SYNC_INTERVAL_SECONDS
    interval = max(15, min(3600, interval))
    # Let the backend and emulator finish starting before the first read.
    time.sleep(5)
    while True:
        started = time.monotonic()
        try:
            with UI_LOCK:
                snapshot = ThsSimulator(Config()).snapshot(activate_app=True)
            result = report_snapshot(snapshot)
            LOG.info(
                "Periodic full sync completed (positions=%s changed_orders=%s)",
                result.get("positions_calibrated"),
                result.get("changed_orders"),
            )
        except HTTPError as exc:
            # 后端 400 时会带具体拒绝原因，必须记下来，否则只能盲猜。
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                detail = ""
            LOG.error(
                "Periodic full account synchronization failed: HTTP %s %s",
                exc.code, detail,
            )
        except Exception:
            LOG.exception("Periodic full account synchronization failed")
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, interval - elapsed))


def rest_get(path: str, params: dict[str, str] | None = None) -> Any:
    cfg = Config()
    if not cfg.token:
        raise BridgeError("未设置 WATCH_TRADER_TOKEN")
    url = cfg.base_url + path
    if params:
        url += "?" + urlencode(params)
    req = Request(url, headers={"Authorization": f"Bearer {cfg.token}", "Accept": "application/json"})
    with _DIRECT_OPENER.open(req, timeout=15) as response:
        return json.load(response)


def _execution_status(*, live: bool, cfg: Config) -> dict[str, bool]:
    return {
        "live": bool(live),
        "order_click_authorized": bool(cfg.allow_orders),
        "auto_execute": bool(live and cfg.allow_orders),
    }


class WebhookHandler(BaseHTTPRequestHandler):
    live = False

    def _json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _asset(self, name: str, content_type: str) -> None:
        path = ROOT / name
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._asset("dashboard.html", "text/html; charset=utf-8")
        elif path == "/dashboard.css":
            self._asset("dashboard.css", "text/css; charset=utf-8")
        elif path == "/dashboard.js":
            self._asset("dashboard.js", "application/javascript; charset=utf-8")
        elif path == "/api/status":
            try:
                cfg = Config()
                device = ThsSimulator(cfg).check()
                with sqlite3.connect(DB_PATH) as db:
                    webhook_row = db.execute(
                        "SELECT received_at FROM events WHERE event_id NOT LIKE 'webhook-selftest-%' "
                        "ORDER BY received_at DESC, rowid DESC LIMIT 1"
                    ).fetchone()
                self._json(200, {
                    "service": "running",
                    **_execution_status(live=self.live, cfg=cfg),
                    "webhook_url": f"http://127.0.0.1:{self.server.server_address[1]}/hooks/watch-trader",
                    "webhook": {
                        "receiver_online": True,
                        "last_delivery_at": webhook_row[0] if webhook_row else None,
                    },
                    "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "device": device,
                })
            except Exception as exc:
                cfg = Config()
                self._json(200, {
                    "service": "running",
                    **_execution_status(live=self.live, cfg=cfg),
                    "device_error": str(exc),
                })
        elif path == "/health":
            cfg = Config()
            self._json(200, {
                "status": "ok",
                "service": "watch-trader-android-bridge",
                **_execution_status(live=self.live, cfg=cfg),
            })
        elif path == "/api/events":
            with sqlite3.connect(DB_PATH) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT event_id,symbol,action,validation_status,state,error,result_json,"
                    "received_at,executed_at,last_checked_at "
                    "FROM events ORDER BY received_at DESC, rowid DESC LIMIT 50"
                ).fetchall()
            self._json(200, {"items": [dict(row) for row in rows]})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/snapshot":
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            secret = Config().webhook_secret
            received = self.headers.get("X-Watch-Trader-Bridge-Signature", "")
            expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            if not secret or not hmac.compare_digest(received, expected):
                self.send_error(401, "invalid signature")
                return
            try:
                with UI_LOCK:
                    snapshot = ThsSimulator(Config()).snapshot()
                self._json(200, snapshot)
            except BridgeError as exc:
                self._json(409, {"detail": str(exc)})
            except Exception as exc:
                LOG.exception("Snapshot failed")
                self._json(500, {"detail": str(exc)})
            return
        if path != "/hooks/watch-trader":
            self.send_error(404)
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        secret = Config().webhook_secret
        received = self.headers.get("X-Watch-Trader-Signature", "")
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not secret or not hmac.compare_digest(received, expected):
            self.send_error(401, "invalid signature")
            return
        try:
            event = json.loads(raw)
            if event.get("event_id") != self.headers.get("X-Watch-Trader-Event-Id"):
                raise BridgeError("event id mismatch")
            result = process_event(event, live=self.live)
            self._json(200, result)
        except (BridgeError, ValueError, KeyError) as exc:
            self.send_error(400, str(exc))

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info(fmt, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description="盯盘手 → 同花顺 BlueStacks 模拟交易桥")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="检查 BlueStacks 和同花顺页面")
    execute = sub.add_parser("execute", help="处理单个 JSON 事件文件")
    execute.add_argument("event_file", type=Path)
    execute.add_argument("--live", action="store_true", help="允许点击模拟下单（仍需环境变量双重授权）")
    latest = sub.add_parser("latest", help="从盯盘手读取指定股票最新分析")
    latest.add_argument("symbol")
    latest.add_argument("--live", action="store_true")
    serve = sub.add_parser("serve", help="启动 Webhook 接收器")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=DEFAULT_PORT, type=int)
    serve.add_argument("--live", action="store_true", default=True)
    serve.add_argument("--dry-run", action="store_false", dest="live")
    args = parser.parse_args()
    init_db()
    if args.command == "check":
        print(json.dumps(ThsSimulator(Config()).check(), ensure_ascii=False, indent=2))
    elif args.command == "execute":
        print(json.dumps(process_event(json.loads(args.event_file.read_text("utf-8")), live=args.live), ensure_ascii=False, indent=2))
    elif args.command == "latest":
        event = rest_get("/api/external/watch-trader/analyses/latest", {"symbol": args.symbol})
        print(json.dumps(process_event(event, live=args.live), ensure_ascii=False, indent=2))
    else:
        WebhookHandler.live = args.live
        server = ThreadingHTTPServer((args.host, args.port), WebhookHandler)
        if args.live:
            threading.Thread(
                target=reconcile_forever,
                name="ths-order-reconciler",
                daemon=True,
            ).start()
            if os.getenv("BLUESTACKS_WATCHDOG", "true").lower() != "false":
                threading.Thread(
                    target=device_watchdog_forever,
                    name="bluestacks-device-watchdog",
                    daemon=True,
                ).start()
        threading.Thread(
            target=full_sync_forever,
            name="paper-full-synchronizer",
            daemon=True,
        ).start()
        LOG.warning("Webhook listening on http://%s:%s/hooks/watch-trader (live=%s)", args.host, args.port, args.live)
        server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()

"""UI 桥契约回归测试。

运行：`python -m pytest test_bridge_contract.py -q`（需要 pytest）
全部为纯逻辑测试，不需要后端数据库。这些测试钉住桥与后端的对账契约；
修改 bridge.py 或后端对账逻辑时两边都要过。
"""
import importlib.util
import json
import os
import sqlite3
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
_MARKETING_POPUP_HIERARCHY = """<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" bounds="[0,0][1080,1920]">
    <node index="1" text="pop页面属性" resource-id="" class="android.webkit.WebView" bounds="[0,0][1080,1920]">
      <node index="2" text="" resource-id="pop-layer-pop-up-one-1752721222169" class="android.view.View" bounds="[0,0][1080,1920]">
        <node index="3" text="" resource-id="" class="android.widget.Image" bounds="[159,415][921,1504]" />
        <node index="4" text="" resource-id="" class="android.view.View" bounds="[519,1515][561,1555]" />
      </node>
    </node>
    <node index="5" text="买入" resource-id="com.hexin.plat.android:id/menu_buy_text" class="android.widget.TextView" bounds="[88,507][128,535]" />
    <node index="6" text="模拟练习区" resource-id="com.hexin.plat.android:id/division_name_text" class="android.widget.TextView" bounds="[114,130][339,163]" />
  </node>
</hierarchy>"""
_CLEAN_SIMULATION_HIERARCHY = """<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" bounds="[0,0][1080,1920]">
    <node index="1" text="买入" resource-id="com.hexin.plat.android:id/menu_buy_text" class="android.widget.TextView" bounds="[88,507][128,535]" />
    <node index="2" text="模拟练习区" resource-id="com.hexin.plat.android:id/division_name_text" class="android.widget.TextView" bounds="[114,130][339,163]" />
    <node index="3" text="委托买入确认" resource-id="com.hexin.plat.android:id/dialog_title" class="android.widget.TextView" bounds="[100,800][980,860]" />
    <node index="4" text="确认买入" resource-id="com.hexin.plat.android:id/ok_btn" class="android.widget.Button" clickable="true" bounds="[340,1000][740,1080]" />
  </node>
</hierarchy>"""
class _SelectorNode:
    def __init__(self, selector):
        self.selector = selector

    @property
    def exists(self):
        return False

    def get_text(self):
        return ""
class _HierarchyDevice:
    """Fake device that replays queued hierarchies and records every tap."""

    info = {"displayWidth": 1080}

    def __init__(self, hierarchies):
        self.hierarchies = list(hierarchies)
        self.clicks: list[tuple[int, int] | str] = []

    def app_current(self):
        return {"package": "com.hexin.plat.android"}

    def __call__(self, **selector):
        return _SelectorNode(selector)

    def dump_hierarchy(self, compressed=False):
        return self.hierarchies.pop(0) if self.hierarchies else _CLEAN_SIMULATION_HIERARCHY

    def click(self, x, y):
        self.clicks.append((x, y))

    def press(self, key):
        self.clicks.append(key)
_HISTORY_DEAL_HIERARCHY = """<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" bounds="[0,0][1080,1920]">
    <node index="1" text="凡拓数创" resource-id="" class="android.widget.TextView" bounds="[27,1435][669,1474]" />
    <node index="2" text="20261021 13:44:27" resource-id="" class="android.widget.TextView" bounds="[54,1474][669,1503]" />
    <node index="3" text="53.860" resource-id="" class="android.widget.TextView" bounds="[669,1422][806,1516]" />
    <node index="4" text="100" resource-id="" class="android.widget.TextView" bounds="[806,1422][943,1516]" />
    <node index="5" text="买入" resource-id="" class="android.widget.TextView" bounds="[943,1435][1067,1474]" />
    <node index="6" text="5386.000" resource-id="" class="android.widget.TextView" bounds="[943,1474][1067,1503]" />
    <node index="7" text="吉大正元" resource-id="" class="android.widget.TextView" bounds="[27,1530][669,1569]" />
    <node index="8" text="20261021 11:12:03" resource-id="" class="android.widget.TextView" bounds="[54,1569][669,1598]" />
    <node index="9" text="27.070" resource-id="" class="android.widget.TextView" bounds="[669,1517][806,1611]" />
    <node index="10" text="100" resource-id="" class="android.widget.TextView" bounds="[806,1517][943,1611]" />
    <node index="11" text="买入" resource-id="" class="android.widget.TextView" bounds="[943,1530][1067,1569]" />
    <node index="12" text="2707.000" resource-id="" class="android.widget.TextView" bounds="[943,1569][1067,1598]" />
  </node>
</hierarchy>"""


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def _decision(*, symbol="000001", price_type="market", limit_price=None, quantity=100):
    return SimpleNamespace(
        id=str(uuid4()),
        symbol=symbol,
        action="buy",
        quantity=quantity,
        trading_mode="auction",
        price_type=price_type,
        limit_price=limit_price,
    )


def _load_bridge_module():
    # 桥源码在本仓库根目录；这些测试把 UI 桥与后端的契约钉在一起，
    # 桥文件缺失的环境直接跳过。
    path = Path(os.getenv(
        "THS_UI_BRIDGE_PATH",
        "D:/ths-ui-bridge/bridge.py",
    ))
    if not path.is_file():
        pytest.skip(f"UI bridge source not found: {path}")
    spec = importlib.util.spec_from_file_location("stockanalyzer_watch_trader_bridge", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _insert_bridge_event(bridge, event, *, state="unknown", received_at="2026-10-21 02:13:14"):
    decision = event.get("decision") or {}
    with sqlite3.connect(bridge.DB_PATH) as db:
        db.execute(
            "INSERT INTO events(event_id,turn_id,symbol,action,validation_status,state,payload_json,received_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                event["event_id"], event.get("turn_id"), event.get("symbol"),
                decision.get("action"), decision.get("validation_status"), state,
                json.dumps(event, ensure_ascii=False), received_at,
            ),
        )


def _insert_cancelled_sibling(bridge, order_id: int, *, filled_quantity: int = 0):
    """写入一个已终态收口的撤单事件（模拟撤单流程完成后的账本形态）。"""
    event = {
        "event_id": f"cancel-{order_id}", "event_type": "analysis.completed",
        "status": "completed", "symbol": "002204", "stock_name": "大连重工",
        "decision": {"action": "cancel", "symbol": "002204", "paper_order_id": order_id},
    }
    _insert_bridge_event(
        bridge, event, state="cancelled",
        received_at="2026-10-21 03:00:00",
    )
    result = {
        "ths": {"status": "cancelled", "filled_quantity": filled_quantity,
                "average_fill_price": None, "rejection_reason": None},
    }
    with sqlite3.connect(bridge.DB_PATH) as db:
        db.execute(
            "UPDATE events SET result_json=? WHERE event_id=?",
            (json.dumps(result, ensure_ascii=False), f"cancel-{order_id}"),
        )



def test_bridge_backend_callbacks_ignore_environment_proxies():
    bridge = _load_bridge_module()

    proxy_handlers = [
        handler
        for handler in bridge._DIRECT_OPENER.handlers
        if isinstance(handler, bridge.ProxyHandler)
    ]

    # An empty ProxyHandler suppresses urllib's environment-derived default
    # handler; because it has no protocol methods, build_opener does not retain
    # it in the active handler chain.
    assert proxy_handlers == []


def test_bridge_reports_auto_execution_only_with_both_authorizations():
    bridge = _load_bridge_module()

    assert bridge._execution_status(
        live=True,
        cfg=bridge.Config(allow_orders=True),
    ) == {
        "live": True,
        "order_click_authorized": True,
        "auto_execute": True,
    }
    assert bridge._execution_status(
        live=True,
        cfg=bridge.Config(allow_orders=False),
    )["auto_execute"] is False
    assert bridge._execution_status(
        live=False,
        cfg=bridge.Config(allow_orders=True),
    )["auto_execute"] is False


def test_bridge_order_identity_is_stable_and_refuses_duplicate_rows():
    bridge = _load_bridge_module()
    simulator = bridge.ThsSimulator(bridge.Config())
    row = {
        "result0": "平安银行",
        "result1": "10:01:02",
        "result2": "10.02",
        "result3": "",
        "result6": "买入",
        "result7": "已报",
        "first_tv": "100",
        "second_tv": "0",
    }
    updated = {**row, "result3": "10.01", "result7": "全部成交", "second_tv": "100"}
    first_id = simulator._order_identity("**0000", "2026-08-15", row)
    assert simulator._order_identity("**0000", "2026-08-15", updated) == first_id

    duplicate_rows = [dict(row), dict(row)]
    simulator._annotate_order_identities(
        duplicate_rows,
        account_suffix="**0000",
        trading_date="2026-08-15",
    )
    assert all(item["identity_ambiguous"] is True for item in duplicate_rows)
    assert all("external_order_id" not in item for item in duplicate_rows)


def test_snapshot_returns_to_order_page_after_reading_holdings(monkeypatch):
    bridge = _load_bridge_module()
    calls = []

    class Node:
        exists = False

        def wait(self, timeout=0):
            return True

        def click(self):
            return None

        def get_text(self):
            return ""

    class Device:
        def __call__(self, **_kwargs):
            return Node()

        def dump_hierarchy(self, compressed=False):
            return f"""<hierarchy>
                <node resource-id="{bridge.PACKAGE}:id/capital_cell_title" text="总资产" />
                <node resource-id="{bridge.PACKAGE}:id/capital_cell_title" text="总市值" />
                <node resource-id="{bridge.PACKAGE}:id/capital_cell_title" text="可用" />
                <node resource-id="{bridge.PACKAGE}:id/capital_cell_value" text="200000.00" />
                <node resource-id="{bridge.PACKAGE}:id/capital_cell_value" text="0.00" />
                <node resource-id="{bridge.PACKAGE}:id/capital_cell_value" text="200000.00" />
            </hierarchy>"""

        def swipe(self, *_args, **_kwargs):
            return None

    simulator = bridge.ThsSimulator(bridge.Config())
    monkeypatch.setattr(simulator, "connect", lambda: Device())
    monkeypatch.setattr(simulator, "_ensure_simulation_order_page", lambda _d: calls.append("page"))
    monkeypatch.setattr(simulator, "_account_guard", lambda _d: calls.append("guard"))
    monkeypatch.setattr(simulator, "_position_rows", lambda _d: [])
    monkeypatch.setattr(simulator, "_open_query_tab", lambda _d, **kwargs: calls.append(kwargs["resource_name"]))
    monkeypatch.setattr(simulator, "_result_rows", lambda _d: [])

    simulator.snapshot()

    assert calls == [
        "page", "guard", "page", "guard", "weituo",
        "page", "guard", "chengjiao",
    ]


def test_bridge_dismisses_known_error_dialog_hiding_trading_page():
    bridge = _load_bridge_module()
    state = {"visible": True, "clicks": 0}

    class Node:
        def __init__(self, selector):
            self.selector = selector

        @property
        def exists(self):
            return state["visible"]

        def get_text(self):
            if self.selector.get("resourceId", "").endswith("dialog_title"):
                return "请求失败"
            return "确定"

        def click(self):
            state["visible"] = False
            state["clicks"] += 1

    class Device:
        def __call__(self, **selector):
            return Node(selector)

    simulator = bridge.ThsSimulator(bridge.Config())

    assert simulator._dismiss_blocking_error_dialogs(Device()) == 1
    assert state == {"visible": False, "clicks": 1}


def test_bridge_never_dismisses_transaction_confirmation_dialog():
    bridge = _load_bridge_module()
    clicks = []

    class Node:
        def __init__(self, selector):
            self.selector = selector

        @property
        def exists(self):
            return self.selector.get("resourceId", "").endswith(("dialog_title", "left_btn"))

        def get_text(self):
            if self.selector.get("resourceId", "").endswith("dialog_title"):
                return "委托确认"
            return "确定"

        def click(self):
            clicks.append(self.selector)

    class Device:
        def __call__(self, **selector):
            return Node(selector)

    simulator = bridge.ThsSimulator(bridge.Config())

    assert simulator._dismiss_blocking_error_dialogs(Device()) == 0
    assert clicks == []


def test_bridge_dismisses_marketing_popup_by_clicking_close_below_card(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    device = _HierarchyDevice([_MARKETING_POPUP_HIERARCHY])

    actions = bridge.ThsSimulator(bridge.Config())._dismiss_marketing_popups(device)

    assert actions == 1
    # 真机验证过的关闭点：卡片下沿居中的无文字小节点。
    assert device.clicks == [(540, 1535)]


def test_bridge_marks_dont_show_before_closing_marketing_popup(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    with_dont_show = _MARKETING_POPUP_HIERARCHY.replace(
        '<node index="4" text="" resource-id="" class="android.view.View" bounds="[519,1515][561,1555]" />',
        '<node index="4" text="不再展示" resource-id="" class="android.widget.CheckBox"'
        ' clickable="true" checked="false" bounds="[470,1560][610,1600]" />'
        '<node index="7" text="关闭" resource-id="" class="android.view.View"'
        ' clickable="true" bounds="[900,300][960,360]" />',
    )
    device = _HierarchyDevice([with_dont_show, with_dont_show, _CLEAN_SIMULATION_HIERARCHY])

    actions = bridge.ThsSimulator(bridge.Config())._dismiss_marketing_popups(device)

    # 先勾“不再展示”，第二轮再点关闭；勾选项不会因 checked 未回写而被反复点。
    assert actions == 2
    assert device.clicks == [(540, 1580), (930, 330)]


def test_bridge_folds_marketing_popup_via_dim_area_and_back_key(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    card_only = _MARKETING_POPUP_HIERARCHY.replace(
        '        <node index="4" text="" resource-id="" class="android.view.View" bounds="[519,1515][561,1555]" />\n',
        "",
    )
    device = _HierarchyDevice([card_only, card_only, card_only])

    actions = bridge.ThsSimulator(bridge.Config())._dismiss_marketing_popups(device)

    assert actions == 3
    # 没有关闭控件时点击卡片上方的遮罩空白区，三处落点都在 dim 区同一位置。
    assert device.clicks == [(540, 355), (540, 355), (540, 355)]


def test_bridge_never_touches_ui_without_ad_markers(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    device = _HierarchyDevice([_CLEAN_SIMULATION_HIERARCHY])

    actions = bridge.ThsSimulator(bridge.Config())._dismiss_marketing_popups(device)

    assert actions == 0
    assert device.clicks == []


def test_bridge_uses_back_key_when_navigation_fails_with_ad_marker(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    native_dialog = """<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" bounds="[0,0][1080,1920]">
    <node index="1" text="金豆会员日" resource-id="" class="android.widget.TextView" bounds="[330,320][750,380]" />
    <node index="2" text="立即参与" resource-id="" class="android.widget.Button" clickable="true" bounds="[340,1400][740,1480]" />
  </node>
</hierarchy>"""
    device = _HierarchyDevice([native_dialog])

    actions = bridge.ThsSimulator(bridge.Config())._dismiss_marketing_popups(device, symptom=True)

    # 原生广告对话框没有全屏浮层可点，症状模式下用一次返回键收起即交回调用方。
    assert actions == 1
    assert device.clicks == ["back"]


def test_bridge_check_clears_marketing_popup_before_probing_visibility(monkeypatch):
    bridge = _load_bridge_module()
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    device = _HierarchyDevice([_MARKETING_POPUP_HIERARCHY])
    monkeypatch.setattr(bridge.ThsSimulator, "connect", lambda self: device)
    monkeypatch.setattr(device, "app_current", lambda: {"package": bridge.PACKAGE})
    monkeypatch.setattr(device, "info", {"displayWidth": 1080})

    bridge.ThsSimulator(bridge.Config()).check()

    # 状态探测前必须先关掉残留弹层，否则 visibility 探针全部误报为被遮挡。
    assert device.clicks == [(540, 1535)]


def test_bridge_parses_history_deal_rows_from_real_layout():
    bridge = _load_bridge_module()
    rows = bridge._parse_history_deal_rows(ET.fromstring(_HISTORY_DEAL_HIERARCHY))

    assert rows == [
        {"trading_day": "20261021", "time": "20261021 13:44:27", "name": "凡拓数创",
         "price": 53.86, "quantity": 100, "side": "买入", "amount": 5386.0},
        {"trading_day": "20261021", "time": "20261021 11:12:03", "name": "吉大正元",
         "price": 27.07, "quantity": 100, "side": "买入", "amount": 2707.0},
    ]


def test_bridge_event_trading_date_prefers_payload_then_utc_fallback():
    bridge = _load_bridge_module()
    event = {
        "trading_date": "2026-10-15",
        "decision": {"paper_order": {"created_at": "2026-10-15T09:33:48+08:00"}},
    }

    assert bridge._event_trading_date(event, "2026-10-20 01:33:49") == "2026-10-15"
    # 只有 received_at（UTC naive）时换算成北京日期。
    assert bridge._event_trading_date({}, "2026-10-20 17:00:00") == "2026-10-21"
    assert bridge._event_trading_date({}, "not-a-date") is None


def test_bridge_settles_stale_events_from_trade_history(tmp_path, monkeypatch):
    bridge = _load_bridge_module()
    bridge.DB_PATH = tmp_path / "bridge.db"
    bridge.init_db()
    # 交易日随当前日期回退2天：历史成交面板只保留近7日，硬编码日期会让测试随日历失效。
    stale_day = datetime.now().astimezone().date() - timedelta(days=2)
    day_iso = stale_day.isoformat()
    day_compact = stale_day.strftime("%Y%m%d")
    filled_event = {
        "event_id": "stale-filled", "event_type": "analysis.completed", "status": "completed",
        "symbol": "002164", "stock_name": "宁波东力", "trading_date": day_iso,
        "decision": {"action": "buy", "symbol": "002164", "quantity": 100, "price_type": "limit",
                      "limit_price": 15.9, "validation_status": "accepted", "paper_order_id": 7},
    }
    # 同日同股存在更高价的成交（属于别的委托），限价 15.5 的委托不可能以 15.9/16.45
    # 成交——价格可行性过滤必须把它排除，按撤销收口而不是错报成交。
    pricier_event = {
        "event_id": "stale-priced-out", "event_type": "analysis.completed", "status": "completed",
        "symbol": "002164", "stock_name": "宁波东力", "trading_date": day_iso,
        "decision": {"action": "buy", "symbol": "002164", "quantity": 100, "price_type": "limit",
                      "limit_price": 15.5, "validation_status": "accepted", "paper_order_id": 8},
    }
    _insert_bridge_event(bridge, filled_event)
    _insert_bridge_event(bridge, pricier_event)
    history = [
        {"trading_day": day_compact, "time": f"{day_compact} 10:20:00", "name": "宁波东力",
         "price": 15.9, "quantity": 100, "side": "买入", "amount": 1590.0},
        {"trading_day": day_compact, "time": f"{day_compact} 11:11:27", "name": "宁波东力",
         "price": 16.45, "quantity": 100, "side": "买入", "amount": 1645.0},
    ]
    scans = []
    monkeypatch.setattr(
        bridge.ThsSimulator, "read_recent_trade_history",
        lambda self: (scans.append(1), history)[1],
    )
    reports = []
    monkeypatch.setattr(
        bridge, "report_execution",
        lambda event, update: reports.append((event["event_id"], update["status"])) or {"ok": True},
    )

    reconciled = bridge.reconcile_pending_once()

    assert reconciled == 2
    assert scans == [1]  # 整批只扫描一次历史成交
    assert reports == [("stale-filled", "filled"), ("stale-priced-out", "cancelled")]
    with sqlite3.connect(bridge.DB_PATH) as db:
        states = dict(db.execute("SELECT event_id,state FROM events").fetchall())
    assert states["stale-filled"] == "filled"
    assert states["stale-priced-out"] == "cancelled"


def test_bridge_parks_ambiguous_stale_event_without_reporting(tmp_path, monkeypatch):
    bridge = _load_bridge_module()
    bridge.DB_PATH = tmp_path / "bridge.db"
    bridge.init_db()
    stale_day = datetime.now().astimezone().date() - timedelta(days=2)
    day_iso = stale_day.isoformat()
    day_compact = stale_day.strftime("%Y%m%d")
    ambiguous_event = {
        "event_id": "stale-ambiguous", "event_type": "analysis.completed", "status": "completed",
        "symbol": "002164", "stock_name": "宁波东力", "trading_date": day_iso,
        "decision": {"action": "buy", "symbol": "002164", "quantity": 100, "price_type": "limit",
                      "limit_price": 16.5, "validation_status": "accepted", "paper_order_id": 9},
    }
    _insert_bridge_event(bridge, ambiguous_event)
    history = [
        {"trading_day": day_compact, "time": f"{day_compact} 10:20:00", "name": "宁波东力",
         "price": 16.0, "quantity": 100, "side": "买入", "amount": 1600.0},
        {"trading_day": day_compact, "time": f"{day_compact} 11:11:27", "name": "宁波东力",
         "price": 16.45, "quantity": 100, "side": "买入", "amount": 1645.0},
    ]
    monkeypatch.setattr(bridge.ThsSimulator, "read_recent_trade_history", lambda self: history)
    reports = []
    monkeypatch.setattr(
        bridge, "report_execution",
        lambda event, update: reports.append(event["event_id"]) or {"ok": True},
    )

    bridge.reconcile_pending_once()

    # 同日同向成交 200 股多于委托 100 股：不猜测归属、不上报，本地落库停止重试。
    assert reports == []
    with sqlite3.connect(bridge.DB_PATH) as db:
        state, error = db.execute(
            "SELECT state,error FROM events WHERE event_id='stale-ambiguous'"
        ).fetchone()
    assert state == "stale_abandoned"
    assert "无法唯一归属" in error


def test_bridge_parks_stale_event_beyond_history_window_without_ui(tmp_path, monkeypatch):
    bridge = _load_bridge_module()
    bridge.DB_PATH = tmp_path / "bridge.db"
    bridge.init_db()
    ancient_event = {
        "event_id": "stale-ancient", "event_type": "analysis.completed", "status": "completed",
        "symbol": "600000", "stock_name": "浦发银行", "trading_date": "2026-08-01",
        "decision": {"action": "buy", "symbol": "600000", "quantity": 100, "price_type": "limit",
                      "limit_price": 8.0, "validation_status": "accepted", "paper_order_id": 11},
    }
    _insert_bridge_event(bridge, ancient_event, received_at="2026-08-01 01:00:00")
    monkeypatch.setattr(
        bridge.ThsSimulator, "read_recent_trade_history",
        lambda self: (_ for _ in ()).throw(AssertionError("超过7日窗口的事件不应再导航界面")),
    )

    bridge.reconcile_pending_once()

    with sqlite3.connect(bridge.DB_PATH) as db:
        state, error = db.execute(
            "SELECT state,error FROM events WHERE event_id='stale-ancient'"
        ).fetchone()
    assert state == "stale_abandoned"
    assert "7日" in error


def test_bridge_does_not_mistake_history_pane_for_order_page(monkeypatch):
    bridge = _load_bridge_module()
    simulator = bridge.ThsSimulator(bridge.Config())
    # 历史成交面板：页面标题同样是“模拟炒股”，但没有按钮行的“买入”锚点。
    class Node:
        def __init__(self, selector):
            self.selector = selector

        @property
        def exists(self):
            return self.selector.get("text") == "模拟炒股"

        def wait(self, timeout=0):
            return False

        def click(self):
            return None

        def get_text(self):
            return ""

    class Device:
        def __call__(self, **selector):
            return Node(selector)

        def app_current(self):
            return {"package": bridge.PACKAGE}

        def dump_hierarchy(self, compressed=False):
            return "<hierarchy><node text=\"\" resource-id=\"\" bounds=\"[0,0][1080,1920]\" /></hierarchy>"

        def press(self, key):
            return None

    monkeypatch.setattr(simulator, "_clear_blocking_overlays", lambda _d: None)

    # 不能把查询面板误判成委托页静默返回；应继续导航并因找不到入口报错。
    try:
        simulator._ensure_simulation_order_page_once(Device())
    except bridge.BridgeError as exc:
        assert "找不到同花顺交易入口" in str(exc)
    else:
        raise AssertionError("查询面板被误判为模拟委托页")


def test_bridge_keeps_same_day_events_on_reconcile_path(tmp_path, monkeypatch):
    bridge = _load_bridge_module()
    bridge.DB_PATH = tmp_path / "bridge.db"
    bridge.init_db()
    today = datetime.now().astimezone().date().isoformat()
    fresh_event = {
        "event_id": "fresh-today", "event_type": "analysis.completed", "status": "completed",
        "symbol": "000001", "stock_name": "平安银行", "trading_date": today,
        "decision": {"action": "buy", "symbol": "000001", "quantity": 100, "price_type": "limit",
                      "limit_price": 10.0, "validation_status": "accepted", "paper_order_id": 12},
    }
    _insert_bridge_event(bridge, fresh_event)
    update = {"status": "filled", "filled_quantity": 100, "average_fill_price": 10.0,
              "rejection_reason": None, "external_order_id": "ths-sim:x"}
    monkeypatch.setattr(bridge.ThsSimulator, "read_recent_trade_history", lambda self: [])
    monkeypatch.setattr(
        bridge.ThsSimulator, "reconcile", lambda self, event, decision: update,
    )
    reports = []
    monkeypatch.setattr(
        bridge, "report_execution",
        lambda event, value: reports.append((event["event_id"], value["status"])) or {"ok": True},
    )

    bridge.reconcile_pending_once()

    assert reports == [("fresh-today", "filled")]
    with sqlite3.connect(bridge.DB_PATH) as db:
        state = db.execute("SELECT state FROM events WHERE event_id='fresh-today'").fetchone()[0]
    assert state == "filled"


def test_bridge_restarts_ths_once_then_retries_page_navigation(monkeypatch):
    bridge = _load_bridge_module()
    simulator = bridge.ThsSimulator(bridge.Config())
    calls = []

    def navigate(_d):
        calls.append("navigate")
        if calls.count("navigate") <= 2:
            raise bridge.BridgeError("找不到同花顺交易入口")

    monkeypatch.setattr(simulator, "_ensure_simulation_order_page_once", navigate)
    monkeypatch.setattr(simulator, "_transaction_sensitive_dialog", lambda _d: None)
    monkeypatch.setattr(simulator, "_restart_ths", lambda _d: calls.append("restart"))

    simulator._ensure_simulation_order_page(object())

    assert calls == ["navigate", "navigate", "restart", "navigate"]


def test_bridge_retries_navigation_after_clearing_popup_before_restart(monkeypatch):
    bridge = _load_bridge_module()
    simulator = bridge.ThsSimulator(bridge.Config())
    calls = []

    def navigate(_d):
        calls.append("navigate")
        if calls.count("navigate") == 1:
            raise bridge.BridgeError("进入模拟委托页失败")

    monkeypatch.setattr(simulator, "_ensure_simulation_order_page_once", navigate)
    monkeypatch.setattr(simulator, "_transaction_sensitive_dialog", lambda _d: None)
    monkeypatch.setattr(simulator, "_restart_ths", lambda _d: calls.append("restart"))
    monkeypatch.setattr(
        simulator, "_dismiss_marketing_popups", lambda _d, **kwargs: calls.append("clear-popup")
    )

    simulator._ensure_simulation_order_page(object())

    # 弹层清理重试成功时绝不应走到重启同花顺那一步。
    assert calls == ["navigate", "clear-popup", "navigate"]


def test_bridge_retries_simulation_home_entry_when_first_click_is_swallowed(monkeypatch):
    bridge = _load_bridge_module()
    simulator = bridge.ThsSimulator(bridge.Config())
    state = {"buy_clicks": 0}

    class Node:
        def __init__(self, selector):
            self.selector = selector

        @property
        def exists(self):
            return self.selector.get("text") == "模拟练习区"

        def wait(self, timeout=0):
            if self.selector.get("resourceId", "").endswith("menu_buy"):
                return True
            if (
                self.selector.get("text") == "买入"
                and self.selector.get("resourceId", "").endswith(":id/btn")
            ):
                return state["buy_clicks"] >= 2
            return False

        def click(self):
            if self.selector.get("resourceId", "").endswith("menu_buy"):
                state["buy_clicks"] += 1

        def get_text(self):
            return ""

    class Device:
        def __call__(self, **selector):
            return Node(selector)

        def dump_hierarchy(self, compressed=False):
            return "<hierarchy><node text=\"\" resource-id=\"\" bounds=\"[0,0][1080,1920]\" /></hierarchy>"

    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)

    simulator._open_simulation_order_entry(Device())

    assert state["buy_clicks"] == 2


def test_bridge_does_not_restart_during_order_confirmation(monkeypatch):
    bridge = _load_bridge_module()
    simulator = bridge.ThsSimulator(bridge.Config())
    restarts = []

    monkeypatch.setattr(
        simulator,
        "_ensure_simulation_order_page_once",
        lambda _d: (_ for _ in ()).throw(bridge.BridgeError("页面异常")),
    )
    monkeypatch.setattr(
        simulator,
        "_transaction_sensitive_dialog",
        lambda _d: "委托买入确认",
    )
    monkeypatch.setattr(simulator, "_restart_ths", lambda _d: restarts.append("restart"))

    with pytest.raises(bridge.ExecutionUncertain, match="禁止自动重启"):
        simulator._ensure_simulation_order_page(object())

    assert restarts == []


def test_bridge_throttles_repeated_app_restarts(monkeypatch):
    bridge = _load_bridge_module()
    simulator = bridge.ThsSimulator(bridge.Config())
    bridge._LAST_THS_RESTART_AT = bridge.time.monotonic()
    calls = []

    class Device:
        def app_stop(self, package):
            calls.append(("stop", package))

        def app_start(self, package, wait=False):
            calls.append(("start", package, wait))

    monkeypatch.setattr(simulator, "_transaction_sensitive_dialog", lambda _d: None)

    with pytest.raises(bridge.BridgeError, match="不重复重启"):
        simulator._restart_ths(Device())

    assert calls == []


def test_snapshot_can_activate_ths_for_read_only_sync(monkeypatch):
    bridge = _load_bridge_module()
    calls = []

    class Node:
        exists = False

        def wait(self, timeout=0):
            return True

        def click(self):
            return None

        def get_text(self):
            return ""

    class Device:
        foreground = False

        def __call__(self, **_kwargs):
            return Node()

        def app_current(self):
            return {"package": bridge.PACKAGE if self.foreground else "other.app"}

        def app_start(self, package, wait=False):
            calls.append((package, wait))
            self.foreground = True

        def dump_hierarchy(self, compressed=False):
            return f"""<hierarchy>
                <node resource-id="{bridge.PACKAGE}:id/capital_cell_title" text="总资产" />
                <node resource-id="{bridge.PACKAGE}:id/capital_cell_title" text="总市值" />
                <node resource-id="{bridge.PACKAGE}:id/capital_cell_title" text="可用" />
                <node resource-id="{bridge.PACKAGE}:id/capital_cell_value" text="200000.00" />
                <node resource-id="{bridge.PACKAGE}:id/capital_cell_value" text="0.00" />
                <node resource-id="{bridge.PACKAGE}:id/capital_cell_value" text="200000.00" />
            </hierarchy>"""

        def swipe(self, *_args, **_kwargs):
            return None

    device = Device()
    simulator = bridge.ThsSimulator(bridge.Config())
    monkeypatch.setattr(simulator, "connect", lambda: device)
    monkeypatch.setattr(simulator, "_ensure_simulation_order_page", lambda _d: None)
    monkeypatch.setattr(simulator, "_account_guard", lambda _d: None)
    monkeypatch.setattr(simulator, "_position_rows", lambda _d: [])
    monkeypatch.setattr(simulator, "_open_query_tab", lambda _d, **_kwargs: None)
    monkeypatch.setattr(simulator, "_result_rows", lambda _d: [])

    simulator.snapshot(activate_app=True)

    assert calls == [(bridge.PACKAGE, True)]


def test_snapshot_retries_holdings_tab_when_first_click_is_ignored(monkeypatch):
    bridge = _load_bridge_module()
    holding_clicks = []

    class Node:
        exists = True
        info = {"selected": False}

        def __init__(self, selector):
            self.selector = selector

        def wait(self, timeout=0):
            return True

        def click(self):
            if self.selector.get("text") == "持仓":
                holding_clicks.append("holding")

        def get_text(self):
            return ""

    class Device:
        def __call__(self, **selector):
            return Node(selector)

    simulator = bridge.ThsSimulator(bridge.Config())
    monkeypatch.setattr(simulator, "connect", lambda: Device())
    monkeypatch.setattr(simulator, "_ensure_simulation_order_page", lambda _d: None)
    monkeypatch.setattr(simulator, "_account_guard", lambda _d: None)
    values = iter([
        bridge.BridgeError("第一次点击未生效"),
        {"总资产": 200000.0, "总市值": 0.0, "可用": 200000.0},
    ])

    def wait_for_values(_d):
        result = next(values)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(simulator, "_wait_for_account_values", wait_for_values)
    monkeypatch.setattr(simulator, "_read_complete_positions", lambda _d, **_kwargs: [])
    monkeypatch.setattr(simulator, "_open_query_tab", lambda _d, **_kwargs: None)
    monkeypatch.setattr(simulator, "_result_rows", lambda _d: [])

    simulator.snapshot()

    assert holding_clicks == ["holding", "holding"]


def test_bridge_waits_for_holdings_totals_instead_of_returning_empty_snapshot(monkeypatch):
    bridge = _load_bridge_module()
    simulator = bridge.ThsSimulator(bridge.Config())
    dumps = iter([
        "<hierarchy />",
        f"""<hierarchy>
            <node resource-id="{bridge.PACKAGE}:id/capital_cell_title" text="总资产" />
            <node resource-id="{bridge.PACKAGE}:id/capital_cell_title" text="总市值" />
            <node resource-id="{bridge.PACKAGE}:id/capital_cell_title" text="可用" />
            <node resource-id="{bridge.PACKAGE}:id/capital_cell_value" text="203144.89" />
            <node resource-id="{bridge.PACKAGE}:id/capital_cell_value" text="17670.00" />
            <node resource-id="{bridge.PACKAGE}:id/capital_cell_value" text="185474.89" />
        </hierarchy>""",
    ])
    device = SimpleNamespace(dump_hierarchy=lambda compressed=False: next(dumps))
    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)

    values = simulator._wait_for_account_values(device, timeout=1.0)

    assert values == {"总资产": 203144.89, "总市值": 17670.0, "可用": 185474.89}


def test_bridge_converts_market_intent_to_captured_counterparty_limit():
    bridge = _load_bridge_module()
    decision = {
        "action": "buy",
        "symbol": "000001",
        "quantity": 100,
        "price_type": "market",
        "limit_price": None,
        "paper_order": {"execution_limit_price": 10.02},
    }

    normalized = bridge.execution_decision({}, decision)

    assert normalized["price_type"] == "limit"
    assert normalized["limit_price"] == 10.02
    assert normalized["original_price_type"] == "market"
    assert decision["price_type"] == "market"


def test_bridge_expands_cancel_intent_from_original_paper_order():
    bridge = _load_bridge_module()
    decision = {
        "action": "cancel",
        "symbol": "000001",
        "quantity": 0,
        "price_type": "none",
        "paper_order": {
            "side": "buy",
            "quantity": 500,
            "execution_limit_price": 9.98,
            "created_at": "2026-08-13T10:00:00+08:00",
        },
    }

    normalized = bridge.execution_decision({}, decision)

    assert normalized["action"] == "cancel"
    assert normalized["original_action"] == "buy"
    assert normalized["quantity"] == 500
    assert normalized["limit_price"] == 9.98
    assert normalized["order_created_at"] == "2026-08-13T10:00:00+08:00"


def test_bridge_reports_pre_submit_bridge_error_as_rejected(tmp_path, monkeypatch):
    bridge = _load_bridge_module()
    bridge.DB_PATH = tmp_path / "bridge.db"
    bridge.init_db()
    callbacks = []
    decision = {
        "action": "buy",
        "symbol": "002648",
        "quantity": 300,
        "price_type": "limit",
        "limit_price": 26.74,
        "validation_status": "accepted",
        "paper_order_id": 3,
    }
    event = {
        "event_id": "event-rejected-before-submit",
        "event_type": "analysis.completed",
        "turn_id": "turn-1",
        "symbol": "002648",
        "stock_name": "卫星化学",
        "decision": decision,
    }

    monkeypatch.setattr(bridge, "validate_event", lambda _event: ("accepted", decision))
    monkeypatch.setattr(bridge, "execution_decision", lambda _event, value: value)

    class Simulator:
        def __init__(self, _config):
            pass

        def execute(self, _decision, *, live):
            assert live is True
            raise bridge.BridgeError("搜索结果中找不到证券代码 002648")

    def report(_event, update):
        callbacks.append(update)
        return {"status": update["status"]}

    monkeypatch.setattr(bridge, "ThsSimulator", Simulator)
    monkeypatch.setattr(bridge, "report_execution", report)

    result = bridge.process_event(event, live=True)
    with sqlite3.connect(bridge.DB_PATH) as db:
        state, error = db.execute(
            "SELECT state,error FROM events WHERE event_id=?", (event["event_id"],),
        ).fetchone()

    assert result["state"] == "rejected"
    assert callbacks == [{
        "status": "rejected",
        "filled_quantity": 0,
        "average_fill_price": None,
        "rejection_reason": "搜索结果中找不到证券代码 002648",
    }]
    assert state == "rejected"
    assert "002648" in error


def test_bridge_repairs_legacy_symbol_lookup_failure_callback(tmp_path, monkeypatch):
    bridge = _load_bridge_module()
    bridge.DB_PATH = tmp_path / "bridge.db"
    bridge.init_db()
    callbacks = []
    event = {
        "event_id": "legacy-execution-failed",
        "event_type": "analysis.completed",
        "turn_id": "turn-legacy",
        "symbol": "002648",
        "stock_name": "卫星化学",
        "decision": {
            "action": "buy",
            "symbol": "002648",
            "paper_order_id": 3,
        },
    }
    with sqlite3.connect(bridge.DB_PATH) as db:
        db.execute(
            "INSERT INTO events(event_id,turn_id,symbol,action,validation_status,state,payload_json,error,result_json) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                event["event_id"],
                event["turn_id"],
                event["symbol"],
                "buy",
                "accepted",
                "execution_failed",
                json.dumps(event, ensure_ascii=False),
                "Remote end closed connection without response",
                json.dumps({
                    "state": "execution_failed",
                    "blocked": "搜索结果中找不到证券代码 002648",
                }, ensure_ascii=False),
            ),
        )

    def report(_event, update):
        callbacks.append(update)
        return {"status": update["status"]}

    monkeypatch.setattr(bridge, "report_execution", report)

    assert bridge.reconcile_pending_once() == 1
    with sqlite3.connect(bridge.DB_PATH) as db:
        state = db.execute(
            "SELECT state FROM events WHERE event_id=?", (event["event_id"],),
        ).fetchone()[0]
    assert state == "rejected"
    assert callbacks[0]["status"] == "rejected"
    assert "002648" in callbacks[0]["rejection_reason"]


def test_bridge_reads_filled_state_from_ths_order_row(monkeypatch):
    bridge = _load_bridge_module()

    class Tab:
        exists = False

        def wait(self, timeout=0):
            return True

        def click(self):
            return None

        def get_text(self):
            return ""

    class Device:
        def __call__(self, **kwargs):
            return Tab()

        def dump_hierarchy(self, compressed=False):
            return "<hierarchy><node text=\"\" resource-id=\"\" bounds=\"[0,0][1080,1920]\" /></hierarchy>"

    simulator = bridge.ThsSimulator(bridge.Config())
    monkeypatch.setattr(simulator, "connect", lambda: Device())
    monkeypatch.setattr(simulator, "_ensure_simulation_order_page", lambda _d: None)
    monkeypatch.setattr(simulator, "_account_guard", lambda _d: None)
    monkeypatch.setattr(
        simulator,
        "_result_rows",
        lambda _d: [{
            "result0": "鼎通科技",
            "result1": "09:58:15",
            "result2": "259.730",
            "result3": "259.510",
            "result6": "买入",
            "result7": "全部成交",
            "first_tv": "500",
            "second_tv": "500",
        }],
    )

    update = simulator.reconcile(
        {"stock_name": "鼎通科技"},
        {"action": "buy", "quantity": 500, "limit_price": 259.73},
    )

    assert update["status"] == "filled"
    assert update["filled_quantity"] == 500
    assert update["average_fill_price"] == 259.51


def test_bridge_opens_visible_query_menu_without_toggling_query_closed(monkeypatch):
    bridge = _load_bridge_module()
    clicks = []

    class Node:
        def __init__(self, name, visible):
            self.name = name
            self.visible = visible

        @property
        def exists(self):
            return False

        def get_text(self):
            return ""

        def wait(self, timeout=0):
            return self.visible

        def click(self):
            clicks.append(self.name)

    class Device:
        def __call__(self, **selector):
            if selector == {"text": "当日委托", "resourceId": f"{bridge.PACKAGE}:id/menu_name"}:
                return Node("当日委托", True)
            if selector == {"text": "查询", "resourceId": f"{bridge.PACKAGE}:id/btn"}:
                return Node("查询", True)
            return Node("other", False)

        def dump_hierarchy(self, compressed=False):
            return "<hierarchy><node text=\"\" resource-id=\"\" bounds=\"[0,0][1080,1920]\" /></hierarchy>"

    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    bridge.ThsSimulator(bridge.Config())._open_query_tab(
        Device(), resource_name="weituo", menu_name="当日委托",
    )

    assert clicks == ["当日委托"]


def test_bridge_opens_query_before_menu_when_target_is_hidden(monkeypatch):
    bridge = _load_bridge_module()
    clicks = []

    class Device:
        menu_visible = False

        class Node:
            def __init__(self, device, name):
                self.device = device
                self.name = name

            @property
            def exists(self):
                return False

            def get_text(self):
                return ""

            def wait(self, timeout=0):
                if self.name == "查询":
                    return True
                if self.name == "当日委托":
                    return self.device.menu_visible
                return False

            def click(self):
                clicks.append(self.name)
                if self.name == "查询":
                    self.device.menu_visible = True

        def __call__(self, **selector):
            if selector == {"text": "查询", "resourceId": f"{bridge.PACKAGE}:id/btn"}:
                return self.Node(self, "查询")
            if selector == {"text": "当日委托", "resourceId": f"{bridge.PACKAGE}:id/menu_name"}:
                return self.Node(self, "当日委托")
            return self.Node(self, "other")

        def dump_hierarchy(self, compressed=False):
            return "<hierarchy><node text=\"\" resource-id=\"\" bounds=\"[0,0][1080,1920]\" /></hierarchy>"

    monkeypatch.setattr(bridge.time, "sleep", lambda _seconds: None)
    bridge.ThsSimulator(bridge.Config())._open_query_tab(
        Device(), resource_name="weituo", menu_name="当日委托",
    )

    assert clicks == ["查询", "当日委托"]


def test_bridge_history_matching_does_not_reclaim_claimed_fills():
    """隔天撮合不得认领已被其他订单回报占用的同一笔历史成交（2026-10-27 幻影成交根因）。"""
    bridge = _load_bridge_module()
    rows = [{
        "trading_day": "20261027", "time": "20261027 10:14:58", "name": "福莱新材",
        "price": 44.79, "quantity": 100, "side": "买入", "amount": 4479.0,
    }]
    update = bridge._match_history_fills(
        rows, stock_name="福莱新材", side_text="买入", quantity=100,
        trading_day="2026-10-27", limit_price=None,
    )
    assert update["status"] == "filled" and update["filled_quantity"] == 100

    # 订单98已占用该成交：订单95的隔天撮合必须按“查无成交”收口，而不是把98的成交据为己有。
    update = bridge._match_history_fills(
        rows, stock_name="福莱新材", side_text="买入", quantity=100,
        trading_day="2026-10-27", limit_price=None, claimed_quantity=100,
    )
    assert update["status"] == "cancelled" and update["filled_quantity"] == 0

    # 部分占用：仅剩余量可归属，成交金额按比例折算。
    update = bridge._match_history_fills(
        rows, stock_name="福莱新材", side_text="买入", quantity=100,
        trading_day="2026-10-27", limit_price=None, claimed_quantity=60,
    )
    assert update["status"] == "partially_filled" and update["filled_quantity"] == 40
    assert update["average_fill_price"] == 44.79


def test_history_match_ignores_fills_before_order_submission():
    """成交时间早于委托提交的行物理上不可能属于本订单，必须剔除（只删不猜）。

    面板"成交时间"列语义经 2026-11-02 真机对照核实（非委托时间）。
    """
    bridge = _load_bridge_module()
    anchor = datetime(2026, 10, 27, 10, 5, 0).astimezone()
    rows = [
        {
            "trading_day": "20261027", "time": "20261027 10:00:00", "name": "福莱新材",
            "price": 44.79, "quantity": 100, "side": "买入", "amount": 4479.0,
        },
        {
            "trading_day": "20261027", "time": "20261027 10:06:00", "name": "福莱新材",
            "price": 44.80, "quantity": 100, "side": "买入", "amount": 4480.0,
        },
    ]

    # 无锚点时保持旧行为：两行合计超出委托量，拒绝归属（返回 None 走人工收口）。
    assert bridge._match_history_fills(
        rows, stock_name="福莱新材", side_text="买入", quantity=100,
        trading_day="2026-10-27", limit_price=None,
    ) is None

    # 有锚点时，10:00 的行早于 10:05 的委托提交，不可能属于本订单；
    # 10:06 的行被正确归属，而不是因合计超量整体拒绝。
    update = bridge._match_history_fills(
        rows, stock_name="福莱新材", side_text="买入", quantity=100,
        trading_day="2026-10-27", limit_price=None, submitted_at=anchor,
    )
    assert update["status"] == "filled" and update["filled_quantity"] == 100
    assert update["average_fill_price"] == 44.8

    # 120s 时钟余量内不算"早于提交"：60s 前的行保留，180s 前的行剔除。
    update = bridge._match_history_fills(
        [{
            "trading_day": "20261027", "time": "20261027 10:04:00", "name": "福莱新材",
            "price": 44.79, "quantity": 100, "side": "买入", "amount": 4479.0,
        }],
        stock_name="福莱新材", side_text="买入", quantity=100,
        trading_day="2026-10-27", limit_price=None, submitted_at=anchor,
    )
    assert update is not None and update["status"] == "filled"
    update = bridge._match_history_fills(
        [{
            "trading_day": "20261027", "time": "20261027 10:02:00", "name": "福莱新材",
            "price": 44.79, "quantity": 100, "side": "买入", "amount": 4479.0,
        }],
        stock_name="福莱新材", side_text="买入", quantity=100,
        trading_day="2026-10-27", limit_price=None, submitted_at=anchor,
    )
    assert update is not None and update["status"] == "cancelled"

    # 秒级即成交的合法形态不得被误删（2026-10-26 订单91：创建13:24:40、成交13:24:48）。
    update = bridge._match_history_fills(
        [{
            "trading_day": "20261026", "time": "20261026 13:24:48", "name": "埃斯顿",
            "price": 30.75, "quantity": 100, "side": "买入", "amount": 3075.0,
        }],
        stock_name="埃斯顿", side_text="买入", quantity=100,
        trading_day="2026-10-26", limit_price=None,
        submitted_at=datetime(2026, 10, 26, 13, 24, 40).astimezone(),
    )
    assert update is not None and update["status"] == "filled"


def test_event_submitted_at_prefers_paper_order_creation():
    """提交锚点取原订单创建时间；撤单事件也用原订单锚点，缺失时返回 None。"""
    bridge = _load_bridge_module()
    decision = {
        "action": "cancel",
        "paper_order": {
            "id": 87, "side": "buy", "created_at": "2026-10-26T10:05:48.097529+08:00",
        },
    }
    anchor = bridge._event_submitted_at({"decision": decision}, decision)
    assert anchor is not None and anchor.hour == 10 and anchor.minute == 5

    assert bridge._event_submitted_at({}, {"action": "buy"}) is None


def test_bridge_stale_settlement_does_not_double_claim_same_fill(tmp_path, monkeypatch):
    """两个同日同向的隔天事件共享同一笔历史成交时，先到者认领、后者必须按撤销收口。"""
    bridge = _load_bridge_module()
    bridge.DB_PATH = tmp_path / "bridge.db"
    bridge.init_db()
    stale_day = datetime.now().astimezone().date() - timedelta(days=2)
    day_iso = stale_day.isoformat()
    day_compact = stale_day.strftime("%Y%m%d")
    events = [
        {
            "event_id": "stale-first", "event_type": "analysis.completed", "status": "completed",
            "symbol": "605488", "stock_name": "福莱新材", "trading_date": day_iso,
            "decision": {"action": "buy", "symbol": "605488", "quantity": 100, "price_type": "limit",
                          "limit_price": 45.0, "validation_status": "accepted", "paper_order_id": 21},
        },
        {
            "event_id": "stale-second", "event_type": "analysis.completed", "status": "completed",
            "symbol": "605488", "stock_name": "福莱新材", "trading_date": day_iso,
            "decision": {"action": "buy", "symbol": "605488", "quantity": 100, "price_type": "limit",
                          "limit_price": 46.0, "validation_status": "accepted", "paper_order_id": 22},
        },
    ]
    for offset, event in enumerate(events):
        _insert_bridge_event(
            bridge, event,
            received_at=f"{day_iso} 0{2 + offset}:13:14",
        )
    history = [{
        "trading_day": day_compact, "time": f"{day_compact} 10:14:58", "name": "福莱新材",
        "price": 44.79, "quantity": 100, "side": "买入", "amount": 4479.0,
    }]
    monkeypatch.setattr(bridge.ThsSimulator, "read_recent_trade_history", lambda self: history)
    reports = []
    monkeypatch.setattr(
        bridge, "report_execution",
        lambda event, update: reports.append((event["event_id"], update["status"])) or {"ok": True},
    )

    bridge.reconcile_pending_once()

    assert reports == [("stale-first", "filled"), ("stale-second", "cancelled")]
    with sqlite3.connect(bridge.DB_PATH) as db:
        states = dict(db.execute("SELECT event_id,state FROM events").fetchall())
    assert states["stale-first"] == "filled"
    assert states["stale-second"] == "cancelled"


def test_bridge_stale_settlement_claims_fills_in_submission_order(tmp_path, monkeypatch):
    """同键多单按提交时刻先后认领：先提交的订单先认领较早的成交行。

    收件顺序故意反转（后提交的事件先入账本），旧行为会把成交归给后提交的
    订单；按提交时刻排序 + “成交不早于委托”过滤后，归属与物理顺序一致。
    """
    bridge = _load_bridge_module()
    bridge.DB_PATH = tmp_path / "bridge.db"
    bridge.init_db()
    stale_day = datetime.now().astimezone().date() - timedelta(days=2)
    day_iso = stale_day.isoformat()
    day_compact = stale_day.strftime("%Y%m%d")
    events = [
        {
            "event_id": "stale-late", "event_type": "analysis.completed", "status": "completed",
            "symbol": "605488", "stock_name": "福莱新材", "trading_date": day_iso,
            "decision": {"action": "buy", "symbol": "605488", "quantity": 100, "price_type": "limit",
                          "limit_price": 46.0, "validation_status": "accepted", "paper_order_id": 32,
                          "paper_order": {"id": 32, "side": "buy", "quantity": 100,
                                          "created_at": f"{day_iso}T10:05:00+08:00"}},
        },
        {
            "event_id": "stale-early", "event_type": "analysis.completed", "status": "completed",
            "symbol": "605488", "stock_name": "福莱新材", "trading_date": day_iso,
            "decision": {"action": "buy", "symbol": "605488", "quantity": 100, "price_type": "limit",
                          "limit_price": 45.0, "validation_status": "accepted", "paper_order_id": 31,
                          "paper_order": {"id": 31, "side": "buy", "quantity": 100,
                                          "created_at": f"{day_iso}T10:00:00+08:00"}},
        },
    ]
    # 后提交的事件先入账本：收件顺序与提交顺序相反。
    _insert_bridge_event(bridge, events[0], received_at=f"{day_iso} 01:13:14")
    _insert_bridge_event(bridge, events[1], received_at=f"{day_iso} 03:13:14")
    history = [{
        "trading_day": day_compact, "time": f"{day_compact} 10:00:30", "name": "福莱新材",
        "price": 44.79, "quantity": 100, "side": "买入", "amount": 4479.0,
    }]
    monkeypatch.setattr(bridge.ThsSimulator, "read_recent_trade_history", lambda self: history)
    reports = []
    monkeypatch.setattr(
        bridge, "report_execution",
        lambda event, update: reports.append((event["event_id"], update["status"])) or {"ok": True},
    )

    bridge.reconcile_pending_once()

    assert reports == [("stale-early", "filled"), ("stale-late", "cancelled")]
    with sqlite3.connect(bridge.DB_PATH) as db:
        states = dict(db.execute("SELECT event_id,state FROM events").fetchall())
    assert states["stale-early"] == "filled"
    assert states["stale-late"] == "cancelled"


def test_bridge_settles_submit_event_locally_when_cancel_sibling_terminal(tmp_path, monkeypatch):
    """同订单已有零成交终态撤单事件时，提交事件本地收口，不再导航当日委托。

    THS 当日委托只显示在途委托，已撤单的行立即消失（2026-11-02 订单115），
    提交事件因此永远匹配不到，旧行为会每 15 秒空转一次 UI 直到隔天。
    """
    bridge = _load_bridge_module()
    bridge.DB_PATH = tmp_path / "bridge.db"
    bridge.init_db()
    day_iso = datetime.now().astimezone().date().isoformat()
    submit_event = {
        "event_id": "submit-stuck", "event_type": "analysis.completed", "status": "completed",
        "symbol": "002204", "stock_name": "大连重工", "trading_date": day_iso,
        "decision": {"action": "buy", "symbol": "002204", "quantity": 1200, "price_type": "limit",
                      "limit_price": 4.86, "validation_status": "accepted", "paper_order_id": 115},
    }
    _insert_bridge_event(
        bridge, submit_event, state="unknown",
        received_at=f"{day_iso} 01:34:53",
    )
    _insert_cancelled_sibling(bridge, 115, filled_quantity=0)

    def _fail_reconcile(self, event, decision):
        raise AssertionError("命中撤单终态映射后不得再导航当日委托")

    monkeypatch.setattr(bridge.ThsSimulator, "reconcile", _fail_reconcile)
    reports = []
    monkeypatch.setattr(
        bridge, "report_execution",
        lambda event, update: reports.append((event["event_id"], update["status"])) or {"ok": True},
    )

    reconciled = bridge.reconcile_pending_once()

    assert reconciled == 1
    assert reports == [("submit-stuck", "cancelled")]
    with sqlite3.connect(bridge.DB_PATH) as db:
        state, result_json = db.execute(
            "SELECT state,result_json FROM events WHERE event_id='submit-stuck'"
        ).fetchone()
    assert state == "cancelled"
    result = json.loads(result_json)
    assert result["ths"]["status"] == "cancelled"
    assert result["cancelled_by_event"] == "cancel-115"


def test_bridge_does_not_short_circuit_when_cancel_sibling_has_fills(tmp_path, monkeypatch):
    """撤单事件带成交量（撤单-成交竞争）时不得本地收口，仍走面板如实上报。"""
    bridge = _load_bridge_module()
    bridge.DB_PATH = tmp_path / "bridge.db"
    bridge.init_db()
    day_iso = datetime.now().astimezone().date().isoformat()
    submit_event = {
        "event_id": "submit-race", "event_type": "analysis.completed", "status": "completed",
        "symbol": "002204", "stock_name": "大连重工", "trading_date": day_iso,
        "decision": {"action": "buy", "symbol": "002204", "quantity": 1200, "price_type": "limit",
                      "limit_price": 4.86, "validation_status": "accepted", "paper_order_id": 116},
    }
    _insert_bridge_event(
        bridge, submit_event, state="unknown",
        received_at=f"{day_iso} 01:34:53",
    )
    _insert_cancelled_sibling(bridge, 116, filled_quantity=500)

    update_seen = {}

    def _fake_reconcile(self, event, decision):
        update_seen["called"] = True
        return {"status": "cancelled", "filled_quantity": 500,
                "average_fill_price": 4.84, "rejection_reason": None}

    monkeypatch.setattr(bridge.ThsSimulator, "reconcile", _fake_reconcile)
    monkeypatch.setattr(
        bridge, "report_execution", lambda event, update: {"ok": True},
    )

    bridge.reconcile_pending_once()

    assert update_seen.get("called") is True
    with sqlite3.connect(bridge.DB_PATH) as db:
        state, = db.execute(
            "SELECT state FROM events WHERE event_id='submit-race'"
        ).fetchone()
    assert state == "cancelled"  # 面板回报的带成交撤单，而非零成交本地收口


def test_load_cancelled_order_ids_skips_nonzero_fills_and_unsettled(tmp_path):
    """映射只收：cancel 终态 + 零成交；带成交或未终态的不进映射。"""
    bridge = _load_bridge_module()
    bridge.DB_PATH = tmp_path / "bridge.db"
    bridge.init_db()
    _insert_cancelled_sibling(bridge, 201, filled_quantity=0)
    _insert_cancelled_sibling(bridge, 202, filled_quantity=300)
    # cancel-203 事件存在但 state=unknown（未终态）
    event = {
        "event_id": "cancel-203", "event_type": "analysis.completed", "status": "completed",
        "symbol": "002204", "stock_name": "大连重工",
        "decision": {"action": "cancel", "symbol": "002204", "paper_order_id": 203},
    }
    _insert_bridge_event(bridge, event, state="unknown", received_at="2026-10-21 03:10:00")

    mapping = bridge._load_cancelled_order_ids()

    assert set(mapping) == {201}
    assert mapping[201]["source_event_id"] == "cancel-201"


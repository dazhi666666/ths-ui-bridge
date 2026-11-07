# ths-ui-bridge（同花顺模拟盘 BlueStacks UI 模拟点击桥）

BlueStacks 中的同花顺 App 由 uiautomator2 驱动，通过 UI 模拟点击执行模拟交易。
台账位于 `%LOCALAPPDATA%\StockAnalyzer\data\watch_trader_bridge\bridge.db`。

此程序只适配同花顺的“模拟练习区”。启用实时模拟同步后，同花顺的“当日委托/当日成交”是订单状态的
权威来源；模型产生的市价意图会使用决策时捕获的卖一价（买入）或买一价（卖出）转换为保护限价单。

## 接口契约

盯盘手后端只通过以下三个 HTTP 接口与本桥交互，WT8002 直连执行器实现同一契约：

1. **决策下发**（后端 → 桥）：`POST http://127.0.0.1:8899/hooks/watch-trader`
   - 体：`analysis.completed` 事件 JSON
   - 头：`X-Watch-Trader-Event-Id`（须与体内 event_id 一致）+ `X-Watch-Trader-Signature: sha256=<hmac_sha256(体, webhook_secret)>`
2. **回报上报**（桥 → 后端）：
   - `POST {backend}/api/internal/watch-trader/executor/execution`（旧路径 `.../android-bridge/execution` 仍可用）
   - `POST {backend}/api/internal/watch-trader/executor/snapshot`（旧路径 `.../android-bridge/snapshot` 仍可用）
   - 头：`X-Watch-Trader-Bridge-Signature: sha256=<hmac_sha256(体, webhook_secret)>`
3. **按需快照**（后端 → 桥）：`POST http://127.0.0.1:8899/api/snapshot`，同上验签头，响应体即快照 JSON。

快照行格式（orders/trades 表行）：`result0` 名称 / `result1` 时间 / `result2` 委托价 / `result3` 成交均价 /
`result6` 买入|卖出 / `result7` 状态文本 / `first_tv` 委托数量 / `second_tv` 成交数量 —— 后端
按此格式对账，勿改字段名。

共享 webhook_secret 的唯一真源：`%LOCALAPPDATA%\StockAnalyzer\config\android_bridge.json`。

## 安装

```powershell
cd ths-ui-bridge
py -3.14 -m venv venv
venv\Scripts\pip install -r requirements.txt
```

## 日常启停

```powershell
python start_bridge.py        # 后台拉起（读取 LOCALAPPDATA secret，端口 8899，live 模式）
```

或手动：`venv\Scripts\python bridge.py serve --port 8899 --live`

状态/看板：http://127.0.0.1:8899/ （`/health`、`/api/status`、`/api/events`）

回切 UI 通道：把 `%LOCALAPPDATA%\StockAnalyzer\config\android_bridge.json` 的 `channel` 改回 `"ui"`
并重启后端，然后 `python start_bridge.py`。

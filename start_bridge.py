"""Launch the watch-trader UI bridge with credentials loaded from local config.

BlueStacks UI 模拟点击链路完整保留在本目录；回切 UI 通道时启动本脚本即可。
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent
VENV_PY = BRIDGE_DIR / "venv" / "Scripts" / "python.exe"
PYTHON = str(VENV_PY) if VENV_PY.is_file() else sys.executable
APP_CFG = Path(os.getenv("LOCALAPPDATA", "")) / "StockAnalyzer" / "config" / "android_bridge.json"
# 兼容旧安装：secret 找不到时回查本地数据库里的内置桥行（只读）。
DB_FALLBACKS = [
    Path(os.getenv("LOCALAPPDATA", "")) / "StockAnalyzer" / "data" / "llm_chat.db",
]

env = os.environ.copy()
env.setdefault("WATCH_TRADER_BASE_URL", "http://127.0.0.1:8000")
env.setdefault("ALLOW_SIMULATED_ORDERS", "true")
env.setdefault("BLUESTACKS_SERIAL", "127.0.0.1:5555")
env.setdefault("BLUESTACKS_ADB_ENDPOINT", "127.0.0.1:5555")
env.setdefault("BLUESTACKS_WATCHDOG", "true")

secret = ""
try:
    cfg = json.loads(APP_CFG.read_text(encoding="utf-8"))
    secret = str(cfg.get("webhook_secret") or "")
except Exception:
    pass
if not secret:
    for db_path in DB_FALLBACKS:
        if not db_path.exists():
            continue
        try:
            row = sqlite3.connect(str(db_path)).execute(
                "SELECT secret FROM watch_trader_webhooks WHERE enabled=1 AND url LIKE '%8899%' LIMIT 1"
            ).fetchone()
            secret = row[0] if row else ""
            if secret:
                break
        except Exception:
            continue
if secret:
    env["WATCH_TRADER_WEBHOOK_SECRET"] = secret
else:
    print("warning: webhook secret not found; set WATCH_TRADER_WEBHOOK_SECRET manually", file=sys.stderr)

log_dir = BRIDGE_DIR / "logs"
log_dir.mkdir(exist_ok=True)
log = open(log_dir / "bridge.txt", "a", encoding="utf-8")
subprocess.Popen(
    [PYTHON, str(BRIDGE_DIR / "bridge.py"), "serve", "--port", "8899", "--live"],
    env=env, cwd=str(BRIDGE_DIR), stdout=log, stderr=subprocess.STDOUT,
    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
)
print("bridge launched (D:\\ths-ui-bridge, port 8899)")

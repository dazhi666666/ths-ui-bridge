import hashlib
import hmac
import json
import os
import time
from urllib.request import Request, urlopen

event_id = f"webhook-selftest-hold-{time.time_ns()}"
event = {
    "schema_version": "1.0",
    "event_id": event_id,
    "event_type": "analysis.completed",
    "turn_id": event_id + "-turn",
    "symbol": "000001",
    "status": "completed",
    "decision": {
        "id": event_id + "-decision",
        "action": "hold",
        "symbol": "000001",
        "trading_mode": "none",
        "price_type": "none",
        "limit_price": None,
        "quantity": 0,
        "order_id": None,
        "validation_status": "accepted_hold",
        "validation_reason": None,
        "paper_order_id": None,
    },
}
body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
secret = os.environ["WATCH_TRADER_WEBHOOK_SECRET"].encode()
signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
request = Request(
    "http://127.0.0.1:8899/hooks/watch-trader",
    data=body,
    headers={
        "Content-Type": "application/json",
        "X-Watch-Trader-Event": "analysis.completed",
        "X-Watch-Trader-Event-Id": event_id,
        "X-Watch-Trader-Signature": signature,
    },
)
with urlopen(request, timeout=10) as response:
    print(response.status, response.read().decode())

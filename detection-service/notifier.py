import os
import requests
import logging
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

def send_slack_alert(endpoint_id, signal_type, severity, score, raw_value):
    if not SLACK_WEBHOOK_URL:
        log.warning("SLACK_WEBHOOK_URL not set, skipping notification")
        return

    if signal_type == "p99_ms":
        detail = f"p99={raw_value:.0f}ms (seuil depasse)"
    else:
        detail = f"error_rate={raw_value:.1f}%"

    emoji = "🔴" if severity == "critical" else "🟠"
    text = (
        f"{emoji} *FIRING* [{severity.upper()}]\n"
        f"*Endpoint:* {endpoint_id}\n"
        f"*Signal:* {signal_type}\n"
        f"*Detail:* {detail}\n"
        f"*Score:* {score:.2f}"
    )
    try:
        resp = requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": text},
            timeout=5,
        )
        if resp.status_code != 200:
            log.error(f"Slack notification failed: {resp.status_code} {resp.text}")
        else:
            log.info(f"Slack notification sent for {endpoint_id} -> {signal_type}")
    except Exception as e:
        log.error(f"Slack notification error: {e}")

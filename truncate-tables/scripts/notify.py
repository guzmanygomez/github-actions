"""Slack notification helper for the truncate-tables action."""
from __future__ import annotations

import requests


def send_slack_notification(webhook_url: str, channel: str, text: str) -> None:
    if not webhook_url:
        return
    payload = {"channel": channel, "text": text}
    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()

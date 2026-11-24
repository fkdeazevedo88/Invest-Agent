"""Phone alerts through ntfy (https://ntfy.sh): install the ntfy app and subscribe to your secret
channel name, kept in ntfy-topic.txt on your computer (never uploaded). Alerts never stop the
agent: if sending fails, it just says so."""
import os
from pathlib import Path

import requests

TOPIC_FILE = Path(__file__).resolve().parent.parent / "ntfy-topic.txt"   # private: never uploaded (.gitignore)


def topic(cfg: dict) -> str:
    """The secret channel name: from the config, the NTFY_TOPIC environment variable, or ntfy-topic.txt."""
    t = (cfg.get("ntfy") or {}).get("topic") or os.environ.get("NTFY_TOPIC", "")
    if not t:
        try:
            t = TOPIC_FILE.read_text().strip()
        except OSError:
            t = ""
    return t


def send(cfg: dict, title: str, message: str, tags: str = "chart_with_upwards_trend") -> bool:
    n = cfg.get("ntfy") or {}
    t = topic(cfg)
    if not t:
        return False
    try:
        r = requests.post(f"{n.get('server', 'https://ntfy.sh').rstrip('/')}/{t}",
                          data=message.encode("utf-8"),
                          headers={"Title": title.encode("utf-8"), "Tags": tags}, timeout=15)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"  (phone alert not sent: {e})")
        return False

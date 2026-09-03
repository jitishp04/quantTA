"""Alert de-duplication state.

Without this, a ticker that sits below its lower band for six weeks fires an
identical alert every single trading day and the channel becomes noise you
learn to ignore -- which defeats the purpose of a low-frequency extremes
scanner.

Rules:
  * signal flips to NEUTRAL   -> clear the record (the setup re-arms)
  * new or changed signal     -> alert
  * same signal, still active -> stay silent until `cooldown_days` have passed,
                                 then re-notify once and reset the clock

State is a small JSON file committed back to the repo by the workflow, so the
scanner survives a missed cron run without spamming on the next one.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .config import ALERT_STATE_FILE
from .signals import NEUTRAL, Snapshot


class AlertState:
    def __init__(self, path: Path = ALERT_STATE_FILE):
        self.path = path
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}  # corrupt state is recoverable; start clean

    def should_alert(self, snap: Snapshot, cooldown_days: int) -> bool:
        """Decide whether this snapshot is worth a push notification."""
        if snap.signal == NEUTRAL:
            self.data.pop(snap.key, None)  # re-arm
            return False

        previous = self.data.get(snap.key)
        if previous is None or previous.get("signal") != snap.signal:
            return True

        if cooldown_days <= 0:
            return False

        try:
            last = dt.date.fromisoformat(previous["date"])
        except (KeyError, ValueError):
            return True
        return (dt.date.today() - last).days >= cooldown_days

    def record(self, snap: Snapshot) -> None:
        self.data[snap.key] = {
            "signal": snap.signal,
            "date": dt.date.today().isoformat(),
            "bar_date": snap.bar_date,
            "close": round(snap.close, 4),
            "rsi": round(snap.rsi, 2),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(dict(sorted(self.data.items())), indent=2)
        self.path.write_text(payload + "\n", encoding="utf-8")

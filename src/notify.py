"""Discord transport and alert formatting.

Two independent capabilities, deliberately decoupled so the easy half works
without the hard half:

  Sending    a webhook URL. One secret, no bot, no server. This is all the
             scanner needs to alert you.
  Receiving  a bot token plus a channel id, polled over the REST API. Only
             required for /add and /remove; the scanner is fully functional
             without it.

Alerts are rendered as embeds rather than plain messages: a coloured left rail
makes capitulation and euphoria distinguishable at a glance on a phone, and the
metric table sits in a fenced code block so it stays monospaced and aligned.

Discord's limits, all enforced below: 2000 characters of `content`, 4096 per
embed description, 6000 across all embeds in one message, and 10 embeds per
message.
"""

from __future__ import annotations

import logging
import os
import re
import time

import requests

from .signals import CAPITULATION, EUPHORIA, Snapshot

log = logging.getLogger(__name__)

API_ROOT = "https://discord.com/api/v10"
TIMEOUT = 30

MAX_CONTENT = 2000
MAX_EMBED_DESCRIPTION = 4096
MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_CHARS = 6000

COLOURS = {
    CAPITULATION: 0xE74C3C,   # red
    EUPHORIA: 0xF39C12,       # amber
}
ICONS = {CAPITULATION: "\U0001fa78", EUPHORIA: "\U0001f525"}   # blood drop / fire
HEADLINES = {
    CAPITULATION: "CAPITULATION — accumulate",
    EUPHORIA: "EUPHORIA — trim",
}
HEARTBEAT_COLOUR = 0x2ECC71   # green
PROBLEM_COLOUR = 0x95A5A6     # grey

# Characters Discord treats as markdown outside a code fence.
_MD_SPECIAL = re.compile(r"([*_~`|\\>])")


def escape(text: str) -> str:
    """Escape Discord markdown. Not needed inside fenced code blocks."""
    return _MD_SPECIAL.sub(r"\\\1", text)


def fmt_price(value: float) -> str:
    """Scale decimal places to magnitude so BTC and penny stocks both read well."""
    magnitude = abs(value)
    if magnitude >= 1000:
        return f"{value:,.0f}"
    if magnitude >= 10:
        return f"{value:,.2f}"
    if magnitude >= 1:
        return f"{value:,.3f}"
    return f"{value:,.4f}"


class DiscordClient:
    """Webhook sender with optional bot-token reading."""

    def __init__(
        self,
        webhook_url: str | None = None,
        bot_token: str | None = None,
        channel_id: str | None = None,
        user_id: str | None = None,
    ):
        self.webhook_url = webhook_url or os.environ.get("DISCORD_WEBHOOK_URL", "")
        self.bot_token = bot_token or os.environ.get("DISCORD_BOT_TOKEN", "")
        self.channel_id = channel_id or os.environ.get("DISCORD_CHANNEL_ID", "")
        # Optional: restrict commands to one author. Unset means anyone who can
        # post in the channel can manage the watchlist, which is usually what
        # you want for a private server.
        self.user_id = user_id or os.environ.get("DISCORD_USER_ID", "")
        self.session = requests.Session()

    # -- capability probes ------------------------------------------------

    @property
    def can_send(self) -> bool:
        return bool(self.webhook_url)

    @property
    def can_read(self) -> bool:
        return bool(self.bot_token and self.channel_id)

    def require_send(self) -> None:
        if not self.can_send:
            raise RuntimeError(
                "DISCORD_WEBHOOK_URL must be set (GitHub Actions secret, or .env)"
            )

    def require_read(self) -> None:
        if not self.can_read:
            raise RuntimeError(
                "DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID must be set to read "
                "commands. Alerts work without them -- see the README."
            )

    # -- transport --------------------------------------------------------

    def _request(self, method: str, url: str, retries: int = 3, **kwargs) -> dict | list:
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                response = self.session.request(method, url, timeout=TIMEOUT, **kwargs)
                if response.status_code == 429:
                    # Discord returns retry_after in SECONDS as a float.
                    wait = float(response.json().get("retry_after", 1.0))
                    log.warning("rate limited, sleeping %.2fs", wait)
                    time.sleep(min(wait, 30.0))
                    continue
                response.raise_for_status()
                if response.status_code == 204 or not response.content:
                    return {}
                return response.json()
            except Exception as exc:
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"discord {method} {url.split('?')[0]} failed: {last_error}")

    def send(self, payload: dict) -> None:
        """Post one alert payload, splitting if it exceeds Discord's limits."""
        self.require_send()
        for chunk in _split_payload(payload):
            self._request("POST", self.webhook_url, json=chunk)

    def send_text(self, text: str) -> None:
        """Post a plain message, wrapping over 2000 characters."""
        self.require_send()
        for i in range(0, len(text), MAX_CONTENT):
            self._request("POST", self.webhook_url,
                          json={"content": text[i : i + MAX_CONTENT]})

    def read_messages(self, after: str | None = None, limit: int = 100) -> list[dict]:
        """Fetch channel messages, oldest first.

        Requires the MESSAGE CONTENT INTENT to be enabled for the application,
        otherwise `content` comes back empty for every message and commands
        appear to be silently ignored.
        """
        self.require_read()
        params: dict = {"limit": min(limit, 100)}
        if after:
            params["after"] = after
        result = self._request(
            "GET",
            f"{API_ROOT}/channels/{self.channel_id}/messages",
            headers={"Authorization": f"Bot {self.bot_token}"},
            params=params,
        )
        messages = result if isinstance(result, list) else []
        return list(reversed(messages))     # API returns newest-first


def _embed_size(embed: dict) -> int:
    return len(embed.get("title", "")) + len(embed.get("description", "")) + len(
        (embed.get("footer") or {}).get("text", "")
    )


def _split_payload(payload: dict) -> list[dict]:
    """Break a payload into messages within the 10-embed / 6000-char caps."""
    embeds = payload.get("embeds") or []
    if not embeds:
        return [payload]

    batches: list[list[dict]] = []
    current: list[dict] = []
    running = 0
    for embed in embeds:
        size = _embed_size(embed)
        if current and (len(current) >= MAX_EMBEDS_PER_MESSAGE
                        or running + size > MAX_EMBED_CHARS):
            batches.append(current)
            current, running = [], 0
        current.append(embed)
        running += size
    if current:
        batches.append(current)

    messages = []
    for index, batch in enumerate(batches):
        message: dict = {"embeds": batch}
        if index == 0 and payload.get("content"):
            message["content"] = payload["content"][:MAX_CONTENT]
        messages.append(message)
    return messages


def _embed(snap: Snapshot) -> dict:
    """One alert as a coloured embed with an aligned metric table."""
    rows = [
        ("Close", fmt_price(snap.close)),
        ("BBL", fmt_price(snap.bbl)),
        ("BBM", fmt_price(snap.bbm)),
        ("BBU", fmt_price(snap.bbu)),
        ("RSI 21", f"{snap.rsi:.1f}"),
        ("%B", f"{snap.percent_b:.3f}"),
        ("vs band", f"{snap.band_distance_pct:+.2f}%"),
        ("streak", f"{snap.streak} candle{'s' if snap.streak != 1 else ''}"),
    ]
    table = "\n".join(f"{name:<8}{value:>14}" for name, value in rows)
    context = " · ".join(snap.context) or "trend unavailable"

    description = f"**{HEADLINES[snap.signal]}**\n```\n{table}\n```"
    return {
        "title": f"{ICONS[snap.signal]} {snap.ticker} · {snap.label}",
        "description": description[:MAX_EMBED_DESCRIPTION],
        "color": COLOURS[snap.signal],
        "footer": {"text": f"{context} · bar {snap.bar_date}"},
    }


def format_alerts(snaps: list[Snapshot], problems: dict[str, str] | None = None) -> dict:
    """Assemble the alert payload, capitulation first."""
    order = {CAPITULATION: 0, EUPHORIA: 1}
    ranked = sorted(
        snaps,
        key=lambda s: (order[s.signal], s.timeframe != "weekly", s.ticker),
    )

    caps = sum(1 for s in ranked if s.signal == CAPITULATION)
    euph = len(ranked) - caps

    payload = {
        "content": f"📡 **BARBELL SCAN** — {caps} capitulation · {euph} euphoria",
        "embeds": [_embed(s) for s in ranked],
    }
    if problems:
        payload["embeds"].append({
            "title": "⚠ Skipped",
            "description": escape(", ".join(sorted(problems)))[:MAX_EMBED_DESCRIPTION],
            "color": PROBLEM_COLOUR,
        })
    return payload


def format_heartbeat(
    ticker_count: int,
    timeframes: list[str],
    problems: dict[str, str] | None = None,
) -> dict:
    scope = ", ".join(timeframes)
    description = (
        f"{ticker_count} tickers · {escape(scope)}\n"
        "No candles outside BB(200, 2.5) with RSI21 confirmation."
    )
    if problems:
        description += f"\n\n⚠ Skipped: {escape(', '.join(sorted(problems)))}"
    return {
        "embeds": [{
            "title": "✅ Scan clean",
            "description": description[:MAX_EMBED_DESCRIPTION],
            "color": HEARTBEAT_COLOUR,
        }]
    }


def describe(payload: dict) -> str:
    """Flatten a payload to plain text for --dry-run console output."""
    lines: list[str] = []
    if payload.get("content"):
        lines.append(payload["content"])
    for embed in payload.get("embeds", []):
        lines.append("")
        lines.append(f"[{embed.get('title', '')}]")
        if embed.get("description"):
            lines.append(embed["description"])
        footer = (embed.get("footer") or {}).get("text")
        if footer:
            lines.append(f"  {footer}")
    return "\n".join(lines)

"""Two-way watchlist management over Discord.

The scanner has no server, so there is nothing to receive a gateway connection
or an interactions webhook. Instead the sync workflow polls the channel over
the REST API on a short cron, applies any !add or !remove it finds, and commits
the rewritten watchlist back to the repository.

Two Discord-specific consequences worth knowing:

* Reading message text requires the **MESSAGE CONTENT INTENT**, a privileged
  intent toggled in the Developer Portal. Without it the API still returns
  messages, but every `content` field is an empty string -- so commands appear
  to be silently ignored with no error anywhere. `sync()` detects that case
  explicitly rather than letting it look like nothing was sent.

* Commands are prefixed with `!`, not `/`. A leading slash is reserved by
  Discord for application (slash) commands, which need a public HTTPS endpoint
  to receive interactions and therefore cannot work from a cron job.

Unlike Telegram's getUpdates queue, Discord retains channel history
indefinitely, so nothing is lost if the sync job is down for a day. The trade
is that a first run must NOT replay the whole channel, so when no cursor is
stored the newest message id is recorded without acting on anything.

Supported commands (from the configured channel):
    !add NVDA GOOGL RELIANCE.NS
    !remove TSLA            (aliases: !rm, !del)
    !list                   (alias: !ls)
    !status
    !help
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import COMMAND_STATE_FILE
from .data import is_valid_symbol
from .notify import DiscordClient, escape
from .watchlist import load_tickers, normalise, save_tickers

log = logging.getLogger(__name__)

PREFIX = "!"

HELP = (
    "**Barbell Scanner**\n"
    "```\n"
    "!add NVDA GOOGL   add symbols\n"
    "!remove TSLA      drop symbols\n"
    "!list             show watchlist\n"
    "!status           scanner config\n"
    "!help             this message\n"
    "```\n"
    "Yahoo Finance symbols: `AAPL` `^GSPC` `RELIANCE.NS` `BTC-USD` `BRK-B`"
)


def _load_cursor(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("last_message_id")
    except (json.JSONDecodeError, OSError):
        return None


def _save_cursor(path: Path, message_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"last_message_id": str(message_id)}, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse(text: str) -> tuple[str, list[str]]:
    """Split a message into (command, arguments)."""
    parts = text.strip().split()
    if not parts or not parts[0].startswith(PREFIX):
        return "", []
    return parts[0].lower(), parts[1:]


def _add(args: list[str], tickers: list[str]) -> tuple[list[str], str]:
    if not args:
        return tickers, "Usage: `!add NVDA GOOGL`"

    current = set(tickers)
    added, duplicate, rejected = [], [], []

    for raw in args:
        symbol = normalise(raw)
        if not symbol:
            rejected.append(f"{raw} (malformed)")
        elif symbol in current:
            duplicate.append(symbol)
        elif not is_valid_symbol(symbol):
            rejected.append(f"{symbol} (no Yahoo data)")
        else:
            current.add(symbol)
            added.append(symbol)

    lines = []
    if added:
        lines.append(f"✅ Added: **{escape(' '.join(sorted(added)))}**")
    if duplicate:
        lines.append(f"↩️ Already tracked: {escape(' '.join(sorted(duplicate)))}")
    if rejected:
        lines.append(f"❌ Rejected: {escape(', '.join(rejected))}")
    lines.append(f"_Watchlist: {len(current)} tickers_")
    return sorted(current), "\n".join(lines)


def _remove(args: list[str], tickers: list[str]) -> tuple[list[str], str]:
    if not args:
        return tickers, "Usage: `!remove TSLA`"

    current = set(tickers)
    removed, missing = [], []
    for raw in args:
        symbol = normalise(raw) or raw.strip().upper()
        if symbol in current:
            current.remove(symbol)
            removed.append(symbol)
        else:
            missing.append(symbol)

    lines = []
    if removed:
        lines.append(f"🗑 Removed: **{escape(' '.join(sorted(removed)))}**")
    if missing:
        lines.append(f"❓ Not tracked: {escape(' '.join(sorted(missing)))}")
    lines.append(f"_Watchlist: {len(current)} tickers_")
    return sorted(current), "\n".join(lines)


def _list(tickers: list[str]) -> str:
    if not tickers:
        return "Watchlist is empty. Add one with `!add SPY`"
    body = "\n".join(
        "  ".join(f"{t:<12}" for t in tickers[i : i + 3]).rstrip()
        for i in range(0, len(tickers), 3)
    )
    return f"**Watchlist ({len(tickers)})**\n```\n{body}\n```"


def _status(tickers: list[str], settings: dict) -> str:
    rows = [
        ("tickers", str(len(tickers))),
        ("timeframes", ",".join(settings.get("timeframes", []))),
        ("cooldown", f"{settings.get('cooldown_days')}d"),
        ("bands", "BB(200, 2.5)"),
        ("momentum", "RSI(21) @ 30/70"),
        ("trend", "SMA50 / EMA200"),
        ("window", "756d · 260w"),
    ]
    table = "\n".join(f"{k:<12}{v}" for k, v in rows)
    return f"**Scanner status**\n```\n{table}\n```"


def sync(client: DiscordClient, settings: dict, dry_run: bool = False) -> bool:
    """Drain pending Discord commands and apply them to the watchlist.

    Returns True if the watchlist file was modified.
    """
    client.require_read()
    cursor = _load_cursor(COMMAND_STATE_FILE)

    # First run: adopt the current head without replaying channel history.
    if cursor is None:
        latest = client.read_messages(limit=1)
        if latest and not dry_run:
            _save_cursor(COMMAND_STATE_FILE, latest[-1]["id"])
            log.info("initialised cursor at message %s; history not replayed",
                     latest[-1]["id"])
        return False

    messages = client.read_messages(after=cursor)
    if not messages:
        return False

    tickers = load_tickers()
    original = list(tickers)
    newest = cursor
    blank_content = 0
    acted = False

    for message in messages:
        newest = message["id"]
        author = message.get("author") or {}

        # Never react to our own webhook posts, or any other bot.
        if author.get("bot") or message.get("webhook_id"):
            continue
        if client.user_id and str(author.get("id")) != str(client.user_id):
            log.warning("ignoring command from unauthorised user %s", author.get("id"))
            continue

        text = message.get("content") or ""
        if not text.strip():
            blank_content += 1
            continue

        command, args = _parse(text)
        if not command:
            continue

        acted = True
        if command == f"{PREFIX}add":
            tickers, reply = _add(args, tickers)
        elif command in (f"{PREFIX}remove", f"{PREFIX}rm", f"{PREFIX}del"):
            tickers, reply = _remove(args, tickers)
        elif command in (f"{PREFIX}list", f"{PREFIX}ls"):
            reply = _list(tickers)
        elif command == f"{PREFIX}status":
            reply = _status(tickers, settings)
        elif command in (f"{PREFIX}help", f"{PREFIX}start"):
            reply = HELP
        else:
            reply = f"Unknown command `{escape(command)}`. Try `{PREFIX}help`"

        client.send_text(reply)

    # Every message empty and nothing actioned is the classic signature of the
    # privileged intent not being enabled -- worth naming, because the API
    # reports success and the user just sees their commands ignored.
    if blank_content and not acted:
        log.warning(
            "read %d message(s) with empty content and no commands parsed. "
            "Enable MESSAGE CONTENT INTENT for the application at "
            "discord.com/developers -> Bot -> Privileged Gateway Intents.",
            blank_content,
        )

    changed = tickers != original
    if not dry_run:
        # Advance the cursor even when nothing changed, so the same messages
        # are not re-read on the next run.
        _save_cursor(COMMAND_STATE_FILE, newest)
        if changed:
            save_tickers(tickers)

    return changed

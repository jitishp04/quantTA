#!/usr/bin/env python3
"""Watchlist sync entrypoint.

Polls Discord for pending !add, !remove, !list, !status and !help commands and
applies them to config/tickers.txt. The workflow commits any resulting change
back to the repository, so the next scan picks it up automatically.

Optional layer: a Discord webhook can only send, so this needs a bot token to
read the channel. Without one the job exits cleanly and you manage tickers by
editing config/tickers.txt directly.

Exit codes are meaningful to CI:
    0  no watchlist change  (nothing to commit)
    2  watchlist changed    (commit and push)
    1  error
"""

from __future__ import annotations

import argparse
import logging
import sys

from src.commands import sync
from src.config import load_dotenv
from src.notify import DiscordClient
from src.watchlist import load_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sync")

load_dotenv()   # local convenience; real env vars always take precedence

EXIT_UNCHANGED = 0
EXIT_ERROR = 1
EXIT_CHANGED = 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync watchlist from Discord")
    parser.add_argument("--dry-run", action="store_true",
                        help="process commands without writing state or watchlist")
    args = parser.parse_args()

    client = DiscordClient()

    # The bot half is optional. A webhook-only setup sends alerts perfectly
    # well and just manages tickers by editing config/tickers.txt, so a missing
    # bot token is a clean skip, not a failure -- otherwise every scheduled run
    # would show a red X for a configuration the user chose deliberately.
    if not client.can_read:
        log.info(
            "no DISCORD_BOT_TOKEN/DISCORD_CHANNEL_ID configured -- "
            "command sync disabled, edit config/tickers.txt directly"
        )
        return EXIT_UNCHANGED

    try:
        changed = sync(client, load_settings(), dry_run=args.dry_run)
    except Exception as exc:
        log.error("sync failed: %s", exc)
        return EXIT_ERROR

    if changed:
        log.info("watchlist updated")
        return EXIT_CHANGED

    log.info("no watchlist change")
    return EXIT_UNCHANGED


if __name__ == "__main__":
    sys.exit(main())

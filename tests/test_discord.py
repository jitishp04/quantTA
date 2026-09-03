"""Self-checks for Discord command dispatch (!scan, !study) and chunking.

    python tests/test_discord.py

Covers three things that would otherwise only be caught by clicking through
Discord manually: the code-block chunker never produces an unbalanced fence
(a truncated report with a dangling ``` breaks formatting for every message
after it), the workflow dispatcher builds the exact request GitHub's API
requires, and !scan / !study degrade to a clear reply instead of a traceback
when dispatch is unavailable (e.g. this job's token lacks the permission, or a
local run has no GITHUB_TOKEN at all).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import commands  # noqa: E402
from src.dispatch import GitHubDispatcher  # noqa: E402
from src.notify import MAX_CONTENT, chunk_code_block  # noqa: E402


# ---------------------------------------------------------------------------
# chunk_code_block
# ---------------------------------------------------------------------------

def test_short_text_becomes_one_fenced_chunk() -> None:
    chunks = chunk_code_block("line one\nline two")
    assert len(chunks) == 1
    assert chunks[0] == "```\nline one\nline two\n```"
    print("  ok  short text produces a single balanced fence")


def test_long_report_splits_on_line_boundaries() -> None:
    """A report bigger than one message must split without cutting a line."""
    lines = [f"row {i:>4}  close={i * 1.2345:.4f}  rsi={i % 100:.1f}" for i in range(400)]
    text = "\n".join(lines)
    chunks = chunk_code_block(text)

    assert len(chunks) > 1, "400 lines should exceed one 2000-char message"
    for chunk in chunks:
        assert len(chunk) <= MAX_CONTENT, f"chunk of {len(chunk)} exceeds Discord's cap"
        assert chunk.startswith("```\n") and chunk.endswith("\n```"), (
            "every chunk must be self-fenced"
        )
        assert chunk.count("```") == 2, "unbalanced fence inside a chunk"

    # No line was dropped or mangled by the split.
    recovered = "\n".join(
        c.removeprefix("```\n").removesuffix("\n```") for c in chunks
    ).split("\n")
    assert recovered == lines, "chunking altered or lost a line"
    print(f"  ok  {len(lines)} lines split into {len(chunks)} balanced chunks, "
          f"content preserved")


def test_oversized_single_line_degrades_without_raising() -> None:
    """A line longer than the whole budget must hard-split, not crash."""
    huge = "x" * 5000
    chunks = chunk_code_block(f"normal line\n{huge}\nanother normal line")
    assert all(len(c) <= MAX_CONTENT for c in chunks)
    assert all(c.count("```") == 2 for c in chunks)
    print(f"  ok  a {len(huge)}-char line hard-splits into "
          f"{len(chunks)} chunks without raising")


def test_empty_text_produces_no_chunks() -> None:
    assert chunk_code_block("") == []
    print("  ok  empty input produces no messages")


# ---------------------------------------------------------------------------
# GitHubDispatcher
# ---------------------------------------------------------------------------

def test_dispatcher_unavailable_without_token() -> None:
    d = GitHubDispatcher(token="", repo="")
    assert d.available is False
    try:
        d.dispatch("scan.yml")
        raise AssertionError("dispatch should have raised with no token")
    except RuntimeError as exc:
        assert "GITHUB_TOKEN" in str(exc)
    print("  ok  dispatcher reports unavailable and refuses to call the API")


def test_dispatcher_sends_correct_request_shape() -> None:
    """Verify the exact call GitHub's workflow_dispatch API requires.

    Wrong here means a silent no-op in production: the API returns 404/422 for
    a malformed request, which is exactly the failure mode this asserts
    against directly rather than trusting an end-to-end Discord click-through.
    """
    d = GitHubDispatcher(token="tok123", repo="jitishp04/quantTA", ref="main")
    assert d.available is True

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        response = MagicMock()
        response.status_code = 204
        response.raise_for_status.return_value = None
        return response

    with patch.object(d.session, "post", side_effect=fake_post):
        d.dispatch("study.yml", {"tickers": "NVDA", "timeframe": "daily"})

    assert captured["url"] == (
        "https://api.github.com/repos/jitishp04/quantTA/actions/"
        "workflows/study.yml/dispatches"
    )
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["json"]["ref"] == "main"
    # Inputs must be strings -- the REST API rejects non-string input values.
    assert captured["json"]["inputs"] == {"tickers": "NVDA", "timeframe": "daily"}
    assert all(isinstance(v, str) for v in captured["json"]["inputs"].values())
    print("  ok  dispatch request matches GitHub's workflow_dispatch API shape")


def test_dispatcher_404_gets_a_specific_hint() -> None:
    """A 404 here almost always means the permission was never granted --
    the error should say so rather than surface a bare HTTP status."""
    d = GitHubDispatcher(token="tok", repo="a/b")
    response = MagicMock()
    response.status_code = 404

    with patch.object(d.session, "post", return_value=response):
        try:
            d.dispatch("scan.yml")
            raise AssertionError("expected a RuntimeError on 404")
        except RuntimeError as exc:
            assert "actions: write" in str(exc), f"unhelpful message: {exc}"
    print("  ok  a 404 names the likely permissions fix")


# ---------------------------------------------------------------------------
# !scan / !study command handlers
# ---------------------------------------------------------------------------

def test_scan_command_without_dispatcher_access() -> None:
    d = GitHubDispatcher(token="", repo="")
    reply = commands._scan(d)
    assert "GITHUB_TOKEN" in reply or "actions: write" in reply
    print("  ok  !scan without dispatch access replies with the fix, not a crash")


def test_study_command_parses_optional_args() -> None:
    calls = []

    class FakeDispatcher:
        available = True
        def dispatch(self, workflow, inputs):
            calls.append((workflow, inputs))

    reply = commands._study(["nvda", "weekly", "capitulation"], FakeDispatcher())
    assert calls == [("study.yml", {
        "tickers": "NVDA", "timeframe": "weekly", "signal": "capitulation",
    })]
    assert "NVDA" in reply
    print("  ok  !study NVDA weekly capitulation dispatches with parsed inputs")


def test_study_command_defaults_when_args_omitted() -> None:
    calls = []

    class FakeDispatcher:
        available = True
        def dispatch(self, workflow, inputs):
            calls.append((workflow, inputs))

    commands._study(["spy"], FakeDispatcher())
    assert calls == [("study.yml", {
        "tickers": "SPY", "timeframe": "daily", "signal": "both",
    })]
    print("  ok  !study SPY defaults to daily/both")


def test_study_command_rejects_bad_ticker_and_bad_option() -> None:
    class FakeDispatcher:
        available = True
        def dispatch(self, *a, **k):
            raise AssertionError("must not dispatch on invalid input")

    assert "valid symbol" in commands._study(["not a ticker"], FakeDispatcher())
    assert "recognise" in commands._study(["NVDA", "monthly"], FakeDispatcher())
    print("  ok  !study rejects a bad symbol and an unrecognised option "
          "without dispatching")


def test_study_command_no_args_shows_usage() -> None:
    reply = commands._study([], GitHubDispatcher(token="", repo=""))
    assert "Usage" in reply
    print("  ok  !study with no ticker shows usage")


def main() -> int:
    print("\ndiscord command self-check\n" + "-" * 52)
    tests = [
        test_short_text_becomes_one_fenced_chunk,
        test_long_report_splits_on_line_boundaries,
        test_oversized_single_line_degrades_without_raising,
        test_empty_text_produces_no_chunks,
        test_dispatcher_unavailable_without_token,
        test_dispatcher_sends_correct_request_shape,
        test_dispatcher_404_gets_a_specific_hint,
        test_scan_command_without_dispatcher_access,
        test_study_command_parses_optional_args,
        test_study_command_defaults_when_args_omitted,
        test_study_command_rejects_bad_ticker_and_bad_option,
        test_study_command_no_args_shows_usage,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {test.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")

    print("-" * 52)
    print(f"{len(tests) - failures}/{len(tests)} passed\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

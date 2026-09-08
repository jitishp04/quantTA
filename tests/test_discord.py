"""Self-checks for Discord message formatting.

    python tests/test_discord.py

Covers the code-block chunker, which is the one piece of the Discord layer
that can fail silently. The study report is wider and longer than a single
Discord message, so it has to be split -- and a split that lands mid-fence
leaves a dangling ``` that breaks monospace formatting for every message after
it. That is invisible to any HTTP status code: the API returns 200 for a
perfectly malformed report. Hence asserting fence balance directly rather than
trusting an end-to-end click-through.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.notify import MAX_CONTENT, chunk_code_block  # noqa: E402


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


def main() -> int:
    print("\ndiscord formatting self-check\n" + "-" * 52)
    tests = [
        test_short_text_becomes_one_fenced_chunk,
        test_long_report_splits_on_line_boundaries,
        test_oversized_single_line_degrades_without_raising,
        test_empty_text_produces_no_chunks,
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

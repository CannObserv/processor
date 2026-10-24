---
title: Records reach journald at their level's priority (#76)
date: 2026-10-24
status: per #76's brief, its pre-flight and the review comment (2026-10-24)
---

# Records reach journald at their level's priority

## Problem

`configure_logging` writes bare JSON lines to stderr. journald gives each the stream's default, `PRIORITY=6`, so WARNING and ERROR records are invisible to `journalctl -p warning` (measured: 7 WARNINGs of 2026-10-22 all `PRIORITY=6`). #77's patch-window post-check needs `-p warning`.

## Approach (brief option 1)

- `logging.py`: `_is_journal(stream)`: `JOURNAL_STREAM` (`<dev>:<ino>`) equals `os.fstat(stream.fileno())`; any failure (unset, malformed, no fileno) is `False`.
- `JournalPrefixFormatter(JsonFormatter)`, or a `prefix` flag on the formatter: prepends `<N>` (≥CRITICAL 2, ≥ERROR 3, ≥WARNING 4, ≥INFO 6, else 7). journald strips it (`SyslogLevelPrefix=yes`), so `-o cat` stays pure JSON.
- `configure_logging` picks it once, from the handler's stream. A terminal, a pipe (`systemd-run --pipe`), a pytest capture never see a prefix.
- No unit change. The child needs nothing (stderr is a pipe to the parent).
- Docs: `DEPLOYMENT.md` § Logs (priority), § Tailnet flaps (`-p warning`, `jq` kept for fields), line 24 (watcher#325 confirmed); spec Amended entry + Section 3's logging sentence; `logging.py` docstring.

## Tests (red first)

- level → prefix mapping, per level incl. a custom level between.
- `JOURNAL_STREAM` = the stream's real dev:ino → each line `<N>{json}`; the JSON after the prefix parses with the floor.
- `JOURNAL_STREAM` unset, malformed, or another dev:ino → no prefix.
- a stream without `fileno` (capsys) and `JOURNAL_STREAM` set → no prefix, no error.
- existing `test_warnings_are_json_records` holds (with `JOURNAL_STREAM` set to something else, since agent shells inherit one).

## Ship

PR `Refs #76` → FF merge → pull → `scripts/deploy.sh` → proof: transient unit running the live venv's Python, `configure_logging()` + one `logger.warning`, then `journalctl -u <unit> -p warning -o json` shows `PRIORITY=4`. No forced warning in production.

## Open questions

None.

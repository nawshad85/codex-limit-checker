from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.models import DataStatus, LimitKind
from app.monitor import SessionMonitor
from app.utils import resolve_codex_home, resolve_sessions_dir


class _Clock:
    def __init__(self, value: float = 1_900_001_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float = 1.0) -> None:
        self.value += seconds


def _limit(used: float, minutes: int, reset: float = 1_900_010_000) -> dict[str, object]:
    return {
        "used_percent": used,
        "window_minutes": minutes,
        "resets_at": reset,
    }


def _token_event(
    timestamp: float,
    *,
    used: float = 25,
    weekly_used: float | None = 40,
    context_used: int = 50_000,
    context_window: int = 258_400,
    null_rates: bool = False,
    cwd: str | None = None,
) -> dict[str, object]:
    if null_rates:
        rates = None
    else:
        rates = {"primary": _limit(used, 300), "plan_type": "plus"}
        if weekly_used is not None:
            rates["secondary"] = _limit(weekly_used, 10_080, 1_900_500_000)
    payload: dict[str, object] = {
        "type": "token_count",
        "info": {
            "total_token_usage": {"total_tokens": 9_000_000},
            "last_token_usage": {
                "input_tokens": max(0, context_used - 1_000),
                "cached_input_tokens": max(0, context_used - 5_000),
                "output_tokens": 1_000,
                "reasoning_output_tokens": 200,
                "total_tokens": context_used,
            },
            "model_context_window": context_window,
        },
        "rate_limits": rates,
    }
    if cwd is not None:
        payload["cwd"] = cwd
    return {"timestamp": timestamp, "type": "event_msg", "payload": payload}


def _turn_context(timestamp: float, model: str, effort: str, cwd: str) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "type": "turn_context",
        "payload": {"model": model, "effort": effort, "cwd": cwd},
    }


def _json_line(event: dict[str, object]) -> bytes:
    return json.dumps(event, separators=(",", ":")).encode("utf-8")


def _write_rollout(path: Path, events: list[dict[str, object]], *, final_newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"\n".join(_json_line(event) for event in events)
    if final_newline and data:
        data += b"\n"
    path.write_bytes(data)


class SessionMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sessions = self.root / "sessions"
        self.clock = _Clock()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _monitor(self, **kwargs) -> SessionMonitor:
        return SessionMonitor(
            self.sessions,
            clock=self.clock,
            stale_after=120,
            full_scan_interval=60,
            **kwargs,
        )

    def test_missing_sessions_directory_returns_no_data(self) -> None:
        snapshot = self._monitor().refresh(force_discovery=True)

        self.assertEqual(snapshot.status, DataStatus.NO_DATA)
        self.assertIsNone(snapshot.five_hour)
        self.assertIsNone(snapshot.latest_file)

    def test_backward_bootstrap_keeps_valid_rates_when_latest_event_has_null(self) -> None:
        path = self.sessions / "2026" / "09" / "22" / "rollout-null-tail.jsonl"
        _write_rollout(
            path,
            [
                _token_event(self.clock.value - 20, used=31, weekly_used=48, context_used=40_000),
                _token_event(self.clock.value - 5, null_rates=True, context_used=72_000),
            ],
        )

        snapshot = self._monitor().refresh(force_discovery=True)

        self.assertEqual(snapshot.five_hour.used_percent, 31)
        self.assertEqual(snapshot.weekly.used_percent, 48)
        self.assertEqual(snapshot.context.used_tokens, 72_000)
        self.assertEqual(snapshot.status, DataStatus.LIVE)

    def test_newest_valid_rate_snapshot_wins_across_files(self) -> None:
        older = self.sessions / "rollout-older.jsonl"
        newer = self.sessions / "rollout-newer.jsonl"
        _write_rollout(older, [_token_event(self.clock.value - 30, used=12)])
        _write_rollout(newer, [_token_event(self.clock.value - 10, used=63)])

        snapshot = self._monitor().refresh(force_discovery=True)

        self.assertEqual(snapshot.five_hour.used_percent, 63)
        self.assertEqual(snapshot.five_hour.source_path, newer)

    def test_later_file_missing_secondary_does_not_erase_cached_weekly_rate(self) -> None:
        first = self.sessions / "rollout-weekly.jsonl"
        second = self.sessions / "rollout-five-only.jsonl"
        _write_rollout(first, [_token_event(self.clock.value - 30, used=20, weekly_used=61)])
        _write_rollout(second, [_token_event(self.clock.value - 5, used=35, weekly_used=None)])

        snapshot = self._monitor().refresh(force_discovery=True)

        self.assertEqual(snapshot.five_hour.used_percent, 35)
        self.assertEqual(snapshot.five_hour.source_path, second)
        self.assertEqual(snapshot.weekly.used_percent, 61)
        self.assertEqual(snapshot.weekly.source_path, first)

    def test_append_only_partial_line_is_completed_on_next_refresh(self) -> None:
        path = self.sessions / "rollout-partial.jsonl"
        first = _json_line(_token_event(self.clock.value - 20, used=10, context_used=20_000)) + b"\n"
        completed = _json_line(_token_event(self.clock.value - 5, used=70, context_used=80_000))
        split_at = len(completed) // 2
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(first + completed[:split_at])
        monitor = self._monitor()

        initial = monitor.refresh(force_discovery=True)
        self.assertEqual(initial.five_hour.used_percent, 10)
        self.assertEqual(initial.context.used_tokens, 20_000)

        with path.open("ab") as handle:
            handle.write(completed[split_at:] + b"\n")
        self.clock.advance()
        updated = monitor.refresh()

        self.assertEqual(updated.five_hour.used_percent, 70)
        self.assertEqual(updated.context.used_tokens, 80_000)
        self.assertEqual(monitor._states[path].pending, b"")

    def test_size_growth_is_detected_even_when_mtime_is_pinned(self) -> None:
        path = self.sessions / "rollout-growing.jsonl"
        _write_rollout(path, [_token_event(self.clock.value - 30, used=11)])
        old_mtime = 1_600_000_000
        os.utime(path, (old_mtime, old_mtime))
        monitor = self._monitor()
        initial = monitor.refresh(force_discovery=True)
        self.assertEqual(initial.five_hour.used_percent, 11)

        with path.open("ab") as handle:
            handle.write(_json_line(_token_event(self.clock.value - 2, used=47)) + b"\n")
        os.utime(path, (old_mtime, old_mtime))
        self.clock.advance()
        updated = monitor.refresh()

        self.assertEqual(updated.five_hour.used_percent, 47)
        self.assertGreater(monitor._states[path].activity_at, old_mtime)

    def test_new_session_in_known_directory_is_discovered_without_restart(self) -> None:
        first = self.sessions / "nested" / "rollout-first.jsonl"
        second = self.sessions / "nested" / "rollout-second.jsonl"
        _write_rollout(first, [_token_event(self.clock.value - 20, context_used=21_000)])
        monitor = self._monitor()
        initial = monitor.refresh(force_discovery=True)
        self.assertEqual(initial.latest_file, first)

        _write_rollout(second, [_token_event(self.clock.value - 2, context_used=92_000)])
        self.clock.advance()
        updated = monitor.refresh(force_discovery=False)

        self.assertEqual(updated.latest_file, second)
        self.assertEqual(updated.context.used_tokens, 92_000)

    def test_context_session_switches_to_the_file_that_becomes_active(self) -> None:
        first = self.sessions / "rollout-alpha.jsonl"
        second = self.sessions / "rollout-beta.jsonl"
        _write_rollout(
            first,
            [
                _turn_context(self.clock.value - 40, "model-alpha", "low", r"C:\Work\Alpha"),
                _token_event(self.clock.value - 39, used=20, context_used=30_000),
            ],
        )
        _write_rollout(
            second,
            [
                _turn_context(self.clock.value - 20, "model-beta", "high", r"C:\Work\Beta"),
                _token_event(self.clock.value - 19, used=25, context_used=60_000),
            ],
        )
        monitor = self._monitor()
        initial = monitor.refresh(force_discovery=True)
        self.assertEqual(initial.latest_file, second)
        self.assertEqual(initial.model, "model-beta")
        self.assertEqual(initial.active_session, "Beta")

        with first.open("ab") as handle:
            handle.write(
                _json_line(
                    _turn_context(self.clock.value - 2, "model-alpha-new", "ultra", r"C:\Work\Alpha")
                )
                + b"\n"
            )
            handle.write(
                _json_line(_token_event(self.clock.value - 1, used=30, context_used=95_000)) + b"\n"
            )
        self.clock.advance()
        switched = monitor.refresh()

        self.assertEqual(switched.latest_file, first)
        self.assertEqual(switched.context.used_tokens, 95_000)
        self.assertEqual(switched.model, "model-alpha-new")
        self.assertEqual(switched.reasoning_effort, "ultra")
        self.assertEqual(switched.active_session, "Alpha")

    def test_disappearing_file_keeps_last_valid_cache_and_does_not_crash(self) -> None:
        path = self.sessions / "rollout-vanishing.jsonl"
        _write_rollout(path, [_token_event(self.clock.value - 5, used=36)])
        monitor = self._monitor()
        initial = monitor.refresh(force_discovery=True)
        self.assertEqual(initial.five_hour.used_percent, 36)

        path.unlink()
        self.clock.advance(5)
        cached = monitor.refresh(force_discovery=True)

        self.assertEqual(cached.five_hour.used_percent, 36)
        self.assertIsNone(cached.error_summary)

    def test_empty_and_malformed_files_do_not_break_other_sessions(self) -> None:
        self.sessions.mkdir(parents=True)
        (self.sessions / "rollout-empty.jsonl").write_bytes(b"")
        (self.sessions / "rollout-bad.jsonl").write_bytes(b"{bad json}\n")
        valid = self.sessions / "rollout-valid.jsonl"
        _write_rollout(valid, [_token_event(self.clock.value - 2, used=22)])

        snapshot = self._monitor().refresh(force_discovery=True)

        self.assertEqual(snapshot.five_hour.used_percent, 22)
        self.assertEqual(snapshot.status, DataStatus.LIVE)

    def test_reverse_bootstrap_scan_obeys_configured_byte_bound(self) -> None:
        path = self.sessions / "rollout-bounded.jsonl"
        early_rate = _json_line(_token_event(self.clock.value - 10, used=88)) + b"\n"
        large_irrelevant = json.dumps(
            {
                "timestamp": self.clock.value - 1,
                "type": "response_item",
                "payload": {"type": "message", "content": "x" * (600 * 1024)},
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(early_rate + large_irrelevant)

        snapshot = self._monitor(reverse_scan_limit=512 * 1024).refresh(force_discovery=True)

        # The first-line metadata pass intentionally does not apply quota data,
        # and the bounded reverse scan stops before reaching the old rate event.
        self.assertEqual(snapshot.status, DataStatus.NO_DATA)
        self.assertIsNone(snapshot.five_hour)

    def test_recent_file_limit_bounds_bootstrap_work(self) -> None:
        self.sessions.mkdir(parents=True)
        paths: list[Path] = []
        for index in range(6):
            path = self.sessions / f"rollout-{index}.jsonl"
            _write_rollout(path, [_token_event(self.clock.value - 20 + index, used=index)])
            os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
            paths.append(path)

        monitor = self._monitor(max_recent_files=2)
        snapshot = monitor.refresh(force_discovery=True)

        self.assertLessEqual(len(monitor._states), 2)
        self.assertEqual(snapshot.latest_file, paths[-1])
        self.assertEqual(snapshot.five_hour.used_percent, 5)


class CodexHomeResolutionTests(unittest.TestCase):
    def test_codex_home_environment_has_priority(self) -> None:
        custom = str(Path(tempfile.gettempdir()) / "synthetic-codex-home")
        profile = str(Path(tempfile.gettempdir()) / "synthetic-profile")

        self.assertEqual(
            resolve_codex_home({"CODEX_HOME": custom, "USERPROFILE": profile}),
            Path(custom),
        )
        self.assertEqual(
            resolve_sessions_dir({"CODEX_HOME": custom, "USERPROFILE": profile}),
            Path(custom) / "sessions",
        )

    def test_userprofile_is_used_when_codex_home_is_blank(self) -> None:
        profile = str(Path(tempfile.gettempdir()) / "synthetic-profile")

        self.assertEqual(
            resolve_codex_home({"CODEX_HOME": "  ", "USERPROFILE": profile}),
            Path(profile) / ".codex",
        )


if __name__ == "__main__":
    unittest.main()

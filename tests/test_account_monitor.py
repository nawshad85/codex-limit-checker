from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.codex_app_server import AppServerError, RateLimitState
from app.models import DataStatus, LimitKind, RateWindow
from app.monitor import AccountUsageMonitor, SessionMonitor
from app.settings import RateSnapshotStore


class _Clock:
    def __init__(self, value: float = 1_900_001_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _FakeAppServer:
    def __init__(self, response: RateLimitState | Exception) -> None:
        self.response = response
        self.calls = 0
        self.closed = False

    def read_rate_limits(self) -> RateLimitState:
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self) -> None:
        self.closed = True


class AccountUsageMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sessions = self.root / "sessions"
        self.rate_store = RateSnapshotStore(self.root / "state" / "rate_limits.json")
        self.clock = _Clock()

    def _rate(self, kind: LimitKind, used: float, *, source: str = "app-server") -> RateWindow:
        return RateWindow(
            kind=kind,
            used_percent=used,
            window_minutes=300 if kind is LimitKind.FIVE_HOUR else 10_080,
            resets_at=self.clock.value + (3_600 if kind is LimitKind.FIVE_HOUR else 200_000),
            observed_at=self.clock.value,
            source=source,
        )

    def _account(
        self,
        *,
        five_hour: float | None = 18,
        weekly: float | None = 34,
        plan: str | None = "plus",
    ) -> RateLimitState:
        return RateLimitState(
            five_hour=self._rate(LimitKind.FIVE_HOUR, five_hour) if five_hour is not None else None,
            weekly=self._rate(LimitKind.WEEKLY, weekly) if weekly is not None else None,
            plan=plan,
            fetched_at=self.clock.value,
            limit_id="codex",
        )

    def _monitor(self, client: _FakeAppServer, *, store: RateSnapshotStore | None = None) -> AccountUsageMonitor:
        return AccountUsageMonitor(
            SessionMonitor(self.sessions, clock=self.clock, stale_after=120),
            app_server=client,
            rate_store=store or self.rate_store,
            account_interval=20,
            clock=self.clock,
        )

    def _write_rollout(self, *, five_hour: float = 10, weekly: float = 25) -> Path:
        self.sessions.mkdir(parents=True, exist_ok=True)
        path = self.sessions / "rollout-synthetic.jsonl"
        event = {
            "timestamp": self.clock.value - 10,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": 52_000},
                    "model_context_window": 258_400,
                },
                "rate_limits": {
                    "primary": {
                        "used_percent": five_hour,
                        "window_minutes": 300,
                        "resets_at": self.clock.value + 3_600,
                    },
                    "secondary": {
                        "used_percent": weekly,
                        "window_minutes": 10_080,
                        "resets_at": self.clock.value + 200_000,
                    },
                },
            },
        }
        path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        return path

    def test_app_server_rates_take_priority_while_context_stays_session_specific(self) -> None:
        path = self._write_rollout(five_hour=10, weekly=25)
        monitor = self._monitor(_FakeAppServer(self._account(five_hour=18, weekly=34)))

        snapshot = monitor.refresh(force_discovery=True)

        self.assertEqual(snapshot.five_hour.used_percent, 18)
        self.assertEqual(snapshot.five_hour.remaining_percent, 82)
        self.assertEqual(snapshot.weekly.used_percent, 34)
        self.assertEqual(snapshot.five_hour.source, "app-server")
        self.assertEqual(snapshot.weekly.source, "app-server")
        self.assertEqual(snapshot.context.used_tokens, 52_000)
        self.assertEqual(snapshot.latest_file, path)
        self.assertEqual(snapshot.plan, "plus")
        self.assertEqual(snapshot.status, DataStatus.LIVE)
        self.assertEqual(snapshot.rate_source, "app-server")

    def test_work_usage_change_is_seen_without_any_rollout_change(self) -> None:
        path = self._write_rollout(five_hour=10)
        client = _FakeAppServer(self._account(five_hour=12))
        monitor = self._monitor(client)
        first = monitor.refresh(force_discovery=True)
        file_size = path.stat().st_size
        file_mtime = path.stat().st_mtime_ns

        client.response = self._account(five_hour=18)
        self.clock.advance(20)
        changed = monitor.refresh()

        self.assertEqual(first.five_hour.used_percent, 12)
        self.assertEqual(changed.five_hour.used_percent, 18)
        self.assertEqual(changed.five_hour.remaining_percent, 82)
        self.assertEqual(changed.context.used_tokens, first.context.used_tokens)
        self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), (file_size, file_mtime))
        self.assertEqual(client.calls, 2)
        self.assertEqual(changed.status, DataStatus.LIVE)

    def test_account_limits_stay_live_without_a_codex_session(self) -> None:
        monitor = self._monitor(_FakeAppServer(self._account(five_hour=21, weekly=42)))

        snapshot = monitor.refresh(force_discovery=True)

        self.assertEqual(snapshot.status, DataStatus.LIVE)
        self.assertEqual(snapshot.rate_source, "app-server")
        self.assertEqual(snapshot.five_hour.used_percent, 21)
        self.assertEqual(snapshot.weekly.used_percent, 42)
        self.assertIsNone(snapshot.context)
        self.assertIsNone(snapshot.latest_file)

    def test_partial_account_result_uses_recent_rollout_for_missing_window(self) -> None:
        self._write_rollout(five_hour=10, weekly=25)
        monitor = self._monitor(_FakeAppServer(self._account(five_hour=39, weekly=None)))

        snapshot = monitor.refresh(force_discovery=True)

        self.assertEqual(snapshot.five_hour.used_percent, 39)
        self.assertEqual(snapshot.five_hour.source, "app-server")
        self.assertEqual(snapshot.weekly.used_percent, 25)
        self.assertEqual(snapshot.weekly.source, "rollout")
        self.assertEqual(snapshot.status, DataStatus.LIVE)

    def test_only_weekly_account_window_does_not_invent_five_hour(self) -> None:
        monitor = self._monitor(_FakeAppServer(self._account(five_hour=None, weekly=47)))

        snapshot = monitor.refresh(force_discovery=True)

        self.assertIsNone(snapshot.five_hour)
        self.assertEqual(snapshot.weekly.used_percent, 47)
        self.assertEqual(snapshot.status, DataStatus.LIVE)

    def test_partial_account_result_keeps_last_cached_missing_window(self) -> None:
        client = _FakeAppServer(self._account(five_hour=31, weekly=44))
        monitor = self._monitor(client)
        monitor.refresh()
        client.response = self._account(five_hour=None, weekly=49)

        snapshot = monitor.refresh(force_account=True)

        self.assertEqual(snapshot.five_hour.used_percent, 31)
        self.assertEqual(snapshot.five_hour.source, "cache")
        self.assertEqual(snapshot.weekly.used_percent, 49)
        self.assertEqual(snapshot.weekly.source, "app-server")
        self.assertEqual(snapshot.status, DataStatus.LIVE)
        self.assertTrue(snapshot.rate_from_cache)

    def test_app_server_failure_uses_recent_rollout_fallback(self) -> None:
        self._write_rollout(five_hour=27, weekly=51)
        monitor = self._monitor(_FakeAppServer(AppServerError("unavailable")))

        snapshot = monitor.refresh(force_discovery=True)

        self.assertEqual(snapshot.five_hour.used_percent, 27)
        self.assertEqual(snapshot.weekly.used_percent, 51)
        self.assertEqual(snapshot.status, DataStatus.FALLBACK)
        self.assertEqual(snapshot.rate_source, "rollout")
        self.assertEqual(snapshot.error_summary, "AppServerError")

    def test_persisted_normalized_rates_survive_restart_as_stale_cache(self) -> None:
        initial = self._monitor(_FakeAppServer(self._account(five_hour=36, weekly=58)))
        initial.refresh()
        self.assertTrue(self.rate_store.path.is_file())

        restarted = self._monitor(_FakeAppServer(AppServerError("offline")))
        snapshot = restarted.refresh(force_discovery=True)

        self.assertEqual(snapshot.five_hour.used_percent, 36)
        self.assertEqual(snapshot.weekly.used_percent, 58)
        self.assertEqual(snapshot.five_hour.source, "cache")
        self.assertEqual(snapshot.status, DataStatus.STALE)
        self.assertEqual(snapshot.rate_source, "cache")
        self.assertTrue(snapshot.rate_from_cache)

    def test_no_rate_from_any_source_is_no_data(self) -> None:
        monitor = self._monitor(_FakeAppServer(AppServerError("unavailable")))

        snapshot = monitor.refresh(force_discovery=True)

        self.assertIsNone(snapshot.five_hour)
        self.assertIsNone(snapshot.weekly)
        self.assertEqual(snapshot.status, DataStatus.NO_DATA)
        self.assertIsNone(snapshot.rate_source)

    def test_account_poll_uses_its_own_cadence_and_force_refresh_bypasses_it(self) -> None:
        client = _FakeAppServer(self._account(five_hour=10))
        monitor = self._monitor(client)

        monitor.refresh()
        self.assertEqual(client.calls, 1)
        client.response = self._account(five_hour=20)
        self.clock.advance(19)
        before_due = monitor.refresh()
        self.assertEqual(client.calls, 1)
        self.assertEqual(before_due.five_hour.used_percent, 10)

        self.clock.advance(1)
        due = monitor.refresh()
        self.assertEqual(client.calls, 2)
        self.assertEqual(due.five_hour.used_percent, 20)

        client.response = self._account(five_hour=30)
        forced = monitor.refresh(force_account=True)
        self.assertEqual(client.calls, 3)
        self.assertEqual(forced.five_hour.used_percent, 30)


if __name__ == "__main__":
    unittest.main()

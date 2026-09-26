from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.models import DataStatus, LimitKind, RateWindow, UsageSnapshot
from app.notifications import NotificationManager
from app.settings import AppSettings, RateSnapshotStore, SettingsStore
from app.utils import format_countdown, format_tokens
from app.windows import MonitorWorkArea, clamp_to_work_area, preset_position


class SettingsTests(unittest.TestCase):
    def test_malformed_settings_fall_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{not json", encoding="utf-8")
            loaded = SettingsStore(path).load()
            self.assertEqual(loaded.opacity, 0.95)
            self.assertEqual(loaded.refresh_interval, 4.0)
            self.assertEqual(loaded.account_refresh_interval, 20.0)

    def test_loaded_values_are_validated_field_by_field(self) -> None:
        loaded = AppSettings.from_mapping(
            {
                "opacity": 5,
                "refresh_interval": 0.1,
                "account_refresh_interval": 2,
                "x": "wrong",
                "y": -200,
                "position_preset": "offscreen_magic",
                "notification_thresholds": [20, 10, 10, -1, "bad"],
            }
        )
        self.assertEqual(loaded.opacity, 1.0)
        self.assertEqual(loaded.refresh_interval, 2.0)
        self.assertEqual(loaded.account_refresh_interval, 15.0)
        self.assertIsNone(loaded.x)
        self.assertEqual(loaded.y, -200)
        self.assertIsNone(loaded.position_preset)
        self.assertEqual(loaded.notification_thresholds, [20, 10])

    def test_atomic_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "settings.json"
            store = SettingsStore(path)
            expected = AppSettings(x=123, y=456, opacity=0.72, expanded=True)
            self.assertTrue(store.save(expected))
            loaded = store.load()
            self.assertEqual((loaded.x, loaded.y), (123, 456))
            self.assertAlmostEqual(loaded.opacity, 0.72)
            self.assertTrue(loaded.expanded)
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_rate_cache_contains_only_normalized_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rate_limits.json"
            store = RateSnapshotStore(path)
            rate = RateWindow(
                kind=LimitKind.FIVE_HOUR,
                used_percent=18,
                window_minutes=300,
                resets_at=2_000,
                observed_at=1_000,
                source_path=Path("private-rollout.jsonl"),
                source="app-server",
            )
            self.assertTrue(store.save({LimitKind.FIVE_HOUR: rate}))
            self.assertNotIn("private-rollout", path.read_text(encoding="utf-8"))
            loaded = store.load()[LimitKind.FIVE_HOUR]
            self.assertEqual(loaded.used_percent, 18)
            self.assertEqual(loaded.source, "cache")
            self.assertIsNone(loaded.source_path)


class NotificationTests(unittest.TestCase):
    @staticmethod
    def snapshot(
        remaining: float,
        reset: float,
        status: DataStatus = DataStatus.LIVE,
        source: str = "rollout",
    ) -> UsageSnapshot:
        rate = RateWindow(
            kind=LimitKind.FIVE_HOUR,
            used_percent=100.0 - remaining,
            window_minutes=300,
            resets_at=reset,
            observed_at=1_000.0,
            source=source,
        )
        return UsageSnapshot(
            five_hour=rate,
            weekly=None,
            context=None,
            model=None,
            reasoning_effort=None,
            plan=None,
            active_session=None,
            working_directory=None,
            latest_file=None,
            latest_event_at=1_000.0,
            scanned_at=1_000.0,
            status=status,
        )

    def test_thresholds_notify_once_per_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = AppSettings(notification_thresholds=[20, 10, 5])
            sent: list[tuple[str, str]] = []
            manager = NotificationManager(
                settings,
                SettingsStore(Path(directory) / "settings.json"),
                sender=lambda title, message: not sent.append((title, message)),
            )
            self.assertEqual(manager.consider(self.snapshot(9, 2_000), now=1_000), 10)
            self.assertIsNone(manager.consider(self.snapshot(8, 2_000), now=1_001))
            self.assertEqual(manager.consider(self.snapshot(4, 2_000), now=1_002), 5)
            self.assertEqual(len(sent), 2)

    def test_new_reset_window_allows_notifications_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = AppSettings(notification_thresholds=[20])
            sent: list[str] = []
            manager = NotificationManager(
                settings,
                SettingsStore(Path(directory) / "settings.json"),
                sender=lambda _title, message: not sent.append(message),
            )
            manager.consider(self.snapshot(19, 2_000), now=1_000)
            manager.consider(self.snapshot(19, 3_000), now=1_001)
            self.assertEqual(len(sent), 2)

    def test_stale_or_expired_data_never_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sent: list[str] = []
            manager = NotificationManager(
                AppSettings(),
                SettingsStore(Path(directory) / "settings.json"),
                sender=lambda _title, message: not sent.append(message),
            )
            self.assertIsNone(manager.consider(self.snapshot(5, 2_000, DataStatus.STALE), now=1_000))
            self.assertIsNone(manager.consider(self.snapshot(5, 900), now=1_000))
            self.assertEqual(sent, [])

    def test_recent_fallback_can_notify_but_cached_quota_cannot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sent: list[str] = []
            manager = NotificationManager(
                AppSettings(),
                SettingsStore(Path(directory) / "settings.json"),
                sender=lambda _title, message: not sent.append(message),
            )
            self.assertIsNone(
                manager.consider(
                    self.snapshot(10, 2_000, DataStatus.STALE, "cache"), now=1_000
                )
            )
            self.assertEqual(
                manager.consider(
                    self.snapshot(10, 2_000, DataStatus.FALLBACK), now=1_000
                ),
                10,
            )
            self.assertIn("Work + Codex", sent[0])


class DisplayUtilityTests(unittest.TestCase):
    def test_compact_formatting(self) -> None:
        self.assertEqual(format_tokens(157_000), "157K")
        self.assertEqual(format_tokens(1_200_000), "1.2M")
        self.assertEqual(format_countdown(1_000 + 4 * 86_400 + 17 * 3_600, 1_000), "4d 17h")

    def test_positions_stay_inside_taskbar_aware_work_area(self) -> None:
        area = MonitorWorkArea(-1920, 0, 0, 1040, "left", False)
        self.assertEqual(clamp_to_work_area(-5_000, 5_000, 450, 320, area), (-1920, 720))
        x, y = preset_position("bottom_right", 450, 320, area)
        self.assertGreaterEqual(x, area.left)
        self.assertLessEqual(x + 450, area.right)
        self.assertLessEqual(y + 320, area.bottom)


if __name__ == "__main__":
    unittest.main()

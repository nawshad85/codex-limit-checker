from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .models import LimitKind, RateWindow
from .utils import clamp, parse_timestamp, safe_float, safe_int


LOGGER = logging.getLogger(__name__)


@dataclass
class AppSettings:
    x: Optional[int] = None
    y: Optional[int] = None
    opacity: float = 0.95
    expanded: bool = False
    view_mode: str = "icon"
    refresh_interval: float = 4.0
    account_refresh_interval: float = 20.0
    notifications_enabled: bool = True
    notification_thresholds: list[int] = field(default_factory=lambda: [20, 10, 5])
    always_on_top: bool = True
    position_preset: Optional[str] = None
    monitor_name: Optional[str] = None
    notified_window: Optional[str] = None
    notified_thresholds: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.view_mode, str) or self.view_mode not in {"icon", "compact", "expanded"}:
            self.view_mode = "icon"
        if self.expanded and self.view_mode == "icon":
            self.view_mode = "expanded"
        self.expanded = self.view_mode == "expanded"

    @classmethod
    def from_mapping(cls, raw: Any) -> "AppSettings":
        defaults = cls()
        if not isinstance(raw, dict):
            return defaults

        def optional_int(name: str) -> Optional[int]:
            value = raw.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        opacity = raw.get("opacity", defaults.opacity)
        if not isinstance(opacity, (int, float)) or isinstance(opacity, bool):
            opacity = defaults.opacity
        refresh = raw.get("refresh_interval", defaults.refresh_interval)
        if not isinstance(refresh, (int, float)) or isinstance(refresh, bool):
            refresh = defaults.refresh_interval

        account_refresh = raw.get("account_refresh_interval", defaults.account_refresh_interval)
        if not isinstance(account_refresh, (int, float)) or isinstance(account_refresh, bool):
            account_refresh = defaults.account_refresh_interval

        thresholds = raw.get("notification_thresholds", defaults.notification_thresholds)
        if not isinstance(thresholds, list):
            thresholds = defaults.notification_thresholds
        clean_thresholds = sorted(
            {int(v) for v in thresholds if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < int(v) < 100},
            reverse=True,
        ) or defaults.notification_thresholds

        sent = raw.get("notified_thresholds", [])
        clean_sent = sorted(
            {int(v) for v in sent if isinstance(v, (int, float)) and not isinstance(v, bool)},
            reverse=True,
        ) if isinstance(sent, list) else []

        preset = raw.get("position_preset")
        allowed_presets = {
            "top_left", "top_center", "top_right",
            "bottom_left", "bottom_center", "bottom_right",
        }
        view_mode = raw.get("view_mode")
        if not isinstance(view_mode, str) or view_mode not in {"icon", "compact", "expanded"}:
            view_mode = "expanded" if raw.get("expanded", False) else "icon"
        return cls(
            x=optional_int("x"),
            y=optional_int("y"),
            opacity=float(clamp(float(opacity), 0.50, 1.00)),
            expanded=view_mode == "expanded",
            view_mode=view_mode,
            refresh_interval=float(clamp(float(refresh), 2.0, 60.0)),
            account_refresh_interval=float(clamp(float(account_refresh), 15.0, 300.0)),
            notifications_enabled=bool(raw.get("notifications_enabled", defaults.notifications_enabled)),
            notification_thresholds=clean_thresholds,
            always_on_top=bool(raw.get("always_on_top", defaults.always_on_top)),
            position_preset=preset if preset in allowed_presets else None,
            monitor_name=raw.get("monitor_name") if isinstance(raw.get("monitor_name"), str) else None,
            notified_window=raw.get("notified_window") if isinstance(raw.get("notified_window"), str) else None,
            notified_thresholds=clean_sent,
        )


def default_settings_path(env: Optional[dict[str, str]] = None) -> Path:
    values = os.environ if env is None else env
    appdata = values.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "CodexUsageMonitor" / "settings.json"


class SettingsStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or default_settings_path()

    def load(self) -> AppSettings:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return AppSettings.from_mapping(json.load(handle))
        except FileNotFoundError:
            return AppSettings()
        except (OSError, ValueError, TypeError) as exc:
            LOGGER.warning("Settings could not be loaded: %s", type(exc).__name__)
            return AppSettings()

    def save(self, settings: AppSettings) -> bool:
        temporary = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(asdict(settings), handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, self.path)
            return True
        except OSError as exc:
            LOGGER.warning("Settings could not be saved: %s", type(exc).__name__)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False


class RateSnapshotStore:
    """Persist only normalized quota values, never raw account responses."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or default_settings_path().with_name("rate_limits.json")

    def load(self) -> dict[LimitKind, RateWindow]:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError) as exc:
            LOGGER.warning("Rate cache could not be loaded: %s", type(exc).__name__)
            return {}
        if not isinstance(data, dict):
            return {}

        rates: dict[LimitKind, RateWindow] = {}
        for kind in (LimitKind.FIVE_HOUR, LimitKind.WEEKLY):
            raw = data.get(kind.value)
            if not isinstance(raw, dict):
                continue
            used = safe_float(raw.get("used_percent"))
            window = safe_int(raw.get("window_minutes"))
            observed = parse_timestamp(raw.get("observed_at"))
            if used is None or observed is None or not 0 <= used <= 100:
                continue
            if window is not None and window <= 0:
                continue
            rates[kind] = RateWindow(
                kind=kind,
                used_percent=used,
                window_minutes=window,
                resets_at=parse_timestamp(raw.get("resets_at")),
                observed_at=observed,
                source="cache",
            )
        return rates

    def save(self, rates: dict[LimitKind, RateWindow]) -> bool:
        if not rates:
            return False
        data = {
            kind.value: {
                "used_percent": rate.used_percent,
                "window_minutes": rate.window_minutes,
                "resets_at": rate.resets_at,
                "observed_at": rate.observed_at,
            }
            for kind, rate in rates.items()
            if kind in (LimitKind.FIVE_HOUR, LimitKind.WEEKLY)
        }
        temporary = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, self.path)
            return True
        except OSError as exc:
            LOGGER.warning("Rate cache could not be saved: %s", type(exc).__name__)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            return False

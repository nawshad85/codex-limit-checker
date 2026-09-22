from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .models import DataStatus, UsageSnapshot
from .settings import AppSettings, SettingsStore
from .utils import format_countdown


LOGGER = logging.getLogger(__name__)
ToastSender = Callable[[str, str], bool]


class NotificationManager:
    """Send each quota threshold at most once per five-hour window."""

    def __init__(
        self,
        settings: AppSettings,
        settings_store: SettingsStore,
        *,
        sender: Optional[ToastSender] = None,
        app_name: str = "Codex Usage Monitor",
    ) -> None:
        self.settings = settings
        self.settings_store = settings_store
        self.app_name = app_name
        self._sender = sender or self._send_winotify
        self._warned_unavailable = False

    def consider(self, snapshot: UsageSnapshot, *, now: Optional[float] = None) -> Optional[int]:
        """Consider a snapshot and return the threshold notified, if any."""
        if not self.settings.notifications_enabled:
            return None
        rate = snapshot.five_hour
        if (
            rate is None
            or rate.resets_at is None
            or snapshot.status is not DataStatus.LIVE
            or snapshot.rate_from_cache
        ):
            return None

        current = time.time() if now is None else float(now)
        if rate.resets_at <= current:
            return None

        # A reset timestamp is the most stable local identifier for an account
        # rate-limit window.  Rounding absorbs harmless JSON number formatting.
        window_key = str(int(round(rate.resets_at)))
        if self.settings.notified_window != window_key:
            self.settings.notified_window = window_key
            self.settings.notified_thresholds = []
            self.settings_store.save(self.settings)

        configured = sorted(set(self.settings.notification_thresholds), reverse=True)
        sent = set(self.settings.notified_thresholds)
        crossed = [value for value in configured if rate.remaining_percent <= value and value not in sent]
        if not crossed:
            return None

        # When the first observation is already below several levels, emit only
        # the most severe applicable toast and mark the less severe levels too.
        threshold = min(crossed)
        sent.update(value for value in configured if rate.remaining_percent <= value)
        self.settings.notified_thresholds = sorted(sent, reverse=True)
        self.settings_store.save(self.settings)

        reset_text = format_countdown(rate.resets_at, current)
        message = (
            f"Only {threshold}% of your 5-hour Codex limit remains. "
            f"Reset in {reset_text}."
        )
        try:
            self._sender(self.app_name, message)
        except Exception as exc:  # Notifications must never affect monitoring.
            LOGGER.warning("Windows notification failed: %s", type(exc).__name__)
        return threshold

    def reset_history(self) -> None:
        self.settings.notified_window = None
        self.settings.notified_thresholds = []
        self.settings_store.save(self.settings)

    def _send_winotify(self, title: str, message: str) -> bool:
        try:
            from winotify import Notification

            Notification(
                app_id=self.app_name,
                title=title,
                msg=message,
                duration="short",
            ).show()
            return True
        except (ImportError, OSError, RuntimeError) as exc:
            if not self._warned_unavailable:
                LOGGER.warning("Windows notifications unavailable: %s", type(exc).__name__)
                self._warned_unavailable = True
            return False


# A descriptive alias keeps the class easy to discover from integration code.
QuotaNotifier = NotificationManager


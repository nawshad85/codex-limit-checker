from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from app import __version__
from app.logging_config import configure_logging
from app.models import DataStatus, UsageSnapshot
from app.monitor import AccountUsageMonitor, MonitorService, SessionMonitor
from app.settings import RateSnapshotStore, SettingsStore
from app.utils import format_countdown, format_percent, format_tokens, resolve_sessions_dir


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local floating monitor for shared Work and Codex limits and Codex context."
    )
    parser.add_argument("--debug", action="store_true", help="enable privacy-safe debug logging")
    parser.add_argument("--once", action="store_true", help="print one safe usage summary and exit")
    parser.add_argument(
        "--check-ratelimits",
        action="store_true",
        help="query account limits once and print a safe summary",
    )
    parser.add_argument("--version", action="version", version=f"Codex Usage Monitor {__version__}")
    return parser


def print_once(snapshot: UsageSnapshot, *, rates_only: bool = False) -> int:
    now = time.time()

    def print_limit(title: str, limit) -> None:
        print(f"{title}:")
        if limit is None:
            print("Used: N/A")
            print("Remaining: N/A")
            print("Reset: N/A")
        else:
            print(f"Used: {format_percent(limit.used_percent)}")
            print(f"Remaining: {format_percent(limit.remaining_percent)}")
            print(f"Reset: {format_countdown(limit.resets_at, now)}")

    print("Work + Codex Rate Limits" if rates_only else "Work + Codex Usage Monitor")
    print()
    print_limit("5-hour", snapshot.five_hour)
    print()
    print_limit("Weekly", snapshot.weekly)
    if not rates_only:
        print()
        print("Context:")
        if snapshot.context is None:
            print("N/A")
        else:
            marker = "~" if snapshot.context.estimated else ""
            print(
                f"{marker}{format_tokens(snapshot.context.used_tokens)} / "
                f"{format_tokens(snapshot.context.window_tokens)}"
            )
            print(format_percent(snapshot.context.used_percent))
        if snapshot.model:
            print()
            print(f"Model: {snapshot.model}")
        if snapshot.reasoning_effort:
            print(f"Effort: {snapshot.reasoning_effort}")
    print()
    print(f"Source: {snapshot.rate_source or 'N/A'}")
    print(f"Status: {snapshot.status.value}")
    if rates_only:
        print(f"App-server: {'connected' if snapshot.status is DataStatus.LIVE else 'unavailable'}")
        if snapshot.account_refreshed_at is not None:
            refreshed = datetime.fromtimestamp(snapshot.account_refreshed_at).strftime("%Y-%m-%d %H:%M:%S")
            print(f"Last successful account refresh: {refreshed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    configure_logging(arguments.debug)
    sessions_dir = resolve_sessions_dir()
    store = SettingsStore()
    settings = store.load()
    monitor = AccountUsageMonitor(
        SessionMonitor(sessions_dir),
        rate_store=RateSnapshotStore(store.path.with_name("rate_limits.json")),
        account_interval=settings.account_refresh_interval,
    )
    if arguments.once or arguments.check_ratelimits:
        try:
            snapshot = monitor.refresh(force_discovery=True, force_account=True)
            return print_once(snapshot, rates_only=arguments.check_ratelimits)
        finally:
            monitor.close()

    from app.ui import CodexUsageMonitorUI
    from app.windows import configure_dpi_awareness
    import tkinter as tk

    configure_dpi_awareness()
    service = MonitorService(monitor, interval=settings.refresh_interval)
    root = tk.Tk()
    application = CodexUsageMonitorUI(
        root=root,
        settings=settings,
        settings_store=store,
        monitor_service=service,
        sessions_dir=sessions_dir,
    )
    application.run()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from .codex_app_server import CodexAppServerClient, RateLimitState
from .models import (
    ContextUsage,
    DataStatus,
    FileState,
    LimitKind,
    ParsedUpdate,
    RateWindow,
    UsageSnapshot,
)
from .parser import RolloutParser
from .settings import RateSnapshotStore
from .utils import leaf_name


LOGGER = logging.getLogger(__name__)


class SessionMonitor:
    """Discover recent rollouts and incrementally tail them read-only."""

    def __init__(
        self,
        sessions_dir: Path,
        *,
        max_recent_files: int = 12,
        full_scan_interval: float = 60.0,
        stale_after: float = 120.0,
        reverse_scan_limit: int = 64 * 1024 * 1024,
        clock=time.time,
    ) -> None:
        self.sessions_dir = Path(sessions_dir)
        self.max_recent_files = max(2, max_recent_files)
        self.full_scan_interval = max(5.0, full_scan_interval)
        self.stale_after = max(15.0, stale_after)
        self.reverse_scan_limit = max(512 * 1024, reverse_scan_limit)
        self.clock = clock
        self.parser = RolloutParser()
        self._known_paths: set[Path] = set()
        self._known_parents: set[Path] = set()
        self._known_sizes: dict[Path, int] = {}
        self._path_activity: dict[Path, float] = {}
        self._states: dict[Path, FileState] = {}
        self._last_full_scan = 0.0
        self._last_rates: dict[LimitKind, RateWindow] = {}
        self._last_rate_keys: dict[LimitKind, tuple[float, float, int]] = {}
        self._last_plan: Optional[str] = None
        self._last_error: Optional[str] = None

    @property
    def latest_path(self) -> Optional[Path]:
        if not self._states:
            return None
        state = max(self._states.values(), key=lambda item: item.activity_at)
        return state.path

    def refresh(self, *, force_discovery: bool = False) -> UsageSnapshot:
        now = self.clock()
        self._last_error = None
        try:
            paths = self._discover(force_full=force_discovery)
        except OSError as exc:
            self._last_error = type(exc).__name__
            LOGGER.warning("Session discovery failed: %s", type(exc).__name__)
            paths = self._recent_cached_paths()

        for path in paths:
            try:
                state = self._states.get(path)
                if state is None:
                    LOGGER.info("Detected new rollout file: %s", path.name)
                    state = self._bootstrap(path)
                    self._states[path] = state
                else:
                    self._tail(state)
            except (OSError, PermissionError) as exc:
                self._last_error = type(exc).__name__
                LOGGER.warning("Could not read rollout file %s: %s", path.name, type(exc).__name__)
            except Exception as exc:  # Defensive: one unusual event must not kill the widget.
                self._last_error = type(exc).__name__
                LOGGER.exception("Unexpected parser failure in %s", path.name)

        return self._build_snapshot(paths, now)

    def _discover(self, *, force_full: bool) -> list[Path]:
        if not self.sessions_dir.is_dir():
            return self._recent_cached_paths()

        monotonic_now = time.monotonic()
        do_full = force_full or not self._known_paths or (
            monotonic_now - self._last_full_scan >= self.full_scan_interval
        )
        discovered: set[Path] = set()

        if do_full:
            for root, _directories, files in os.walk(self.sessions_dir, followlinks=False):
                root_path = Path(root)
                for name in files:
                    if name.startswith("rollout-") and name.endswith(".jsonl"):
                        discovered.add(root_path / name)
            self._last_full_scan = monotonic_now
            self._known_paths = discovered
        else:
            discovered.update(path for path in self._known_paths if path.is_file())
            scan_dirs = set(self._known_parents)
            scan_dirs.update(self._likely_date_directories())
            scan_dirs.add(self.sessions_dir)
            for directory in scan_dirs:
                try:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if entry.is_file(follow_symlinks=False) and entry.name.startswith("rollout-") and entry.name.endswith(".jsonl"):
                                discovered.add(Path(entry.path))
                except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
                    continue
            new_paths = discovered - self._known_paths
            self._known_paths.update(discovered)
            for path in new_paths:
                LOGGER.info("Discovered active rollout file: %s", path.name)

        self._known_parents.update(path.parent for path in discovered)
        sortable: list[tuple[float, float, Path]] = []
        for path in discovered:
            try:
                stat = path.stat()
            except (FileNotFoundError, PermissionError, OSError):
                continue
            prior_size = self._known_sizes.get(path)
            if prior_size is None:
                self._path_activity[path] = max(self._path_activity.get(path, 0.0), stat.st_mtime)
            elif prior_size != stat.st_size:
                # LastWriteTime can remain pinned while Codex holds a rollout
                # open. Size changes are therefore the primary activity signal.
                self._path_activity[path] = self.clock()
            self._known_sizes[path] = stat.st_size
            activity = self._path_activity.get(path, stat.st_mtime)
            sortable.append((activity, stat.st_mtime, path))
        sortable.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [path for _activity, _mtime, path in sortable[: self.max_recent_files]]

    def _likely_date_directories(self) -> set[Path]:
        days: set[date] = set()
        local_today = datetime.now().date()
        utc_today = datetime.now(tz=timezone.utc).date()
        for base in (local_today, utc_today):
            for delta in (-1, 0, 1):
                days.add(base + timedelta(days=delta))
        return {
            self.sessions_dir / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
            for day in days
        }

    def _recent_cached_paths(self) -> list[Path]:
        states = sorted(self._states.values(), key=lambda item: item.activity_at, reverse=True)
        return [state.path for state in states[: self.max_recent_files] if state.path.is_file()]

    def _bootstrap(self, path: Path) -> FileState:
        stat = path.stat()
        state = FileState(
            path=path,
            offset=stat.st_size,
            pending=self._read_trailing_partial(path, stat.st_size),
            file_mtime=stat.st_mtime,
            activity_at=stat.st_mtime,
        )

        # Session metadata is normally at the start and gives cwd/session identity.
        try:
            with path.open("rb") as handle:
                for _index in range(12):
                    line = handle.readline(2 * 1024 * 1024)
                    if not line:
                        break
                    if not line.endswith(b"\n") and len(line) >= 2 * 1024 * 1024:
                        continue
                    update = self.parser.parse_line(
                        line.rstrip(b"\r\n"),
                        source_path=path,
                        fallback_time=stat.st_mtime,
                    )
                    if update is not None:
                        self._apply_metadata(state, update)
        except (OSError, PermissionError):
            raise

        scanned = 0
        for offset, line, byte_cost in self._iter_reverse_complete_lines(path):
            scanned += byte_cost
            if scanned > self.reverse_scan_limit:
                LOGGER.debug("Stopped bounded backward scan for %s after %d bytes", path.name, scanned)
                break
            update = self.parser.parse_line(
                line,
                source_path=path,
                source_offset=offset,
                fallback_time=stat.st_mtime,
                context_window_hint=state.context_window_tokens,
            )
            if update is None:
                continue
            self._apply_update(state, update, prefer_existing=True)
            if (
                LimitKind.FIVE_HOUR in state.rates
                and LimitKind.WEEKLY in state.rates
                and state.context is not None
                and state.context.window_tokens
                and state.model is not None
            ):
                break

        state.offset = stat.st_size
        state.file_mtime = stat.st_mtime
        state.activity_at = max(state.activity_at, stat.st_mtime)
        return state

    def _tail(self, state: FileState) -> None:
        stat = state.path.stat()
        state.file_mtime = stat.st_mtime
        grew = stat.st_size != state.offset
        state.activity_at = max(state.activity_at, self.clock() if grew else stat.st_mtime)
        if stat.st_size < state.offset:
            LOGGER.info("Rollout file was replaced or truncated: %s", state.path.name)
            fresh = self._bootstrap(state.path)
            self._states[state.path] = fresh
            return
        if stat.st_size == state.offset:
            return

        read_position = state.offset
        buffer = state.pending
        state.pending = b""
        with state.path.open("rb") as handle:
            handle.seek(state.offset)
            while read_position < stat.st_size:
                chunk = handle.read(min(1024 * 1024, stat.st_size - read_position))
                if not chunk:
                    break
                chunk_start = read_position
                read_position += len(chunk)
                data = buffer + chunk
                lines = data.split(b"\n")
                buffer = lines.pop()
                logical_offset = chunk_start - (len(data) - len(chunk))
                for line in lines:
                    clean = line.rstrip(b"\r")
                    update = self.parser.parse_line(
                        clean,
                        source_path=state.path,
                        source_offset=max(0, logical_offset),
                        fallback_time=stat.st_mtime,
                        context_window_hint=state.context_window_tokens,
                    )
                    logical_offset += len(line) + 1
                    if update is not None:
                        self._apply_update(state, update)

        state.offset = read_position
        if len(buffer) <= 8 * 1024 * 1024:
            state.pending = buffer
        else:
            LOGGER.warning("Discarded oversized partial JSONL line in %s", state.path.name)
            state.pending = b""

    def _apply_metadata(self, state: FileState, update: ParsedUpdate) -> None:
        if update.working_directory:
            state.working_directory = update.working_directory
        if update.session_id:
            state.session_id = update.session_id
        if update.context_window_tokens and not state.context_window_tokens:
            state.context_window_tokens = update.context_window_tokens

    def _apply_update(self, state: FileState, update: ParsedUpdate, *, prefer_existing: bool = False) -> None:
        if update.relevant:
            if state.latest_event_at is None or update.event_time > state.latest_event_at:
                state.latest_event_at = update.event_time
            state.activity_at = max(state.activity_at, min(update.event_time, self.clock() + 300.0))

        if update.context_window_tokens and (not prefer_existing or not state.context_window_tokens):
            state.context_window_tokens = update.context_window_tokens
            if state.context and not state.context.window_tokens:
                state.context = replace(state.context, window_tokens=update.context_window_tokens)

        for rate in update.rates:
            current = state.rates.get(rate.kind)
            if current is None or (not prefer_existing and self._rate_key(rate, state.file_mtime) >= self._rate_key(current, state.file_mtime)):
                state.rates[rate.kind] = rate

        if update.context is not None:
            context = update.context
            if not context.window_tokens and state.context_window_tokens:
                context = replace(context, window_tokens=state.context_window_tokens)
            if state.context is None or (not prefer_existing and context.observed_at >= state.context.observed_at):
                state.context = context

        if update.model and (not prefer_existing or state.model is None):
            state.model = update.model
        if update.reasoning_effort and (not prefer_existing or state.reasoning_effort is None):
            state.reasoning_effort = update.reasoning_effort
        if update.plan and (not prefer_existing or state.plan is None):
            state.plan = update.plan
        if update.working_directory and (not prefer_existing or state.working_directory is None):
            state.working_directory = update.working_directory
        if update.session_id and (not prefer_existing or state.session_id is None):
            state.session_id = update.session_id

    def _build_snapshot(self, active_paths: list[Path], now: float) -> UsageSnapshot:
        candidate_states = [self._states[path] for path in active_paths if path in self._states]
        all_states = candidate_states or list(self._states.values())

        for kind in (LimitKind.FIVE_HOUR, LimitKind.WEEKLY):
            candidates: list[tuple[tuple[float, float, int], RateWindow]] = []
            for state in all_states:
                rate = state.rates.get(kind)
                if rate is not None:
                    candidates.append((self._rate_key(rate, state.file_mtime), rate))
            if candidates:
                key, newest = max(candidates, key=lambda item: item[0])
                if key >= self._last_rate_keys.get(kind, (float("-inf"), 0.0, 0)):
                    self._last_rate_keys[kind] = key
                    self._last_rates[kind] = newest

        active_state = max(all_states, key=lambda item: item.activity_at) if all_states else None
        if active_state and active_state.plan:
            self._last_plan = active_state.plan
        elif self._last_plan is None:
            plans = [state for state in all_states if state.plan]
            if plans:
                self._last_plan = max(plans, key=lambda item: item.activity_at).plan

        five_hour = self._last_rates.get(LimitKind.FIVE_HOUR)
        weekly = self._last_rates.get(LimitKind.WEEKLY)
        context = active_state.context if active_state else None
        has_data = bool(five_hour or weekly or context)
        latest_event_at = active_state.latest_event_at if active_state else None
        latest_activity = active_state.activity_at if active_state else None

        rate_times = [rate.observed_at for rate in (five_hour, weekly) if rate is not None]
        primary_expired = bool(
            five_hour is not None
            and five_hour.resets_at is not None
            and five_hour.resets_at <= now
        )
        rate_from_cache = bool(
            (rate_times and now - max(rate_times) > self.stale_after)
            or primary_expired
        )
        if not has_data:
            status = DataStatus.NO_DATA
        elif rate_from_cache:
            status = DataStatus.STALE
        elif latest_activity is not None and now - latest_activity <= self.stale_after:
            status = DataStatus.LIVE
        else:
            status = DataStatus.STALE

        return UsageSnapshot(
            five_hour=five_hour,
            weekly=weekly,
            context=context,
            model=active_state.model if active_state else None,
            reasoning_effort=active_state.reasoning_effort if active_state else None,
            plan=self._last_plan,
            active_session=leaf_name(active_state.working_directory) if active_state else None,
            working_directory=active_state.working_directory if active_state else None,
            latest_file=active_state.path if active_state else None,
            latest_event_at=latest_event_at,
            scanned_at=now,
            status=status,
            rate_from_cache=rate_from_cache,
            error_summary=self._last_error,
        )

    @staticmethod
    def _rate_key(rate: RateWindow, file_mtime: float) -> tuple[float, float, int]:
        return (rate.observed_at, file_mtime, rate.source_offset)

    @staticmethod
    def _read_trailing_partial(path: Path, size: int) -> bytes:
        if size <= 0:
            return b""
        with path.open("rb") as handle:
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return b""
            amount = min(size, 8 * 1024 * 1024)
            handle.seek(size - amount)
            data = handle.read(amount)
        if b"\n" in data:
            return data.rsplit(b"\n", 1)[-1]
        return data if size <= len(data) else b""

    @staticmethod
    def _iter_reverse_complete_lines(path: Path, block_size: int = 256 * 1024) -> Iterator[tuple[int, bytes, int]]:
        """Yield complete JSONL lines newest-first without loading the file."""
        size = path.stat().st_size
        if size <= 0:
            return
        with path.open("rb") as handle:
            handle.seek(size - 1)
            ends_with_newline = handle.read(1) == b"\n"
            position = size
            suffix = b""
            skipped_incomplete_tail = ends_with_newline
            while position > 0:
                start = max(0, position - block_size)
                handle.seek(start)
                chunk = handle.read(position - start)
                data = chunk + suffix
                parts = data.split(b"\n")
                if start > 0:
                    suffix = parts.pop(0)
                    base = start + len(chunk) - len(data) + len(parts[0]) if parts else start
                else:
                    suffix = b""

                offsets: list[int] = []
                cursor = start
                if start > 0:
                    # The first fragment was removed; locate remaining records safely.
                    cursor = start + len(data.split(b"\n", 1)[0]) + 1
                for part in parts:
                    offsets.append(cursor)
                    cursor += len(part) + 1

                for offset, line in reversed(list(zip(offsets, parts))):
                    if not skipped_incomplete_tail:
                        skipped_incomplete_tail = True
                        continue
                    if line:
                        yield max(0, offset), line.rstrip(b"\r"), len(line) + 1
                position = start


class AccountUsageMonitor:
    """Combine live account limits with rollout context and safe cached limits."""

    def __init__(
        self,
        sessions: SessionMonitor,
        *,
        app_server: Optional[CodexAppServerClient] = None,
        rate_store: Optional[RateSnapshotStore] = None,
        account_interval: float = 20.0,
        clock=time.time,
    ) -> None:
        self.sessions = sessions
        self.app_server = app_server or CodexAppServerClient()
        self.rate_store = rate_store or RateSnapshotStore()
        self.account_interval = max(15.0, min(300.0, float(account_interval)))
        self.clock = clock
        self._cached_rates = self.rate_store.load()
        self._account_state: Optional[RateLimitState] = None
        self._account_succeeded = False
        self._next_account_poll = 0.0
        self._account_error: Optional[str] = None

    def set_account_interval(self, seconds: float) -> None:
        self.account_interval = max(15.0, min(300.0, float(seconds)))
        self._next_account_poll = min(self._next_account_poll, self.clock() + self.account_interval)

    def refresh(self, *, force_discovery: bool = False, force_account: bool = False) -> UsageSnapshot:
        now = self.clock()
        if force_account or now >= self._next_account_poll:
            self._next_account_poll = now + self.account_interval
            try:
                state = self.app_server.read_rate_limits()
                self._account_state = state
                self._account_succeeded = True
                self._account_error = None
                LOGGER.debug(
                    "Account rate fetch succeeded: limit=%s 5h=%s weekly=%s",
                    state.limit_id,
                    state.five_hour.used_percent if state.five_hour else None,
                    state.weekly.used_percent if state.weekly else None,
                )
            except Exception as exc:  # Account failures must never block rollout fallback.
                self._account_succeeded = False
                self._account_error = type(exc).__name__
                LOGGER.debug("Account rate fetch unavailable: %s", type(exc).__name__)

        rollout = self.sessions.refresh(force_discovery=force_discovery)
        return self._combine(rollout, self.clock())

    def _combine(self, rollout: UsageSnapshot, now: float) -> UsageSnapshot:
        live = self._account_state if self._account_succeeded else None
        chosen: dict[LimitKind, Optional[RateWindow]] = {}
        fresh_rollout = False
        cache_changed = False

        for kind, rollout_rate, account_rate in (
            (LimitKind.FIVE_HOUR, rollout.five_hour, live.five_hour if live else None),
            (LimitKind.WEEKLY, rollout.weekly, live.weekly if live else None),
        ):
            if account_rate is not None:
                selected = account_rate
            elif rollout_rate is not None and self._rollout_is_recent(rollout_rate, now):
                selected = rollout_rate
                fresh_rollout = True
            else:
                historical = self._cached_rates.get(kind)
                if rollout_rate is not None and (
                    historical is None or rollout_rate.observed_at > historical.observed_at
                ):
                    historical = replace(rollout_rate, source="cache", source_path=None, source_offset=0)
                selected = historical

            chosen[kind] = selected
            if selected is not None and selected.source != "cache":
                previous = self._cached_rates.get(kind)
                self._cached_rates[kind] = replace(
                    selected, source="cache", source_path=None, source_offset=0
                )
                cache_changed = cache_changed or previous != self._cached_rates[kind]

        if cache_changed:
            self.rate_store.save(self._cached_rates)

        has_rates = any(chosen.values())
        sources = {rate.source for rate in chosen.values() if rate is not None}
        source = " + ".join(
            name for name in ("app-server", "rollout", "cache") if name in sources
        ) or None
        if not has_rates:
            status = DataStatus.NO_DATA
        elif live is not None and (live.five_hour is not None or live.weekly is not None):
            status = DataStatus.LIVE
        elif fresh_rollout:
            status = DataStatus.FALLBACK
        else:
            status = DataStatus.STALE

        cached = any(rate is not None and rate.source == "cache" for rate in chosen.values())
        return replace(
            rollout,
            five_hour=chosen[LimitKind.FIVE_HOUR],
            weekly=chosen[LimitKind.WEEKLY],
            plan=live.plan if live and live.plan else rollout.plan,
            status=status,
            rate_from_cache=cached,
            rate_source=source,
            account_refreshed_at=self._account_state.fetched_at if self._account_state else None,
            error_summary=self._account_error or rollout.error_summary,
        )

    def _rollout_is_recent(self, rate: RateWindow, now: float) -> bool:
        age = now - rate.observed_at
        return (
            -300.0 <= age <= self.sessions.stale_after
            and (rate.resets_at is None or rate.resets_at > now)
        )

    def close(self) -> None:
        self.app_server.close()


class MonitorService:
    """Run account and session monitors off the Tk thread, publishing snapshots."""

    def __init__(self, monitor: AccountUsageMonitor, interval: float = 4.0) -> None:
        self.monitor = monitor
        self.interval = max(2.0, min(60.0, interval))
        self.snapshots: queue.Queue[UsageSnapshot] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._refresh = threading.Event()
        self._force_discovery = True
        self._force_account = True
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="codex-session-monitor", daemon=True)
        self._thread.start()

    def request_refresh(self, *, force_discovery: bool = False, force_account: bool = False) -> None:
        if force_discovery:
            self._force_discovery = True
        if force_account:
            self._force_account = True
        self._refresh.set()

    def set_interval(self, seconds: float) -> None:
        self.interval = max(2.0, min(60.0, float(seconds)))
        self._refresh.set()

    def set_account_interval(self, seconds: float) -> None:
        self.monitor.set_account_interval(seconds)
        self._force_account = True
        self._refresh.set()

    def stop(self, timeout: float = 1.5) -> None:
        self._stop.set()
        self._refresh.set()
        self.monitor.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    force_discovery = self._force_discovery
                    force_account = self._force_account
                    self._force_discovery = False
                    self._force_account = False
                    snapshot = self.monitor.refresh(
                        force_discovery=force_discovery,
                        force_account=force_account,
                    )
                    if not self._stop.is_set():
                        self._publish(snapshot)
                except Exception as exc:
                    LOGGER.exception("Monitor cycle failed")
                    if not self._stop.is_set():
                        self._publish(UsageSnapshot.empty(time.time(), type(exc).__name__))
                self._refresh.wait(self.interval)
                self._refresh.clear()
        finally:
            self.monitor.close()

    def _publish(self, snapshot: UsageSnapshot) -> None:
        try:
            self.snapshots.put_nowait(snapshot)
        except queue.Full:
            try:
                self.snapshots.get_nowait()
            except queue.Empty:
                pass
            try:
                self.snapshots.put_nowait(snapshot)
            except queue.Full:
                pass

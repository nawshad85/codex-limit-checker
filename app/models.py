from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class DataStatus(str, Enum):
    LIVE = "LIVE"
    FALLBACK = "FALLBACK"
    STALE = "STALE"
    NO_DATA = "NO DATA"


class LimitKind(str, Enum):
    FIVE_HOUR = "five_hour"
    WEEKLY = "weekly"


@dataclass(frozen=True)
class RateWindow:
    kind: LimitKind
    used_percent: float
    window_minutes: Optional[int]
    resets_at: Optional[float]
    observed_at: float
    source_path: Optional[Path] = None
    source_offset: int = 0
    source: str = "rollout"

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_percent))


@dataclass(frozen=True)
class ContextUsage:
    used_tokens: int
    window_tokens: Optional[int]
    observed_at: float
    estimated: bool = True
    basis: str = "last_token_usage.total_tokens"

    @property
    def used_percent(self) -> Optional[float]:
        if not self.window_tokens or self.window_tokens <= 0:
            return None
        return max(0.0, min(100.0, self.used_tokens * 100.0 / self.window_tokens))

    @property
    def remaining_percent(self) -> Optional[float]:
        used = self.used_percent
        return None if used is None else 100.0 - used


@dataclass(frozen=True)
class ParsedUpdate:
    event_time: float
    rates: tuple[RateWindow, ...] = ()
    context: Optional[ContextUsage] = None
    context_window_tokens: Optional[int] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    plan: Optional[str] = None
    working_directory: Optional[str] = None
    session_id: Optional[str] = None
    relevant: bool = False


@dataclass(frozen=True)
class UsageSnapshot:
    five_hour: Optional[RateWindow]
    weekly: Optional[RateWindow]
    context: Optional[ContextUsage]
    model: Optional[str]
    reasoning_effort: Optional[str]
    plan: Optional[str]
    active_session: Optional[str]
    working_directory: Optional[str]
    latest_file: Optional[Path]
    latest_event_at: Optional[float]
    scanned_at: float
    status: DataStatus
    rate_from_cache: bool = False
    error_summary: Optional[str] = None
    rate_source: Optional[str] = None
    account_refreshed_at: Optional[float] = None

    @classmethod
    def empty(cls, scanned_at: float, error_summary: Optional[str] = None) -> "UsageSnapshot":
        return cls(
            five_hour=None,
            weekly=None,
            context=None,
            model=None,
            reasoning_effort=None,
            plan=None,
            active_session=None,
            working_directory=None,
            latest_file=None,
            latest_event_at=None,
            scanned_at=scanned_at,
            status=DataStatus.NO_DATA,
            error_summary=error_summary,
        )


@dataclass
class FileState:
    path: Path
    offset: int = 0
    pending: bytes = b""
    file_mtime: float = 0.0
    activity_at: float = 0.0
    latest_event_at: Optional[float] = None
    rates: dict[LimitKind, RateWindow] = field(default_factory=dict)
    context: Optional[ContextUsage] = None
    context_window_tokens: Optional[int] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    plan: Optional[str] = None
    working_directory: Optional[str] = None
    session_id: Optional[str] = None

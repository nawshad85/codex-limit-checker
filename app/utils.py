from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def safe_int(value: Any) -> Optional[int]:
    number = safe_float(value)
    if number is None:
        return None
    try:
        return int(number)
    except (ValueError, OverflowError):
        return None


def parse_timestamp(value: Any) -> Optional[float]:
    """Parse Unix seconds/milliseconds or an ISO-8601 timestamp safely."""
    number = safe_float(value)
    if number is not None:
        if abs(number) > 10_000_000_000:
            number /= 1000.0
        try:
            # Validate the platform conversion range without requiring it to be future.
            datetime.fromtimestamp(number, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
        return number

    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


def format_tokens(value: Optional[int]) -> str:
    if value is None:
        return "N/A"
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        scaled = value / 1_000_000
        return f"{scaled:.1f}M" if abs(scaled) < 10 else f"{scaled:.0f}M"
    if magnitude >= 1_000:
        scaled = value / 1_000
        return f"{scaled:.1f}K" if abs(scaled) < 10 else f"{scaled:.0f}K"
    return str(value)


def format_percent(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{int(round(clamp(value, 0.0, 100.0)))}%"


def format_countdown(resets_at: Optional[float], now: Optional[float] = None) -> str:
    if resets_at is None:
        return "N/A"
    current = datetime.now(tz=timezone.utc).timestamp() if now is None else now
    remaining = max(0, int(resets_at - current))
    if remaining <= 0:
        return "now"
    days, rest = divmod(remaining, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{max(1, seconds)}s"


def resolve_codex_home(env: Optional[dict[str, str]] = None) -> Path:
    values = os.environ if env is None else env
    configured = values.get("CODEX_HOME", "").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured)))
    profile = values.get("USERPROFILE", "").strip()
    if profile:
        return Path(profile) / ".codex"
    return Path.home() / ".codex"


def resolve_sessions_dir(env: Optional[dict[str, str]] = None) -> Path:
    return resolve_codex_home(env) / "sessions"


def leaf_name(path_text: Optional[str]) -> Optional[str]:
    if not path_text:
        return None
    normalized = path_text.rstrip("\\/")
    if not normalized:
        return path_text
    return normalized.replace("\\", "/").rsplit("/", 1)[-1]


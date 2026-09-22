from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import ContextUsage, LimitKind, ParsedUpdate, RateWindow
from .utils import clamp, parse_timestamp, safe_float, safe_int


LOGGER = logging.getLogger(__name__)

_SKIP_RECURSION_KEYS = {
    "base_instructions",
    "content",
    "input",
    "instructions",
    "message",
    "messages",
    "output",
    "prompt",
    "summary",
    "text",
}


class RolloutParser:
    """Extract only usage metadata from a Codex JSONL event.

    The parser deliberately does not retain event objects or inspect message text.
    Every JSONL line is independent, so callers can safely skip partial/bad lines.
    """

    def parse_line(
        self,
        line: bytes | str,
        *,
        source_path: Optional[Path] = None,
        source_offset: int = 0,
        fallback_time: Optional[float] = None,
        context_window_hint: Optional[int] = None,
    ) -> Optional[ParsedUpdate]:
        if isinstance(line, bytes):
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError:
                self._log_malformed(source_path, source_offset, "encoding")
                return None
        else:
            text = line
        if not text.strip():
            return None
        try:
            event = json.loads(text)
        except (json.JSONDecodeError, ValueError, TypeError):
            self._log_malformed(source_path, source_offset, "json")
            return None
        if not isinstance(event, dict):
            return None
        return self.parse_event(
            event,
            source_path=source_path,
            source_offset=source_offset,
            fallback_time=fallback_time,
            context_window_hint=context_window_hint,
        )

    def parse_event(
        self,
        event: dict[str, Any],
        *,
        source_path: Optional[Path] = None,
        source_offset: int = 0,
        fallback_time: Optional[float] = None,
        context_window_hint: Optional[int] = None,
    ) -> ParsedUpdate:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        event_time = (
            parse_timestamp(event.get("timestamp"))
            or parse_timestamp(payload.get("timestamp"))
            or fallback_time
            or time.time()
        )

        rates: dict[LimitKind, RateWindow] = {}
        plan: Optional[str] = None
        for rate_limits in self._named_dicts(event, "rate_limits"):
            extracted, extracted_plan = self._parse_rate_limits(
                rate_limits,
                event_time,
                source_path,
                source_offset,
            )
            for rate in extracted:
                rates.setdefault(rate.kind, rate)
            if plan is None:
                plan = extracted_plan
            if len(rates) == 2 and plan is not None:
                break

        context, window_tokens = self._parse_context(
            event,
            event_time=event_time,
            context_window_hint=context_window_hint,
        )

        event_type = self._safe_string(event.get("type"))
        payload_type = self._safe_string(payload.get("type"))

        model = self._first_string(payload, ("model", "model_name", "model_slug"))
        effort = self._first_string(payload, ("effort", "reasoning_effort"))
        thread_settings = payload.get("thread_settings")
        if isinstance(thread_settings, dict):
            model = model or self._first_string(thread_settings, ("model", "model_name", "model_slug"))
            effort = effort or self._first_string(thread_settings, ("effort", "reasoning_effort"))
        world_state = payload.get("state")
        if isinstance(world_state, dict):
            model = model or self._first_string(world_state, ("model", "model_name", "model_slug"))
        if effort is None:
            reasoning = payload.get("reasoning")
            if isinstance(reasoning, dict):
                effort = self._safe_string(reasoning.get("effort"))

        working_directory = self._safe_path_string(payload.get("cwd"))
        if working_directory is None and isinstance(thread_settings, dict):
            working_directory = self._safe_path_string(thread_settings.get("cwd"))
        session_id = self._first_string(payload, ("session_id", "thread_id", "id"))

        relevant_type = event_type in {"session_meta", "turn_context", "token_usage_record"}
        relevant_type = relevant_type or payload_type == "token_count"
        relevant = bool(
            relevant_type
            or rates
            or context
            or model
            or effort
            or working_directory
            or session_id
        )

        return ParsedUpdate(
            event_time=event_time,
            rates=tuple(rates.values()),
            context=context,
            context_window_tokens=window_tokens,
            model=model,
            reasoning_effort=effort,
            plan=plan,
            working_directory=working_directory,
            session_id=session_id,
            relevant=relevant,
        )

    def _parse_rate_limits(
        self,
        data: dict[str, Any],
        event_time: float,
        source_path: Optional[Path],
        source_offset: int,
    ) -> tuple[list[RateWindow], Optional[str]]:
        plan = self._first_string(data, ("plan_type", "plan"))
        if plan and plan.casefold() in {"unknown", "none", "null"}:
            plan = None

        found: dict[LimitKind, RateWindow] = {}
        candidates: list[tuple[str, dict[str, Any]]] = []
        if "used_percent" in data:
            candidates.append(("", data))
        for name, value in data.items():
            if isinstance(value, dict) and "used_percent" in value:
                candidates.append((str(name), value))

        for name, value in candidates:
            used = safe_float(value.get("used_percent"))
            if used is None:
                continue
            window = safe_int(value.get("window_minutes"))
            kind = self._classify_limit(name, window)
            if kind is None or kind in found:
                continue
            reset = parse_timestamp(value.get("resets_at"))
            found[kind] = RateWindow(
                kind=kind,
                used_percent=clamp(used, 0.0, 100.0),
                window_minutes=window if window is None or window > 0 else None,
                resets_at=reset,
                observed_at=event_time,
                source_path=source_path,
                source_offset=source_offset,
            )
        return list(found.values()), plan

    @staticmethod
    def _classify_limit(name: str, window_minutes: Optional[int]) -> Optional[LimitKind]:
        if window_minutes is not None:
            if 240 <= window_minutes <= 360:
                return LimitKind.FIVE_HOUR
            if 9_000 <= window_minutes <= 11_000:
                return LimitKind.WEEKLY
            # "primary" is only a slot name. Older/free plans use it for
            # weekly or other window sizes, so an unknown duration is ignored.
            return None
        lowered = name.casefold().replace("-", "_")
        if lowered in {"primary", "five_hour", "5_hour", "5h"}:
            return LimitKind.FIVE_HOUR
        if lowered in {"secondary", "weekly", "week", "7_day"}:
            return LimitKind.WEEKLY
        return None

    def _parse_context(
        self,
        event: dict[str, Any],
        *,
        event_time: float,
        context_window_hint: Optional[int],
    ) -> tuple[Optional[ContextUsage], Optional[int]]:
        info_candidates = list(self._context_dicts(event))
        window_tokens = context_window_hint
        used_tokens: Optional[int] = None
        basis = ""
        estimated = True

        for info in info_candidates:
            explicit_window = safe_int(
                info.get("model_context_window", info.get("context_window_size"))
            )
            if explicit_window is not None and explicit_window > 0:
                window_tokens = explicit_window

            for key in ("context_used_tokens", "used_context_tokens", "context_tokens"):
                explicit_used = safe_int(info.get(key))
                if explicit_used is not None and explicit_used >= 0:
                    used_tokens = explicit_used
                    basis = key
                    estimated = False
                    break
            if used_tokens is not None:
                break

            last_usage = info.get("last_token_usage")
            if isinstance(last_usage, dict):
                last_total = safe_int(last_usage.get("total_tokens"))
                if last_total is not None and last_total >= 0:
                    used_tokens = last_total
                    basis = "last_token_usage.total_tokens"
                    estimated = False
                    break
                input_tokens = safe_int(last_usage.get("input_tokens"))
                output_tokens = safe_int(last_usage.get("output_tokens"))
                if input_tokens is not None and input_tokens >= 0:
                    used_tokens = input_tokens + max(0, output_tokens or 0)
                    basis = "last_token_usage.input_plus_output"
                    estimated = True
                    break

        # Newer rollouts also emit token_usage_record.payload.usage, which is
        # equivalent to last_token_usage. Never use cumulative thread/total usage.
        if used_tokens is None:
            payload = event.get("payload")
            if isinstance(payload, dict) and event.get("type") == "token_usage_record":
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    usage_total = safe_int(usage.get("total_tokens"))
                    if usage_total is not None and usage_total >= 0:
                        used_tokens = usage_total
                        basis = "usage.total_tokens"
                        estimated = False

        if used_tokens is None:
            return None, window_tokens
        return (
            ContextUsage(
                used_tokens=used_tokens,
                window_tokens=window_tokens,
                observed_at=event_time,
                estimated=estimated,
                basis=basis,
            ),
            window_tokens,
        )

    def _context_dicts(self, event: dict[str, Any]) -> Iterable[dict[str, Any]]:
        payload = event.get("payload")
        if isinstance(payload, dict):
            info = payload.get("info")
            if isinstance(info, dict):
                yield info
        for node in self._structural_dicts(event):
            if any(
                key in node
                for key in (
                    "model_context_window",
                    "context_window_size",
                    "last_token_usage",
                    "context_used_tokens",
                )
            ):
                yield node

    def _named_dicts(self, event: dict[str, Any], wanted: str) -> Iterable[dict[str, Any]]:
        for node in self._structural_dicts(event):
            value = node.get(wanted)
            if isinstance(value, dict):
                yield value

    def _structural_dicts(self, root: dict[str, Any]) -> Iterable[dict[str, Any]]:
        stack: list[tuple[dict[str, Any], int]] = [(root, 0)]
        seen: set[int] = set()
        while stack:
            node, depth = stack.pop()
            marker = id(node)
            if marker in seen:
                continue
            seen.add(marker)
            yield node
            if depth >= 6:
                continue
            for key, value in node.items():
                if str(key).casefold() in _SKIP_RECURSION_KEYS:
                    continue
                if isinstance(value, dict):
                    stack.append((value, depth + 1))
                elif isinstance(value, list) and len(value) <= 32:
                    for item in value:
                        if isinstance(item, dict):
                            stack.append((item, depth + 1))

    @staticmethod
    def _first_string(data: dict[str, Any], keys: tuple[str, ...]) -> Optional[str]:
        for key in keys:
            value = RolloutParser._safe_string(data.get(key))
            if value:
                return value
        return None

    @staticmethod
    def _safe_string(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text or len(text) > 256:
            return None
        return text

    @staticmethod
    def _safe_path_string(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text or len(text) > 32_767:
            return None
        return text

    @staticmethod
    def _log_malformed(path: Optional[Path], offset: int, reason: str) -> None:
        name = path.name if path else "<memory>"
        LOGGER.debug("Skipped malformed JSONL line (%s) in %s at byte %d", reason, name, offset)


def parse_jsonl_lines(
    lines: Iterable[bytes | str],
    *,
    source_path: Optional[Path] = None,
    fallback_time: Optional[float] = None,
) -> list[ParsedUpdate]:
    """Convenience helper used by tests and diagnostics."""
    parser = RolloutParser()
    updates: list[ParsedUpdate] = []
    offset = 0
    for line in lines:
        update = parser.parse_line(
            line,
            source_path=source_path,
            source_offset=offset,
            fallback_time=fallback_time,
        )
        if update is not None:
            updates.append(update)
        offset += len(line.encode("utf-8") if isinstance(line, str) else line)
    return updates

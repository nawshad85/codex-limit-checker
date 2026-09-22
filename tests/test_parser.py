from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.models import LimitKind
from app.parser import RolloutParser, parse_jsonl_lines


_DEFAULT_RATES = object()


def _limit(used: object, minutes: object, reset: object = 2_000_000_000) -> dict[str, object]:
    return {
        "used_percent": used,
        "window_minutes": minutes,
        "resets_at": reset,
    }


def _token_event(
    *,
    timestamp: object = 1_900_000_000,
    primary: object = _DEFAULT_RATES,
    secondary: object = _DEFAULT_RATES,
    rate_limits: object = _DEFAULT_RATES,
    last_usage: object = _DEFAULT_RATES,
    total_tokens: int = 900_000,
    context_window: object = 258_400,
) -> dict[str, object]:
    if last_usage is _DEFAULT_RATES:
        last_usage = {
            "input_tokens": 150_000,
            "cached_input_tokens": 120_000,
            "output_tokens": 7_000,
            "reasoning_output_tokens": 3_000,
            "total_tokens": 157_000,
        }
    info = {
        "total_token_usage": {"total_tokens": total_tokens},
        "last_token_usage": last_usage,
        "model_context_window": context_window,
    }
    if rate_limits is _DEFAULT_RATES:
        if primary is _DEFAULT_RATES:
            primary = _limit(28.0, 300)
        if secondary is _DEFAULT_RATES:
            secondary = _limit(57.0, 10_080, 2_000_500_000)
        rate_limits = {
            "primary": primary,
            "secondary": secondary,
            "plan_type": "plus",
        }
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": info,
            "rate_limits": rate_limits,
            "model": "gpt-synthetic",
            "reasoning_effort": "high",
        },
    }


def _by_kind(update) -> dict[LimitKind, object]:
    return {rate.kind: rate for rate in update.rates}


class RolloutParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = RolloutParser()

    def test_valid_token_count_extracts_limits_context_and_metadata(self) -> None:
        update = self.parser.parse_event(_token_event())
        rates = _by_kind(update)

        self.assertTrue(update.relevant)
        self.assertEqual(set(rates), {LimitKind.FIVE_HOUR, LimitKind.WEEKLY})
        self.assertEqual(rates[LimitKind.FIVE_HOUR].used_percent, 28.0)
        self.assertEqual(rates[LimitKind.FIVE_HOUR].remaining_percent, 72.0)
        self.assertEqual(rates[LimitKind.WEEKLY].window_minutes, 10_080)
        self.assertEqual(update.context.used_tokens, 157_000)
        self.assertEqual(update.context.window_tokens, 258_400)
        self.assertFalse(update.context.estimated)
        self.assertEqual(update.context.basis, "last_token_usage.total_tokens")
        self.assertEqual(update.model, "gpt-synthetic")
        self.assertEqual(update.reasoning_effort, "high")
        self.assertEqual(update.plan, "plus")

    def test_null_rate_limits_does_not_remove_other_token_data(self) -> None:
        update = self.parser.parse_event(_token_event(rate_limits=None))

        self.assertEqual(update.rates, ())
        self.assertIsNone(update.plan)
        self.assertEqual(update.context.used_tokens, 157_000)
        self.assertTrue(update.relevant)

    def test_missing_secondary_produces_only_five_hour_limit(self) -> None:
        update = self.parser.parse_event(
            _token_event(rate_limits={"primary": _limit(33, 300), "plan_type": "plus"})
        )
        rates = _by_kind(update)

        self.assertEqual(set(rates), {LimitKind.FIVE_HOUR})
        self.assertEqual(rates[LimitKind.FIVE_HOUR].used_percent, 33.0)

    def test_malformed_json_line_is_ignored(self) -> None:
        self.assertIsNone(self.parser.parse_line('{"type":"event_msg"'))
        self.assertIsNone(self.parser.parse_line(b"\xff\xfe"))
        self.assertIsNone(self.parser.parse_line("  \r\n"))

    def test_windows_are_classified_by_duration_not_slot_name(self) -> None:
        update = self.parser.parse_event(
            _token_event(
                rate_limits={
                    # Historical Codex logs can put the weekly window in primary.
                    "primary": _limit(44, 10_080),
                    "secondary": _limit(55, 300),
                }
            )
        )
        rates = _by_kind(update)

        self.assertEqual(rates[LimitKind.WEEKLY].used_percent, 44.0)
        self.assertEqual(rates[LimitKind.FIVE_HOUR].used_percent, 55.0)

    def test_unknown_duration_is_not_mislabeled_from_primary_name(self) -> None:
        update = self.parser.parse_event(
            _token_event(rate_limits={"primary": _limit(9, 43_200), "plan_type": "free"})
        )

        self.assertEqual(update.rates, ())
        self.assertEqual(update.plan, "free")

    def test_invalid_event_and_reset_timestamps_are_graceful(self) -> None:
        fallback = 1_234_567_890.0
        event = _token_event(
            timestamp="not-a-timestamp",
            rate_limits={"primary": _limit(25, 300, "also-invalid")},
        )
        update = self.parser.parse_event(event, fallback_time=fallback)
        rate = _by_kind(update)[LimitKind.FIVE_HOUR]

        self.assertEqual(update.event_time, fallback)
        self.assertIsNone(rate.resets_at)

    def test_multiple_token_count_lines_are_parsed_independently(self) -> None:
        first = _token_event(timestamp=1_900_000_001, rate_limits={"primary": _limit(20, 300)})
        second = _token_event(timestamp=1_900_000_002, rate_limits={"primary": _limit(40, 300)})
        lines = [json.dumps(first) + "\n", json.dumps(second) + "\n"]

        updates = parse_jsonl_lines(lines, source_path=Path("synthetic.jsonl"))

        self.assertEqual(len(updates), 2)
        self.assertEqual(_by_kind(updates[0])[LimitKind.FIVE_HOUR].used_percent, 20.0)
        self.assertEqual(_by_kind(updates[1])[LimitKind.FIVE_HOUR].used_percent, 40.0)
        self.assertEqual(
            _by_kind(updates[1])[LimitKind.FIVE_HOUR].source_offset,
            len(lines[0].encode("utf-8")),
        )

    def test_percentage_values_are_clamped(self) -> None:
        update = self.parser.parse_event(
            _token_event(
                rate_limits={
                    "primary": _limit(-12.5, 300),
                    "secondary": _limit(140.25, 10_080),
                }
            )
        )
        rates = _by_kind(update)

        self.assertEqual(rates[LimitKind.FIVE_HOUR].used_percent, 0.0)
        self.assertEqual(rates[LimitKind.FIVE_HOUR].remaining_percent, 100.0)
        self.assertEqual(rates[LimitKind.WEEKLY].used_percent, 100.0)
        self.assertEqual(rates[LimitKind.WEEKLY].remaining_percent, 0.0)

    def test_context_prefers_explicit_last_total_even_after_compaction(self) -> None:
        event = _token_event(
            last_usage={
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 24_708,
            },
            total_tokens=42_765_552,
        )
        update = self.parser.parse_event(event)

        self.assertEqual(update.context.used_tokens, 24_708)
        self.assertFalse(update.context.estimated)

    def test_context_fallback_does_not_double_count_cached_or_reasoning(self) -> None:
        update = self.parser.parse_event(
            _token_event(
                last_usage={
                    "input_tokens": 100_000,
                    "cached_input_tokens": 90_000,
                    "output_tokens": 5_000,
                    "reasoning_output_tokens": 2_000,
                }
            )
        )

        self.assertEqual(update.context.used_tokens, 105_000)
        self.assertTrue(update.context.estimated)
        self.assertEqual(update.context.basis, "last_token_usage.input_plus_output")

    def test_lifetime_total_is_never_used_as_context_occupancy(self) -> None:
        update = self.parser.parse_event(_token_event(last_usage=None, total_tokens=99_000_000))

        self.assertIsNone(update.context)
        self.assertEqual(update.context_window_tokens, 258_400)

    def test_token_usage_record_is_a_context_fallback(self) -> None:
        event = {
            "timestamp": 1_900_000_100,
            "type": "token_usage_record",
            "payload": {
                "usage": {"input_tokens": 40_000, "output_tokens": 2_000, "total_tokens": 42_000},
                "thread_token_usage": {"total_tokens": 2_000_000},
            },
        }
        update = self.parser.parse_event(event, context_window_hint=258_400)

        self.assertEqual(update.context.used_tokens, 42_000)
        self.assertEqual(update.context.window_tokens, 258_400)
        self.assertEqual(update.context.basis, "usage.total_tokens")

    def test_model_effort_and_cwd_from_thread_settings(self) -> None:
        event = {
            "timestamp": 1_900_000_000,
            "type": "event_msg",
            "payload": {
                "type": "thread_settings_applied",
                "thread_settings": {
                    "model": "custom-model-name",
                    "reasoning_effort": "ultra",
                    "cwd": r"C:\Synthetic\Project",
                },
            },
        }
        update = self.parser.parse_event(event)

        self.assertEqual(update.model, "custom-model-name")
        self.assertEqual(update.reasoning_effort, "ultra")
        self.assertEqual(update.working_directory, r"C:\Synthetic\Project")

    def test_message_content_is_not_recursively_inspected(self) -> None:
        event = {
            "timestamp": 1_900_000_000,
            "type": "response_item",
            "payload": {
                "type": "message",
                "content": [
                    {
                        "rate_limits": {
                            "primary": _limit(99, 300),
                            "secondary": _limit(99, 10_080),
                        }
                    }
                ],
            },
        }
        update = self.parser.parse_event(event)

        self.assertEqual(update.rates, ())
        self.assertFalse(update.relevant)

    def test_structural_search_is_bounded_for_large_arrays(self) -> None:
        event = {
            "timestamp": 1_900_000_000,
            "type": "unknown",
            "payload": {
                "items": [
                    {"rate_limits": {"primary": _limit(1, 300)}} for _ in range(33)
                ]
            },
        }
        update = self.parser.parse_event(event)

        self.assertEqual(update.rates, ())
        self.assertFalse(update.relevant)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.settings import AppSettings, SettingsStore
from app.ui import CodexUsageMonitorUI
from app.windows import MonitorWorkArea, WindowRect


class FakeCanvas:
    def __init__(self) -> None:
        self.width = 0
        self.height = 0

    def configure(self, *, width: int, height: int) -> None:
        self.width, self.height = width, height


class FakePositioner:
    def __init__(self, rect: WindowRect, areas: list[MonitorWorkArea]) -> None:
        self.rect = rect
        self.areas = areas
        self.placements: list[WindowRect] = []
        self.shapes: list[bool] = []

    def window_rect(self) -> WindowRect:
        return self.rect

    def work_areas(self) -> list[MonitorWorkArea]:
        return self.areas

    def place(self, x: int, y: int, width: int, height: int) -> None:
        self.rect = WindowRect(x, y, x + width, y + height)
        self.placements.append(self.rect)

    def apply_window_shape(self, *, circular: bool) -> None:
        self.shapes.append(circular)


class FakeSettingsStore:
    def __init__(self) -> None:
        self.saved: list[dict] = []

    def save(self, settings: AppSettings) -> bool:
        self.saved.append(asdict(settings))
        return True


class ViewModeTests(unittest.TestCase):
    def make_ui(
        self,
        *,
        mode: str = "icon",
        x: int = 100,
        y: int = 100,
        areas: list[MonitorWorkArea] | None = None,
    ) -> CodexUsageMonitorUI:
        ui = CodexUsageMonitorUI.__new__(CodexUsageMonitorUI)
        ui.settings = AppSettings(view_mode=mode, x=x, y=y, monitor_name="Primary")
        width, height = ui.size
        ui.canvas = FakeCanvas()
        ui.positioner = FakePositioner(
            WindowRect(x, y, x + width, y + height),
            areas or [MonitorWorkArea(0, 0, 1920, 1040, "Primary", True)],
        )
        ui.settings_store = FakeSettingsStore()
        ui._icon_anchor = None
        ui._press = None
        ui._dragging = False
        ui._render = Mock()
        return ui

    @staticmethod
    def click(ui: CodexUsageMonitorUI) -> None:
        event = SimpleNamespace(x_root=120, y_root=120)
        ui._on_left_press(event)
        ui._on_left_release(event)

    def test_click_cycles_icon_bar_details_and_back(self) -> None:
        ui = self.make_ui()
        for mode, size in (
            ("compact", ui.COMPACT_SIZE),
            ("expanded", ui.EXPANDED_SIZE),
            ("icon", ui.ICON_SIZE),
        ):
            self.click(ui)
            self.assertEqual(ui.settings.view_mode, mode)
            self.assertEqual(ui.size, size)
            self.assertEqual((ui.canvas.width, ui.canvas.height), size)
            self.assertEqual(ui.settings.expanded, mode == "expanded")
        self.assertEqual(len(ui.settings_store.saved), 3)
        self.assertEqual(ui._render.call_count, 3)
        self.assertEqual(ui.positioner.shapes, [False, False, True])

    def test_escape_goes_back_one_view_and_stops_at_icon(self) -> None:
        ui = self.make_ui(mode="expanded")
        for expected in ("compact", "icon", "icon"):
            ui._on_escape(SimpleNamespace())
            self.assertEqual(ui.settings.view_mode, expected)
        self.assertEqual(len(ui.settings_store.saved), 2)

    def test_each_view_survives_settings_save_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            ui = self.make_ui()
            ui.settings_store = store
            for mode in ("compact", "expanded", "icon"):
                with self.subTest(mode=mode):
                    ui.set_view_mode(mode)
                    loaded = store.load()
                    self.assertEqual(loaded.view_mode, mode)
                    self.assertEqual(loaded.expanded, mode == "expanded")
                    self.assertEqual((loaded.x, loaded.y), (100, 100))

    def test_new_and_legacy_settings_have_valid_view_modes(self) -> None:
        self.assertEqual(AppSettings().view_mode, "icon")
        self.assertEqual(AppSettings.from_mapping({"expanded": False}).view_mode, "icon")
        self.assertEqual(AppSettings.from_mapping({"expanded": True}).view_mode, "expanded")
        self.assertEqual(AppSettings.from_mapping({"view_mode": "invalid"}).view_mode, "icon")
        self.assertEqual(AppSettings.from_mapping({"view_mode": "compact", "expanded": True}).view_mode, "compact")

    def test_right_and_bottom_expansion_is_clamped_then_icon_anchor_restored(self) -> None:
        # The work area's bottom excludes the taskbar.
        ui = self.make_ui(x=1872, y=992)
        ui.toggle_expanded()
        self.assertEqual(ui.positioner.window_rect(), WindowRect(1470, 984, 1920, 1040))
        ui.toggle_expanded()
        self.assertEqual(ui.positioner.window_rect(), WindowRect(1470, 706, 1920, 1040))
        ui.toggle_expanded()
        self.assertEqual(ui.positioner.window_rect(), WindowRect(1872, 992, 1920, 1040))
        self.assertEqual((ui.settings.x, ui.settings.y), (1872, 992))

    def test_expansion_uses_original_monitor_at_display_boundary(self) -> None:
        areas = [
            MonitorWorkArea(0, 0, 1920, 1040, "Primary", True),
            MonitorWorkArea(1920, 0, 3840, 1040, "Secondary", False),
        ]
        ui = self.make_ui(x=1860, y=100, areas=areas)
        ui.toggle_expanded()
        self.assertEqual(ui.settings.monitor_name, "Primary")
        self.assertEqual(ui.positioner.window_rect().right, 1920)
        ui.toggle_expanded()
        self.assertEqual(ui.settings.monitor_name, "Primary")
        ui.toggle_expanded()
        self.assertEqual(ui.positioner.window_rect().left, 1860)

    def test_dragging_does_not_change_view(self) -> None:
        ui = self.make_ui()
        ui._on_left_press(SimpleNamespace(x_root=120, y_root=120))
        ui._on_left_motion(SimpleNamespace(x_root=170, y_root=150))
        ui._on_left_release(SimpleNamespace())
        self.assertEqual(ui.settings.view_mode, "icon")
        self.assertEqual(ui.positioner.window_rect(), WindowRect(150, 130, 198, 178))
        self.assertEqual(ui._icon_anchor, (150, 130))

    def test_invalid_or_unchanged_mode_does_not_save_or_resize(self) -> None:
        ui = self.make_ui()
        ui.set_view_mode("unknown")
        ui.set_view_mode("icon")
        self.assertEqual(ui.settings.view_mode, "icon")
        self.assertFalse(ui.settings_store.saved)
        self.assertFalse(ui.positioner.placements)
        ui._render.assert_not_called()


if __name__ == "__main__":
    unittest.main()

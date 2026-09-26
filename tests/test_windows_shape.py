from __future__ import annotations

import ctypes
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from app.windows import WindowsPositioner, WindowRect


class WindowShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.native = SimpleNamespace(
            dwmapi=SimpleNamespace(DwmSetWindowAttribute=Mock(return_value=0)),
            user32=SimpleNamespace(SetWindowRgn=Mock(return_value=1)),
            gdi32=SimpleNamespace(
                CreateEllipticRgn=Mock(return_value=123),
                DeleteObject=Mock(return_value=1),
            ),
        )
        self.positioner = WindowsPositioner(Mock())
        self.positioner.window_rect = Mock(return_value=WindowRect(100, 100, 148, 148))

    def apply_shape(self, circular: bool) -> None:
        with (
            patch("app.windows.IS_WINDOWS", True),
            patch.object(WindowsPositioner, "hwnd", new_callable=PropertyMock, return_value=456),
            patch.object(ctypes, "windll", self.native, create=True),
        ):
            self.positioner.apply_window_shape(circular=circular)

    def test_icon_region_ownership_transfers_to_windows(self) -> None:
        self.apply_shape(True)
        self.native.gdi32.CreateEllipticRgn.assert_called_once_with(0, 0, 48, 48)
        self.native.user32.SetWindowRgn.assert_called_once_with(456, 123, True)
        self.native.gdi32.DeleteObject.assert_not_called()

    def test_expansion_removes_region_without_allocating_another(self) -> None:
        self.apply_shape(False)
        self.native.user32.SetWindowRgn.assert_called_once_with(456, None, True)
        self.native.gdi32.CreateEllipticRgn.assert_not_called()

    def test_failed_transfer_frees_region(self) -> None:
        self.native.user32.SetWindowRgn.return_value = 0
        self.apply_shape(True)
        self.native.gdi32.DeleteObject.assert_called_once_with(123)

    def test_region_allocation_failure_is_safe(self) -> None:
        self.native.gdi32.CreateEllipticRgn.return_value = None
        self.apply_shape(True)
        self.native.user32.SetWindowRgn.assert_not_called()
        self.native.gdi32.DeleteObject.assert_not_called()

    def test_unavailable_dwm_does_not_prevent_circle(self) -> None:
        self.native.dwmapi.DwmSetWindowAttribute.side_effect = OSError("unavailable")
        self.apply_shape(True)
        self.native.user32.SetWindowRgn.assert_called_once_with(456, 123, True)


@unittest.skipUnless(
    os.name == "nt" and os.environ.get("CODEX_MONITOR_UI_TESTS") == "1",
    "Opt-in Windows desktop test: set CODEX_MONITOR_UI_TESTS=1",
)
class NativeWindowShapeTests(unittest.TestCase):
    def test_real_tk_mode_switches_restore_native_region(self) -> None:
        import tkinter as tk
        from ctypes import wintypes
        from pathlib import Path

        from app.settings import AppSettings
        from app.ui import CodexUsageMonitorUI
        from app.windows import configure_dpi_awareness

        configure_dpi_awareness()
        root = tk.Tk()
        ui = CodexUsageMonitorUI(
            root=root,
            settings=AppSettings(view_mode="icon", x=120, y=120),
            settings_store=Mock(),
            monitor_service=Mock(),
            sessions_dir=Path("nonexistent-test-sessions"),
        )
        gdi = ctypes.windll.gdi32
        user = ctypes.windll.user32
        gdi.CreateRectRgn.argtypes = [ctypes.c_int] * 4
        gdi.CreateRectRgn.restype = wintypes.HRGN
        gdi.PtInRegion.argtypes = [wintypes.HRGN, ctypes.c_int, ctypes.c_int]
        gdi.PtInRegion.restype = wintypes.BOOL
        gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        user.GetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN]
        user.GetWindowRgn.restype = ctypes.c_int
        region = gdi.CreateRectRgn(0, 0, 0, 0)
        self.assertTrue(region)
        try:
            ui.start()
            root.update_idletasks()
            # Repeated switches expose stale circular clips and Tk resize races.
            for mode in ("icon", "compact", "expanded", "icon", "compact", "icon"):
                with self.subTest(mode=mode):
                    ui.set_view_mode(mode)
                    root.update_idletasks()
                    rect = ui.positioner.window_rect()
                    self.assertEqual((rect.width, rect.height), ui.size)
                    kind = user.GetWindowRgn(ui.positioner.hwnd, region)
                    if mode == "icon":
                        self.assertNotEqual(kind, 0)
                        self.assertTrue(gdi.PtInRegion(region, 24, 24))
                        for x, y in ((0, 0), (47, 0), (0, 47), (47, 47)):
                            self.assertFalse(gdi.PtInRegion(region, x, y))
                    else:
                        self.assertEqual(kind, 0, "Expanded views must not retain the icon clip")
        finally:
            gdi.DeleteObject(region)
            ui.close()


if __name__ == "__main__":
    unittest.main()

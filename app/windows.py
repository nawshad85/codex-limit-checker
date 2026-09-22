from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, TYPE_CHECKING


if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers.
    import tkinter as tk


LOGGER = logging.getLogger(__name__)
IS_WINDOWS = os.name == "nt"


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2


@dataclass(frozen=True)
class MonitorWorkArea(WindowRect):
    name: str = "Primary"
    primary: bool = False


def configure_dpi_awareness() -> None:
    """Enable per-monitor DPI awareness before Tk creates a top-level window."""
    if not IS_WINDOWS:
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2.  The negative constant is
        # a special HANDLE, hence c_void_p rather than a normal integer arg.
        context = ctypes.c_void_p(-4 & ((1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1))
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(context):
            return
    except (AttributeError, OSError, ctypes.ArgumentError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError, ctypes.ArgumentError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError, ctypes.ArgumentError):
        LOGGER.debug("Windows DPI-awareness API is unavailable")


def enumerate_work_areas(root: Optional["tk.Misc"] = None) -> list[MonitorWorkArea]:
    """Return taskbar-aware work areas, including monitors with negative origins."""
    if IS_WINDOWS:
        try:
            return _enumerate_windows_work_areas()
        except (AttributeError, OSError, ctypes.ArgumentError):
            LOGGER.debug("Monitor enumeration failed", exc_info=True)
    if root is not None:
        try:
            return [
                MonitorWorkArea(
                    0,
                    0,
                    int(root.winfo_screenwidth()),
                    int(root.winfo_screenheight()),
                    "Primary",
                    True,
                )
            ]
        except Exception:  # TclError is deliberately avoided at module import.
            pass
    return [MonitorWorkArea(0, 0, 1920, 1080, "Primary", True)]


def _enumerate_windows_work_areas() -> list[MonitorWorkArea]:
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )
    results: list[MonitorWorkArea] = []

    def collect(monitor, _device_context, _rect, _data) -> bool:
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            work = info.rcWork
            results.append(
                MonitorWorkArea(
                    int(work.left),
                    int(work.top),
                    int(work.right),
                    int(work.bottom),
                    str(info.szDevice),
                    bool(info.dwFlags & 1),
                )
            )
        return True

    callback = callback_type(collect)
    ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0)
    if not results:
        raise OSError("EnumDisplayMonitors returned no displays")
    return results


def monitor_for_rect(
    rect: WindowRect,
    monitors: Iterable[MonitorWorkArea],
    preferred_name: Optional[str] = None,
) -> MonitorWorkArea:
    choices = list(monitors)
    if not choices:
        return MonitorWorkArea(0, 0, 1920, 1080, "Primary", True)

    def overlap(area: MonitorWorkArea) -> int:
        width = max(0, min(rect.right, area.right) - max(rect.left, area.left))
        height = max(0, min(rect.bottom, area.bottom) - max(rect.top, area.top))
        return width * height

    scored = [(overlap(area), area) for area in choices]
    best_score, best = max(scored, key=lambda item: item[0])
    if best_score:
        return best
    if preferred_name:
        preferred = next((area for area in choices if area.name == preferred_name), None)
        if preferred is not None:
            return preferred
    return next((area for area in choices if area.primary), choices[0])


def clamp_to_work_area(
    x: int,
    y: int,
    width: int,
    height: int,
    area: MonitorWorkArea,
) -> tuple[int, int]:
    max_x = max(area.left, area.right - width)
    max_y = max(area.top, area.bottom - height)
    return (
        min(max(int(x), area.left), max_x),
        min(max(int(y), area.top), max_y),
    )


def preset_position(
    preset: str,
    width: int,
    height: int,
    area: MonitorWorkArea,
    margin: int = 12,
) -> tuple[int, int]:
    usable_left = area.left + margin
    usable_top = area.top + margin
    usable_right = area.right - margin
    usable_bottom = area.bottom - margin
    if preset.endswith("_left"):
        x = usable_left
    elif preset.endswith("_center"):
        x = area.left + (area.width - width) // 2
    else:
        x = usable_right - width
    if preset.startswith("bottom_"):
        y = usable_bottom - height
    else:
        y = usable_top
    return clamp_to_work_area(x, y, width, height, area)


class WindowsPositioner:
    """Native positioning avoids Tk's negative-coordinate geometry semantics."""

    def __init__(self, root: "tk.Misc") -> None:
        self.root = root

    @property
    def hwnd(self) -> int:
        try:
            widget_handle = int(self.root.winfo_id())
        except Exception:
            return 0
        if IS_WINDOWS and widget_handle:
            try:
                from ctypes import wintypes

                get_parent = ctypes.windll.user32.GetParent
                get_parent.argtypes = [wintypes.HWND]
                get_parent.restype = wintypes.HWND
                # Tk exposes the client child HWND from winfo_id().  Window
                # manager operations must target its wrapper/top-level HWND.
                wrapper = get_parent(widget_handle)
                if wrapper:
                    return int(wrapper)
            except (AttributeError, OSError, ctypes.ArgumentError):
                pass
        return widget_handle

    def work_areas(self) -> list[MonitorWorkArea]:
        return enumerate_work_areas(self.root)

    def window_rect(self) -> WindowRect:
        if IS_WINDOWS and self.hwnd:
            try:
                from ctypes import wintypes

                native = wintypes.RECT()
                if ctypes.windll.user32.GetWindowRect(self.hwnd, ctypes.byref(native)):
                    return WindowRect(native.left, native.top, native.right, native.bottom)
            except (AttributeError, OSError, ctypes.ArgumentError):
                pass
        try:
            left = int(self.root.winfo_x())
            top = int(self.root.winfo_y())
            width = int(self.root.winfo_width())
            height = int(self.root.winfo_height())
        except Exception:
            return WindowRect(0, 0, 1, 1)
        return WindowRect(left, top, left + width, top + height)

    def place(self, x: int, y: int, width: int, height: int) -> None:
        if IS_WINDOWS and self.hwnd:
            try:
                # SWP_NOZORDER | SWP_NOACTIVATE: moving the status widget must
                # neither steal focus nor disturb its chosen topmost state.
                ctypes.windll.user32.SetWindowPos(
                    self.hwnd,
                    None,
                    int(x),
                    int(y),
                    int(width),
                    int(height),
                    0x0004 | 0x0010,
                )
                return
            except (AttributeError, OSError, ctypes.ArgumentError):
                pass
        try:
            self.root.geometry(f"{int(width)}x{int(height)}{int(x):+d}{int(y):+d}")
        except Exception:
            LOGGER.debug("Tk window positioning failed", exc_info=True)

    def apply_rounded_corners(self) -> None:
        if not IS_WINDOWS or not self.hwnd:
            return
        try:
            value = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                self.hwnd, 33, ctypes.byref(value), ctypes.sizeof(value)
            )
            dark = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                self.hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark)
            )
        except (AttributeError, OSError, ctypes.ArgumentError):
            LOGGER.debug("DWM window attributes are unavailable")


class StartupManager:
    REGISTRY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

    def __init__(
        self,
        app_name: str = "CodexUsageMonitor",
        main_script: Optional[Path] = None,
    ) -> None:
        self.app_name = app_name
        self.main_script = Path(main_script).resolve() if main_script else None

    def expected_command(self) -> str:
        if getattr(sys, "frozen", False):
            arguments = [str(Path(sys.executable).resolve())]
        else:
            interpreter = Path(sys.executable).resolve()
            if interpreter.name.lower() in {"python.exe", "python3.exe"}:
                pythonw = interpreter.with_name("pythonw.exe")
                if pythonw.is_file():
                    interpreter = pythonw
            script = self.main_script or Path(sys.argv[0]).resolve()
            arguments = [str(interpreter), str(script)]
        return subprocess.list2cmdline(arguments)

    def registered_command(self) -> Optional[str]:
        if not IS_WINDOWS:
            return None
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.REGISTRY_PATH) as key:
                value, value_type = winreg.QueryValueEx(key, self.app_name)
            return str(value) if value_type in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) else None
        except (FileNotFoundError, OSError):
            return None

    def is_enabled(self) -> bool:
        registered = self.registered_command()
        if not registered:
            return False
        return registered.strip().casefold() == self.expected_command().strip().casefold()

    def set_enabled(self, enabled: bool) -> bool:
        if not IS_WINDOWS:
            return False
        try:
            import winreg

            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                self.REGISTRY_PATH,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                if enabled:
                    winreg.SetValueEx(
                        key,
                        self.app_name,
                        0,
                        winreg.REG_SZ,
                        self.expected_command(),
                    )
                else:
                    try:
                        winreg.DeleteValue(key, self.app_name)
                    except FileNotFoundError:
                        pass
            return self.is_enabled() if enabled else self.registered_command() is None
        except OSError as exc:
            LOGGER.warning("Startup registration could not be changed: %s", type(exc).__name__)
            return False


def open_path(path: Path) -> bool:
    """Open an existing local path without invoking a command shell."""
    target = Path(path)
    if not target.exists():
        return False
    try:
        if IS_WINDOWS:
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)], close_fds=True)
        else:
            subprocess.Popen(["xdg-open", str(target)], close_fds=True)
        return True
    except OSError as exc:
        LOGGER.warning("Could not open %s: %s", target.name, type(exc).__name__)
        return False

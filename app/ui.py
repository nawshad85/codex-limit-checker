from __future__ import annotations

import logging
import math
import queue
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import DataStatus, UsageSnapshot
from .monitor import MonitorService
from .notifications import NotificationManager
from .settings import AppSettings, SettingsStore
from .utils import clamp, format_countdown, format_percent, format_tokens
from .windows import (
    MonitorWorkArea,
    StartupManager,
    WindowRect,
    WindowsPositioner,
    clamp_to_work_area,
    monitor_for_rect,
    open_path,
    preset_position,
)


LOGGER = logging.getLogger(__name__)


class Palette:
    TRANSPARENT = "#010203"
    SHADOW = "#0B0D10"
    BACKGROUND = "#171A1F"
    SURFACE = "#1E2229"
    BORDER = "#303640"
    DIVIDER = "#2A3038"
    TRACK = "#303640"
    TEXT = "#F0F3F6"
    MUTED = "#98A2B3"
    DIM = "#707986"
    GREEN = "#69B58E"
    YELLOW = "#C8A957"
    ORANGE = "#D18755"
    RED = "#C96868"


class CodexUsageMonitorUI:
    COMPACT_SIZE = (450, 56)
    EXPANDED_SIZE = (450, 334)
    POSITION_PRESETS = (
        ("Top Left", "top_left"),
        ("Top Center", "top_center"),
        ("Top Right", "top_right"),
        ("Bottom Left", "bottom_left"),
        ("Bottom Center", "bottom_center"),
        ("Bottom Right", "bottom_right"),
    )

    def __init__(
        self,
        *,
        root: tk.Tk,
        settings: AppSettings,
        settings_store: SettingsStore,
        monitor_service: MonitorService,
        sessions_dir: Path,
    ) -> None:
        self.root = root
        self.settings = settings
        self.settings_store = settings_store
        self.monitor_service = monitor_service
        self.sessions_dir = Path(sessions_dir)
        self.positioner = WindowsPositioner(root)
        self.startup = StartupManager(main_script=Path(__file__).resolve().parent.parent / "main.py")
        self.notifications = NotificationManager(settings, settings_store)

        self.snapshot = UsageSnapshot.empty(time.time())
        self._started = False
        self._closing = False
        self._placed = False
        self._press: Optional[tuple[int, int, WindowRect]] = None
        self._dragging = False
        self._after_ids: set[str] = set()
        self._poll_after: Optional[str] = None
        self._tick_after: Optional[str] = None
        self._animation_after: Optional[str] = None
        self._requested_resets: set[str] = set()
        self._bar_display: dict[str, Optional[float]] = {
            "five_hour": None,
            "weekly": None,
            "context": None,
        }
        self._animation_from: dict[str, Optional[float]] = dict(self._bar_display)
        self._bar_targets: dict[str, Optional[float]] = dict(self._bar_display)
        self._animation_started = 0.0

        self._font_family = self._select_font_family()
        self._configure_window()
        self._logo_image = self._load_logo_image()
        self._build_context_menu()
        self._bind_events()
        self._render()

    def _select_font_family(self) -> str:
        try:
            families = {name.casefold(): name for name in tkfont.families(self.root)}
            for candidate in ("Segoe UI Variable Text", "Segoe UI"):
                if candidate.casefold() in families:
                    return families[candidate.casefold()]
        except tk.TclError:
            pass
        return "TkDefaultFont"

    def _font(self, size: int, weight: str = "normal") -> tuple[str, int, str]:
        return self._font_family, size, weight

    def _load_logo_image(self) -> Optional[tk.PhotoImage]:
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
        logo_path = bundle_root / "assets" / "openai-white-monoblossom.png"
        try:
            return tk.PhotoImage(master=self.root, file=str(logo_path))
        except (OSError, tk.TclError):
            LOGGER.warning("Widget logo could not be loaded")
            return None

    @property
    def size(self) -> tuple[int, int]:
        return self.EXPANDED_SIZE if self.settings.expanded else self.COMPACT_SIZE

    def _configure_window(self) -> None:
        width, height = self.size
        self.root.withdraw()
        self.root.title("Codex Usage Monitor")
        self.root.overrideredirect(True)
        self.root.configure(background=Palette.TRANSPARENT)
        self.root.geometry(f"{width}x{height}+0+0")
        try:
            self.root.attributes("-alpha", self.settings.opacity)
            self.root.attributes("-topmost", self.settings.always_on_top)
            self.root.wm_attributes("-transparentcolor", Palette.TRANSPARENT)
        except tk.TclError:
            LOGGER.debug("One or more Windows window attributes are unavailable")

        self.canvas = tk.Canvas(
            self.root,
            width=width,
            height=height,
            background=Palette.TRANSPARENT,
            highlightthickness=0,
            borderwidth=0,
            relief="flat",
            takefocus=False,
        )
        self.canvas.pack(fill="both", expand=True)
        self.root.update_idletasks()
        self.positioner.apply_rounded_corners()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _bind_events(self) -> None:
        self.canvas.bind("<ButtonPress-1>", self._on_left_press)
        self.canvas.bind("<B1-Motion>", self._on_left_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_left_release)
        self.canvas.bind("<Button-3>", self._show_context_menu)
        self.root.bind("<Escape>", self._on_escape)

    def _build_context_menu(self) -> None:
        menu_options = {
            "tearoff": False,
            "background": Palette.SURFACE,
            "foreground": Palette.TEXT,
            "activebackground": Palette.BORDER,
            "activeforeground": Palette.TEXT,
            "disabledforeground": Palette.DIM,
            "borderwidth": 1,
            "relief": "solid",
            "font": self._font(9),
        }
        self.context_menu = tk.Menu(self.root, **menu_options)
        self.context_menu.add_command(label="Expand", command=self.toggle_expanded)
        self.context_menu.add_command(label="Refresh Now", command=self.refresh_now)
        self.context_menu.add_separator()

        self._topmost_var = tk.BooleanVar(value=self.settings.always_on_top)
        self._startup_var = tk.BooleanVar(value=False)
        self.context_menu.add_checkbutton(
            label="Always on Top",
            variable=self._topmost_var,
            command=self._toggle_topmost,
        )
        self.context_menu.add_checkbutton(
            label="Launch at Startup",
            variable=self._startup_var,
            command=self._toggle_startup,
        )

        self._opacity_var = tk.IntVar(value=int(round(self.settings.opacity * 100)))
        opacity_menu = tk.Menu(self.context_menu, **menu_options)
        for percent in (50, 60, 70, 80, 90, 95, 100):
            opacity_menu.add_radiobutton(
                label=f"{percent}%",
                value=percent,
                variable=self._opacity_var,
                command=lambda value=percent: self.set_opacity(value / 100.0),
            )
        self.context_menu.add_cascade(label="Opacity", menu=opacity_menu)

        position_menu = tk.Menu(self.context_menu, **menu_options)
        for label, preset in self.POSITION_PRESETS:
            position_menu.add_command(
                label=label,
                command=lambda value=preset: self.apply_position_preset(value),
            )
        self.context_menu.add_cascade(label="Position", menu=position_menu)
        self.context_menu.add_separator()
        self._folder_menu_index = self.context_menu.index("end") + 1
        self.context_menu.add_command(
            label="Open Codex Sessions Folder",
            command=self._open_sessions_folder,
        )
        self._file_menu_index = self.context_menu.index("end") + 1
        self.context_menu.add_command(
            label="Open Latest Session File",
            command=self._open_latest_file,
        )
        self.context_menu.add_command(label="Settings", command=self.open_settings)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Exit", command=self.close)

    def run(self) -> None:
        self.start()
        try:
            self.root.mainloop()
        finally:
            if not self._closing:
                self.close()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._initial_position()
        self.root.deiconify()
        try:
            self.root.lift()
        except tk.TclError:
            pass
        self.monitor_service.start()
        self._schedule_poll(75)
        self._schedule_tick(1000)

    def _initial_position(self) -> None:
        width, height = self.size
        monitors = self.positioner.work_areas()
        preferred = next(
            (area for area in monitors if area.name == self.settings.monitor_name),
            None,
        )
        area = preferred or next((area for area in monitors if area.primary), monitors[0])
        if self.settings.position_preset:
            x, y = preset_position(self.settings.position_preset, width, height, area)
        elif self.settings.x is not None and self.settings.y is not None:
            proposed = WindowRect(
                self.settings.x,
                self.settings.y,
                self.settings.x + width,
                self.settings.y + height,
            )
            area = monitor_for_rect(proposed, monitors, self.settings.monitor_name)
            x, y = clamp_to_work_area(self.settings.x, self.settings.y, width, height, area)
        else:
            x, y = preset_position("top_right", width, height, area)
        self.positioner.place(x, y, width, height)
        self.settings.x, self.settings.y = x, y
        self.settings.monitor_name = area.name
        self._placed = True

    def _schedule_poll(self, delay_ms: int) -> None:
        if self._closing:
            return
        self._poll_after = self.root.after(delay_ms, self._poll_snapshots)

    def _schedule_tick(self, delay_ms: int) -> None:
        if self._closing:
            return
        self._tick_after = self.root.after(delay_ms, self._tick)

    def _poll_snapshots(self) -> None:
        self._poll_after = None
        newest: Optional[UsageSnapshot] = None
        while True:
            try:
                newest = self.monitor_service.snapshots.get_nowait()
            except queue.Empty:
                break
        if newest is not None:
            self.snapshot = newest
            self.notifications.consider(newest)
            self._begin_bar_animation(newest)
            self._prune_reset_requests(newest)
        self._schedule_poll(150)

    def _tick(self) -> None:
        self._tick_after = None
        now = time.time()
        due = False
        for name, rate in (("five_hour", self.snapshot.five_hour), ("weekly", self.snapshot.weekly)):
            if rate is None or rate.resets_at is None or rate.resets_at > now:
                continue
            key = f"{name}:{int(round(rate.resets_at))}"
            if key not in self._requested_resets:
                self._requested_resets.add(key)
                due = True
        if due:
            self.monitor_service.request_refresh(force_discovery=True)
        self._render()
        self._schedule_tick(1000)

    def _prune_reset_requests(self, snapshot: UsageSnapshot) -> None:
        current = {
            f"{name}:{int(round(rate.resets_at))}"
            for name, rate in (("five_hour", snapshot.five_hour), ("weekly", snapshot.weekly))
            if rate is not None and rate.resets_at is not None
        }
        self._requested_resets.intersection_update(current)

    def refresh_now(self) -> None:
        self.monitor_service.request_refresh(force_discovery=True)

    def _begin_bar_animation(self, snapshot: UsageSnapshot) -> None:
        targets = {
            "five_hour": snapshot.five_hour.remaining_percent if snapshot.five_hour else None,
            "weekly": snapshot.weekly.remaining_percent if snapshot.weekly else None,
            "context": snapshot.context.used_percent if snapshot.context else None,
        }
        if self._animation_after is not None:
            try:
                self.root.after_cancel(self._animation_after)
            except tk.TclError:
                pass
            self._animation_after = None
        self._animation_from = {
            key: (self._bar_display[key] if self._bar_display[key] is not None else 0.0)
            for key in self._bar_display
        }
        self._bar_targets = targets
        self._animation_started = time.monotonic()
        self._animate_bars()

    def _animate_bars(self) -> None:
        self._animation_after = None
        progress = clamp((time.monotonic() - self._animation_started) / 0.20, 0.0, 1.0)
        eased = 1.0 - (1.0 - progress) ** 3
        for key, target in self._bar_targets.items():
            if target is None:
                self._bar_display[key] = None
            else:
                origin = self._animation_from.get(key) or 0.0
                self._bar_display[key] = origin + (target - origin) * eased
        self._render()
        if progress < 1.0 and not self._closing:
            self._animation_after = self.root.after(16, self._animate_bars)

    def _on_left_press(self, event: tk.Event) -> None:
        self._press = (int(event.x_root), int(event.y_root), self.positioner.window_rect())
        self._dragging = False

    def _on_left_motion(self, event: tk.Event) -> None:
        if self._press is None:
            return
        start_x, start_y, rect = self._press
        dx = int(event.x_root) - start_x
        dy = int(event.y_root) - start_y
        if not self._dragging and math.hypot(dx, dy) < 5.0:
            return
        self._dragging = True
        width, height = self.size
        self.positioner.place(rect.left + dx, rect.top + dy, width, height)

    def _on_left_release(self, _event: tk.Event) -> None:
        if self._press is None:
            return
        dragged = self._dragging
        self._press = None
        self._dragging = False
        if dragged:
            self.settings.position_preset = None
            self._clamp_and_remember_position(save=True)
        else:
            self.toggle_expanded()

    def _on_escape(self, _event: tk.Event) -> None:
        if self.settings.expanded:
            self.toggle_expanded()

    def _show_context_menu(self, event: tk.Event) -> None:
        try:
            self.context_menu.entryconfigure(
                0, label="Collapse" if self.settings.expanded else "Expand"
            )
            self._topmost_var.set(self.settings.always_on_top)
            self._startup_var.set(self.startup.is_enabled())
            self._opacity_var.set(int(round(self.settings.opacity * 100)))
            self.context_menu.entryconfigure(
                self._folder_menu_index,
                state="normal" if self.sessions_dir.is_dir() else "disabled",
            )
            latest = self.snapshot.latest_file
            self.context_menu.entryconfigure(
                self._file_menu_index,
                state="normal" if latest is not None and latest.is_file() else "disabled",
            )
            self.context_menu.tk_popup(int(event.x_root), int(event.y_root))
        finally:
            try:
                self.context_menu.grab_release()
            except tk.TclError:
                pass

    def toggle_expanded(self) -> None:
        self.settings.expanded = not self.settings.expanded
        width, height = self.size
        self.canvas.configure(width=width, height=height)
        if self.settings.position_preset:
            self.apply_position_preset(self.settings.position_preset, save=False)
        else:
            rect = self.positioner.window_rect()
            monitors = self.positioner.work_areas()
            proposed = WindowRect(rect.left, rect.top, rect.left + width, rect.top + height)
            area = monitor_for_rect(proposed, monitors, self.settings.monitor_name)
            x, y = clamp_to_work_area(rect.left, rect.top, width, height, area)
            self.positioner.place(x, y, width, height)
            self.settings.x, self.settings.y = x, y
            self.settings.monitor_name = area.name
        self.settings_store.save(self.settings)
        self._render()

    def apply_position_preset(self, preset: str, *, save: bool = True) -> None:
        allowed = {value for _label, value in self.POSITION_PRESETS}
        if preset not in allowed:
            return
        width, height = self.size
        monitors = self.positioner.work_areas()
        rect = self.positioner.window_rect()
        area = monitor_for_rect(rect, monitors, self.settings.monitor_name)
        x, y = preset_position(preset, width, height, area)
        self.positioner.place(x, y, width, height)
        self.settings.position_preset = preset
        self.settings.x, self.settings.y = x, y
        self.settings.monitor_name = area.name
        if save:
            self.settings_store.save(self.settings)

    def _clamp_and_remember_position(self, *, save: bool) -> None:
        rect = self.positioner.window_rect()
        width, height = self.size
        monitors = self.positioner.work_areas()
        proposed = WindowRect(rect.left, rect.top, rect.left + width, rect.top + height)
        area = monitor_for_rect(proposed, monitors, self.settings.monitor_name)
        x, y = clamp_to_work_area(rect.left, rect.top, width, height, area)
        if (x, y) != (rect.left, rect.top):
            self.positioner.place(x, y, width, height)
        self.settings.x, self.settings.y = x, y
        self.settings.monitor_name = area.name
        if save:
            self.settings_store.save(self.settings)

    def set_opacity(self, opacity: float) -> None:
        value = float(clamp(opacity, 0.50, 1.00))
        self.settings.opacity = value
        self._opacity_var.set(int(round(value * 100)))
        try:
            self.root.attributes("-alpha", value)
        except tk.TclError:
            pass
        self.settings_store.save(self.settings)

    def _toggle_topmost(self) -> None:
        self.settings.always_on_top = bool(self._topmost_var.get())
        try:
            self.root.attributes("-topmost", self.settings.always_on_top)
        except tk.TclError:
            pass
        self.settings_store.save(self.settings)

    def _toggle_startup(self) -> None:
        requested = bool(self._startup_var.get())
        succeeded = self.startup.set_enabled(requested)
        actual = self.startup.is_enabled()
        self._startup_var.set(actual)
        if not succeeded and requested != actual:
            try:
                self.root.bell()
            except tk.TclError:
                pass

    def _open_sessions_folder(self) -> None:
        if not open_path(self.sessions_dir):
            self._safe_bell()

    def _open_latest_file(self) -> None:
        latest = self.snapshot.latest_file
        if latest is None or not open_path(latest):
            self._safe_bell()

    def _safe_bell(self) -> None:
        try:
            self.root.bell()
        except tk.TclError:
            pass

    def open_settings(self) -> None:
        existing = getattr(self, "_settings_window", None)
        if existing is not None:
            try:
                existing.deiconify()
                existing.lift()
                return
            except tk.TclError:
                self._settings_window = None

        dialog = tk.Toplevel(self.root)
        self._settings_window = dialog
        dialog.title("Codex Usage Monitor Settings")
        dialog.configure(background=Palette.BACKGROUND)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        try:
            dialog.attributes("-topmost", self.settings.always_on_top)
        except tk.TclError:
            pass

        refresh_var = tk.DoubleVar(value=self.settings.refresh_interval)
        opacity_var = tk.IntVar(value=int(round(self.settings.opacity * 100)))
        notifications_var = tk.BooleanVar(value=self.settings.notifications_enabled)
        threshold_vars = {
            value: tk.BooleanVar(value=value in self.settings.notification_thresholds)
            for value in (20, 10, 5)
        }

        content = tk.Frame(dialog, background=Palette.BACKGROUND, padx=20, pady=18)
        content.pack(fill="both", expand=True)
        tk.Label(
            content,
            text="SETTINGS",
            background=Palette.BACKGROUND,
            foreground=Palette.TEXT,
            font=self._font(12, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 16))
        self._settings_label(content, "Refresh interval").grid(row=1, column=0, sticky="w", pady=6)
        refresh = tk.Spinbox(
            content,
            from_=2,
            to=60,
            increment=1,
            width=7,
            textvariable=refresh_var,
            background=Palette.SURFACE,
            foreground=Palette.TEXT,
            insertbackground=Palette.TEXT,
            buttonbackground=Palette.BORDER,
            relief="flat",
            font=self._font(9),
        )
        refresh.grid(row=1, column=1, sticky="w", padx=(18, 4), pady=6)
        self._settings_label(content, "seconds", muted=True).grid(row=1, column=2, sticky="w")

        self._settings_label(content, "Opacity").grid(row=2, column=0, sticky="w", pady=6)
        opacity = tk.Scale(
            content,
            from_=50,
            to=100,
            orient="horizontal",
            variable=opacity_var,
            length=170,
            showvalue=True,
            resolution=5,
            background=Palette.BACKGROUND,
            foreground=Palette.TEXT,
            activebackground=Palette.GREEN,
            troughcolor=Palette.TRACK,
            highlightthickness=0,
            borderwidth=0,
            font=self._font(8),
        )
        opacity.grid(row=2, column=1, columnspan=2, sticky="w", padx=(14, 0), pady=3)

        notify = tk.Checkbutton(
            content,
            text="Enable 5-hour quota notifications",
            variable=notifications_var,
            background=Palette.BACKGROUND,
            foreground=Palette.TEXT,
            activebackground=Palette.BACKGROUND,
            activeforeground=Palette.TEXT,
            selectcolor=Palette.SURFACE,
            font=self._font(9),
            borderwidth=0,
            highlightthickness=0,
        )
        notify.grid(row=3, column=0, columnspan=3, sticky="w", pady=(14, 5))

        threshold_frame = tk.Frame(content, background=Palette.BACKGROUND)
        threshold_frame.grid(row=4, column=0, columnspan=3, sticky="w", padx=(18, 0))
        for column, value in enumerate((20, 10, 5)):
            tk.Checkbutton(
                threshold_frame,
                text=f"{value}%",
                variable=threshold_vars[value],
                background=Palette.BACKGROUND,
                foreground=Palette.MUTED,
                activebackground=Palette.BACKGROUND,
                activeforeground=Palette.TEXT,
                selectcolor=Palette.SURFACE,
                font=self._font(9),
                borderwidth=0,
                highlightthickness=0,
            ).grid(row=0, column=column, padx=(0, 14))

        button_frame = tk.Frame(content, background=Palette.BACKGROUND)
        button_frame.grid(row=5, column=0, columnspan=3, sticky="e", pady=(20, 0))

        def close_dialog() -> None:
            self._settings_window = None
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()

        def apply_settings() -> None:
            try:
                interval = float(refresh_var.get())
            except (ValueError, tk.TclError):
                interval = self.settings.refresh_interval
            self.settings.refresh_interval = float(clamp(interval, 2.0, 60.0))
            self.settings.notifications_enabled = bool(notifications_var.get())
            selected = [value for value, var in threshold_vars.items() if var.get()]
            self.settings.notification_thresholds = sorted(selected or [20, 10, 5], reverse=True)
            self.monitor_service.set_interval(self.settings.refresh_interval)
            self.set_opacity(float(opacity_var.get()) / 100.0)
            self.settings_store.save(self.settings)
            close_dialog()

        self._dialog_button(button_frame, "Cancel", close_dialog, primary=False).pack(
            side="left", padx=(0, 8)
        )
        self._dialog_button(button_frame, "Apply", apply_settings, primary=True).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        dialog.bind("<Escape>", lambda _event: close_dialog())
        dialog.bind("<Return>", lambda _event: apply_settings())
        dialog.update_idletasks()
        try:
            root_rect = self.positioner.window_rect()
            dialog_width = dialog.winfo_width()
            dialog_height = dialog.winfo_height()
            monitors = self.positioner.work_areas()
            area = monitor_for_rect(root_rect, monitors, self.settings.monitor_name)
            x = root_rect.left + (root_rect.width - dialog_width) // 2
            y = root_rect.top + 28
            x, y = clamp_to_work_area(x, y, dialog_width, dialog_height, area)
            WindowsPositioner(dialog).place(x, y, dialog_width, dialog_height)
        except tk.TclError:
            pass
        try:
            dialog.grab_set()
        except tk.TclError:
            pass

    def _settings_label(self, parent: tk.Misc, text: str, *, muted: bool = False) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            background=Palette.BACKGROUND,
            foreground=Palette.MUTED if muted else Palette.TEXT,
            font=self._font(9),
        )

    def _dialog_button(
        self,
        parent: tk.Misc,
        text: str,
        command,
        *,
        primary: bool,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            background=Palette.GREEN if primary else Palette.SURFACE,
            foreground=Palette.BACKGROUND if primary else Palette.TEXT,
            activebackground=Palette.YELLOW if primary else Palette.BORDER,
            activeforeground=Palette.BACKGROUND if primary else Palette.TEXT,
            relief="flat",
            borderwidth=0,
            padx=14,
            pady=6,
            font=self._font(9, "bold" if primary else "normal"),
            cursor="hand2",
        )

    def _render(self) -> None:
        if self._closing:
            return
        try:
            self.canvas.delete("all")
            width, height = self.size
            self._rounded_rect(3, 4, width - 2, height - 1, 14, fill=Palette.SHADOW)
            self._rounded_rect(
                1,
                1,
                width - 3,
                height - 4,
                14,
                fill=Palette.BACKGROUND,
                outline=Palette.BORDER,
                width=1,
            )
            if self.settings.expanded:
                self._render_expanded(width, height)
            else:
                self._render_compact(width, height)
        except tk.TclError:
            if not self._closing:
                LOGGER.debug("Widget render failed", exc_info=True)

    def _render_compact(self, _width: int, _height: int) -> None:
        status_color = self._status_color(self.snapshot.status)
        if self._logo_image is not None:
            self.canvas.create_image(38, 18, image=self._logo_image)
        else:
            self.canvas.create_text(
                14,
                11,
                text="CODEX",
                anchor="nw",
                fill=Palette.TEXT,
                font=self._font(10, "bold"),
            )
        status_y = 37
        self.canvas.create_oval(14, status_y - 3, 20, status_y + 3, fill=status_color, outline="")
        self.canvas.create_text(
            25,
            status_y,
            text=self.snapshot.status.value,
            anchor="w",
            fill=Palette.MUTED,
            font=self._font(7, "bold"),
        )
        section_starts = (68, 195, 322)
        block_width = 104
        for x in section_starts:
            self.canvas.create_line(x, 10, x, 46, fill=Palette.DIVIDER)

        self._compact_rate_block(
            section_starts[0] + 10,
            block_width,
            "5H",
            self.snapshot.five_hour,
            self._bar_display["five_hour"],
        )
        self._compact_rate_block(
            section_starts[1] + 10,
            block_width,
            "7D",
            self.snapshot.weekly,
            self._bar_display["weekly"],
        )
        self._compact_context_block(section_starts[2] + 10, block_width)

    def _compact_rate_block(self, x: int, width: int, label: str, rate, display: Optional[float]) -> None:
        remaining = rate.remaining_percent if rate else None
        self.canvas.create_text(
            x,
            9,
            text=label,
            anchor="nw",
            fill=Palette.MUTED,
            font=self._font(8, "bold"),
        )
        self.canvas.create_text(
            x + width,
            8,
            text=f"{format_percent(remaining)} LEFT" if remaining is not None else "N/A",
            anchor="ne",
            fill=self._remaining_color(remaining),
            font=self._font(8, "bold"),
        )
        reset = format_countdown(rate.resets_at) if rate else "N/A"
        reset_text = f"reset {reset.replace(' ', '')}" if reset != "N/A" else "reset N/A"
        self.canvas.create_text(
            x,
            29,
            text=reset_text,
            anchor="nw",
            fill=Palette.MUTED,
            font=self._font(8),
        )
        self._progress_bar(x, 47, width, 3, display, self._remaining_color(remaining))

    def _compact_context_block(self, x: int, width: int) -> None:
        context = self.snapshot.context
        used = context.used_percent if context else None
        self.canvas.create_text(
            x,
            9,
            text="CTX",
            anchor="nw",
            fill=Palette.MUTED,
            font=self._font(8, "bold"),
        )
        self.canvas.create_text(
            x + width,
            8,
            text=f"{format_percent(used)} USED" if used is not None else "N/A",
            anchor="ne",
            fill=self._context_color(used),
            font=self._font(8, "bold"),
        )
        if context:
            prefix = "~" if context.estimated else ""
            usage = f"{prefix}{format_tokens(context.used_tokens)} / {format_tokens(context.window_tokens)}"
        else:
            usage = "context N/A"
        self.canvas.create_text(
            x,
            29,
            text=usage,
            anchor="nw",
            fill=Palette.MUTED,
            font=self._font(8),
        )
        self._progress_bar(x, 47, width, 3, self._bar_display["context"], self._context_color(used))

    def _render_expanded(self, width: int, _height: int) -> None:
        if self._logo_image is not None:
            self.canvas.create_image(25, 26, image=self._logo_image)
        self.canvas.create_text(
            47 if self._logo_image is not None else 18,
            15,
            text="CODEX MONITOR",
            anchor="nw",
            fill=Palette.TEXT,
            font=self._font(12, "bold"),
        )
        color = self._status_color(self.snapshot.status)
        status_y = 24
        self.canvas.create_oval(width - 91, status_y - 4, width - 83, status_y + 4, fill=color, outline="")
        self.canvas.create_text(
            width - 77,
            status_y,
            text=self.snapshot.status.value,
            anchor="w",
            fill=Palette.MUTED,
            font=self._font(8, "bold"),
        )
        self.canvas.create_line(18, 43, width - 18, 43, fill=Palette.DIVIDER)

        self._expanded_rate_row(18, 55, width - 36, "5 HOUR", self.snapshot.five_hour, "five_hour")
        self._expanded_rate_row(18, 121, width - 36, "7D", self.snapshot.weekly, "weekly")
        self._expanded_context_row(18, 187, width - 36)
        self.canvas.create_line(18, 255, width - 18, 255, fill=Palette.DIVIDER)

        metadata: list[str] = []
        if self.snapshot.model:
            metadata.append(f"Model: {self.snapshot.model}")
        if self.snapshot.reasoning_effort:
            metadata.append(f"Effort: {self.snapshot.reasoning_effort}")
        if self.snapshot.plan:
            metadata.append(f"Plan: {self.snapshot.plan.title()}")
        self.canvas.create_text(
            18,
            269,
            text="   •   ".join(metadata) if metadata else "Model details unavailable",
            anchor="nw",
            fill=Palette.TEXT if metadata else Palette.DIM,
            font=self._font(8),
        )

        session = self.snapshot.active_session or "No active session"
        self.canvas.create_text(
            18,
            290,
            text=f"Session: {self._ellipsize(session, 45)}",
            anchor="nw",
            fill=Palette.MUTED,
            font=self._font(8),
        )
        timestamp = self.snapshot.latest_event_at or self.snapshot.scanned_at
        try:
            updated = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
        except (OSError, OverflowError, ValueError):
            updated = "N/A"
        suffix = " • cached quota" if self.snapshot.rate_from_cache else ""
        if self.snapshot.error_summary:
            suffix += f" • {self.snapshot.error_summary}"
        self.canvas.create_text(
            18,
            311,
            text=f"Last update: {updated}{suffix}",
            anchor="nw",
            fill=Palette.DIM,
            font=self._font(8),
        )

    def _expanded_rate_row(self, x: int, y: int, width: int, title: str, rate, key: str) -> None:
        remaining = rate.remaining_percent if rate else None
        color = self._remaining_color(remaining)
        self.canvas.create_text(
            x,
            y,
            text=title,
            anchor="nw",
            fill=Palette.MUTED,
            font=self._font(8, "bold"),
        )
        value = f"{format_percent(remaining)} REMAINING" if remaining is not None else "N/A"
        self.canvas.create_text(
            x + width,
            y - 1,
            text=value,
            anchor="ne",
            fill=color,
            font=self._font(10, "bold"),
        )
        self._progress_bar(x, y + 23, width, 7, self._bar_display[key], color)
        reset = format_countdown(rate.resets_at) if rate else "N/A"
        self.canvas.create_text(
            x,
            y + 39,
            text=f"Reset: {reset}",
            anchor="nw",
            fill=Palette.MUTED,
            font=self._font(8),
        )

    def _expanded_context_row(self, x: int, y: int, width: int) -> None:
        context = self.snapshot.context
        used = context.used_percent if context else None
        color = self._context_color(used)
        self.canvas.create_text(
            x,
            y,
            text="CONTEXT",
            anchor="nw",
            fill=Palette.MUTED,
            font=self._font(8, "bold"),
        )
        value = f"{format_percent(used)} USED" if used is not None else "N/A"
        self.canvas.create_text(
            x + width,
            y - 1,
            text=value,
            anchor="ne",
            fill=color,
            font=self._font(10, "bold"),
        )
        self._progress_bar(x, y + 23, width, 7, self._bar_display["context"], color)
        if context:
            estimate = "estimated • " if context.estimated else ""
            detail = (
                f"{estimate}{format_tokens(context.used_tokens)} / "
                f"{format_tokens(context.window_tokens)}"
            )
        else:
            detail = "Context information unavailable"
        self.canvas.create_text(
            x,
            y + 39,
            text=detail,
            anchor="nw",
            fill=Palette.MUTED,
            font=self._font(8),
        )

    def _progress_bar(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        percent: Optional[float],
        color: str,
    ) -> None:
        self._rounded_rect(x, y, x + width, y + height, height / 2, fill=Palette.TRACK)
        if percent is None:
            return
        fill_width = width * clamp(percent, 0.0, 100.0) / 100.0
        if fill_width <= 0.25:
            return
        radius = min(height / 2, fill_width / 2)
        self._rounded_rect(x, y, x + fill_width, y + height, radius, fill=color)

    def _rounded_rect(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        radius: float,
        **options,
    ) -> int:
        radius = max(0.0, min(radius, abs(x2 - x1) / 2, abs(y2 - y1) / 2))
        points = (
            x1 + radius,
            y1,
            x2 - radius,
            y1,
            x2,
            y1,
            x2,
            y1 + radius,
            x2,
            y2 - radius,
            x2,
            y2,
            x2 - radius,
            y2,
            x1 + radius,
            y2,
            x1,
            y2,
            x1,
            y2 - radius,
            x1,
            y1 + radius,
            x1,
            y1,
        )
        return self.canvas.create_polygon(points, smooth=True, splinesteps=24, **options)

    @staticmethod
    def _remaining_color(value: Optional[float]) -> str:
        if value is None:
            return Palette.DIM
        if value > 50:
            return Palette.GREEN
        if value >= 20:
            return Palette.YELLOW
        if value >= 10:
            return Palette.ORANGE
        return Palette.RED

    @classmethod
    def _context_color(cls, used: Optional[float]) -> str:
        return cls._remaining_color(None if used is None else 100.0 - used)

    @staticmethod
    def _status_color(status: DataStatus) -> str:
        if status is DataStatus.LIVE:
            return Palette.GREEN
        if status is DataStatus.STALE:
            return Palette.YELLOW
        return Palette.DIM

    @staticmethod
    def _ellipsize(text: str, limit: int) -> str:
        clean = " ".join(str(text).split())
        if len(clean) <= limit:
            return clean
        return clean[: max(1, limit - 1)].rstrip() + "…"

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for after_id in (self._poll_after, self._tick_after, self._animation_after):
            if after_id is not None:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
        if self._placed:
            self._clamp_and_remember_position(save=False)
        self.settings_store.save(self.settings)
        self.monitor_service.stop(timeout=1.0)
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# Backwards-friendly aliases for callers that use a generic application name.
CodexUsageMonitorApp = CodexUsageMonitorUI
FloatingUsageWidget = CodexUsageMonitorUI

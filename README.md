# Codex Usage Monitor

Codex Usage Monitor is a small Windows widget that reads local Codex session
logs. It shows the latest available 5-hour and 7-day limits, reset countdowns,
and context usage when available. It runs locally and does not need an API key.

## Clone and run

These steps are for Windows 10/11. Install Python 3.10 or newer (with Tkinter)
and Git first.

1. **Clone the project.** Open PowerShell and download the code.

   ```powershell
   git clone https://github.com/nawshad85/codex-limit-checker.git
   cd codex-limit-checker
   ```

2. **Create a virtual environment.** Keep this app's packages separate.

   ```powershell
   python -m venv .venv
   ```

3. **Install the dependencies.** This includes optional Windows notifications.

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

4. **Start the widget.** It reads Codex session logs as they are written.

   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

5. **Use the widget.** Click to expand it, drag to move it, or right-click for
   settings and Exit. It may show `NO DATA` until Codex writes usage data.

## Features

- 5-hour and weekly used/remaining percentages and live reset countdowns
- Active-session context usage and context-window size when available
- Current model, reasoning effort, plan, and project name when Codex records them
- Automatic detection of new and concurrently active rollout files
- Cached last-known-good rate limits when a newer event has `rate_limits: null`
- Compact and expanded views, drag positioning, configurable opacity, and
  always-on-top behavior
- Six taskbar-aware position presets
- Optional Windows notifications at 20%, 10%, and 5% 5-hour allowance remaining
- Per-user Windows startup support without administrator access
- Incremental background reads with bounded initial scans
- Small rotating logs and diagnostic command-line modes

The compact bar uses OpenAI's Blossom symbol from its [official logo pack](https://cdn.openai.com/brand/OpenAI-Logos-2025.zip).
The symbol belongs to OpenAI. This independent project is not affiliated with
or endorsed by OpenAI.

## Codex session directory

The monitor resolves the Codex home in this order:

1. `CODEX_HOME`, if it is set and non-empty
2. `%USERPROFILE%\.codex`

It then appends `sessions`. In other words, set `CODEX_HOME` to the directory
that *contains* `sessions`, not to the `sessions` directory itself.

Verify the normal location with:

```powershell
Get-ChildItem "$HOME\.codex\sessions" -Recurse -Filter "rollout-*.jsonl"
```

To verify the location with `CODEX_HOME` support:

```powershell
$CodexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
Get-ChildItem (Join-Path $CodexRoot "sessions") -Recurse -Filter "rollout-*.jsonl"
```

For a non-default location, set the variable before starting the monitor:

```powershell
$env:CODEX_HOME = "D:\Path\To\CodexHome"
python main.py
```

## Command-line diagnostics

Print one privacy-safe snapshot and exit:

```powershell
python main.py --once
```

Example output:

```text
Codex Usage Monitor

5-hour:
Used: 28%
Remaining: 72%
Reset: 1h 42m

Weekly:
Used: 57%
Remaining: 43%
Reset: 4d 17h

Context:
157K / 258K
61%

Status: LIVE
```

Enable field-level parser diagnostics in the terminal and local log:

```powershell
python main.py --debug
```

Debug logging reports file discovery, byte offsets, skipped malformed lines,
and update state. It does not print prompts, message text, or complete JSON
events. Other supported options are:

```powershell
python main.py --version
python main.py --help
```

`--once` is intended for `python main.py`. The packaged application is built as
a windowed executable and therefore has no console in which to display the
summary.

## Understanding the values

### Rate limits

Codex normally records a 300-minute primary window and a 10,080-minute
secondary window. The app interprets these as the 5-hour and weekly limits and
calculates:

```text
remaining percentage = 100 - used percentage
```

Percentages are clamped to 0–100. Across multiple sessions, the newest valid
account-level rate snapshot wins. A null or incomplete newer snapshot never
replaces a valid cached value with zero or `N/A`.

### Context

`total_token_usage` in Codex rollouts is cumulative session usage and is not a
reliable measure of the current context. The monitor prefers explicit context
fields and then `last_token_usage.total_tokens`, which describes the latest
active context much more accurately. If it must derive a count from input and
output values, the value is internally marked as estimated and displayed with
an approximation marker.

Context, model, effort, and project metadata come from the most recently active
session. Rate limits remain account-level and may come from another recent
session.

### Data state

- `LIVE` — recent valid Codex activity is available.
- `STALE` — the widget is retaining last-known-good data, but its most recent
  valid activity or rate snapshot is older than roughly two minutes.
- `NO DATA` — no usable rate-limit or context snapshot has been found yet.

The reset countdown updates once per second. Rollout data is refreshed in the
background every four seconds by default, and a reset reaching zero requests an
immediate refresh.

## Widget controls

- The default compact size is 450 x 56 pixels; expanded mode is 450 x 334.
- Opacity defaults to 95% and can be set from 50% to 100%.
- Click without dragging to switch between compact and expanded views.
- Drag from a non-interactive area to move the widget.
- Right-click for `Expand / Collapse`, `Refresh Now`, `Always on Top`,
  `Launch at Startup`, `Opacity`, `Position`, `Open Codex Sessions Folder`,
  `Open Latest Session File`, `Settings`, and `Exit`.
- Position presets use the Windows monitor work area so bottom positions do not
  intentionally overlap the taskbar.

The last position, opacity, expanded state, refresh interval, notification
preferences, always-on-top state, and position preset are saved locally at:

```text
%APPDATA%\CodexUsageMonitor\settings.json
```

If a saved position is no longer visible after a monitor is disconnected, the
widget moves back into an available work area at startup.

## Notifications

Notifications are based on *remaining* 5-hour allowance. Each configured
threshold is sent at most once for a particular reset window. Notification
delivery failures do not stop or alter monitoring.

Install the optional helper if it was omitted during setup:

```powershell
python -m pip install winotify==1.1.0
```

Windows Do Not Disturb/Focus Assist and per-app notification settings can still
suppress a toast.

## Launch at Windows startup

Use `Launch at Startup` in the right-click menu. The app creates or removes only
its own value under the current user's `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
key, so it needs no administrator privileges.

If the source folder, virtual environment, or packaged executable is moved,
disable and re-enable startup so the stored command points at the new location.

## Build the standalone executable

Close the running widget first. Install PyInstaller in the virtual environment,
then run the build script on Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build.bat
```

The output is:

```text
dist\CodexUsageMonitor.exe
```

It is a one-file, no-console executable and does not require Python on the
computer where it runs. The executable uses the default file icon; the Blossom
symbol appears inside the widget. An unsigned local build may trigger Windows
SmartScreen or antivirus reputation warnings; inspect the source and build
locally if that is a concern.

`build.bat` uses `.venv\Scripts\python.exe` when present and otherwise falls
back to `python` on `PATH`. It deliberately fails with an actionable message if
PyInstaller has not been installed.

## Run the tests

The tests use synthetic rollout data and never copy real session content:

```powershell
python -m unittest discover -s tests -v
```

## Logging

Logs are written to:

```text
%APPDATA%\CodexUsageMonitor\monitor.log
```

The log rotates at 256 KiB and keeps two backups. It contains operational
metadata such as a rollout filename, byte offset, or exception type; it does
not contain prompts or full rollout events.

## Troubleshooting

### No data found

1. Run the session-directory verification command above.
2. Start or use a Codex session so it emits a token-count event.
3. Run `python main.py --once` to separate parsing from UI behavior.
4. Run `python main.py --debug` and inspect the safe diagnostic output.

The app stays open with `NO DATA` if the directory is missing, empty, or
temporarily inaccessible.

### Rate limit unavailable

Codex sometimes emits `rate_limits: null`. The monitor searches backward in
recent rollouts and retains the last valid snapshot. `N/A` means that no valid
snapshot has been found at all. Generate fresh Codex activity, choose `Refresh
Now`, and check again.

### Context unavailable

Context appears only after Codex records token usage and a context-window size.
The account rate limits may therefore be visible before context data. A fresh
turn in the active Codex session usually creates the needed event.

### The widget says STALE

`STALE` is expected when Codex has been idle or only cached rate data is
available. It does not mean the displayed values were reset. If Codex is
actively producing events, check the system clock, permissions, and `--debug`
log, then use `Refresh Now`.

### A different CODEX_HOME is in use

Confirm that the variable points to the Codex root and that a `sessions`
subdirectory exists. Environment changes affect newly launched processes only,
so restart the monitor after changing it.

### Startup does not launch the widget

The stored per-user command may be stale after moving the project or executable.
Run the app manually, disable `Launch at Startup`, and enable it again. Source
mode also requires the referenced Python virtual environment to still exist.

### Notifications do not appear

Confirm `winotify` is installed in the same Python environment, notifications
are enabled in the app, and Windows Do Not Disturb/Focus Assist is not hiding
them. Notifications are intentionally not repeated after a threshold has
already fired for the current reset window.

### The widget is off-screen or settings are damaged

Exit the process and rename or remove the settings file shown above. Defaults
are recreated on the next launch. Removing this file does not touch Codex data.

### The packaged application has no terminal output

This is intentional: the executable uses PyInstaller's windowed bootloader so a
console does not flash at normal startup. Use `python main.py --debug` or
`python main.py --once` from the source directory for diagnostics.

## Privacy and security

Codex Usage Monitor is local-only:

- It never uses an OpenAI API key or calls the OpenAI API.
- It has no telemetry, analytics, advertising, or network client.
- Rollout files are opened read-only; the app never modifies or deletes them.
- The parser extracts only quota, token, context, model, effort, plan, session,
  and working-directory metadata. It deliberately skips prompt/message fields.
- It does not log complete JSON events, prompts, responses, or credentials.
- The only persistent app data is settings, small rotating logs, notification
  deduplication state, and the optional per-user startup registry value.

`Open Codex Sessions Folder` and `Open Latest Session File` ask Windows to open
local paths with their normal associated applications. Toast notifications are
delivered locally through Windows notification facilities.

## Project layout

```text
main.py                 Command-line entry point and UI launcher
app/parser.py           Privacy-conscious JSONL field extraction
app/monitor.py          Discovery, bounded bootstrap scans, and incremental tails
app/models.py           Immutable usage snapshots and domain models
app/settings.py         Validated local settings persistence
app/logging_config.py   Small rotating privacy-safe log
app/ui.py               Tkinter compact/expanded widget
app/windows.py          DPI, work-area, startup, and Windows helpers
app/notifications.py    Optional threshold notification provider
tests/                  Synthetic parser and monitor tests
build.bat               Standalone Windows build entry point
```

# Codex Usage Monitor

Codex Usage Monitor is a small Windows widget for Codex rate limits and context
usage. It asks the installed, signed-in Codex CLI for account limits and reads
local session logs for context. It does not need an OpenAI API key or make model
requests.

## Clone and run

These steps are for Windows 10/11. Install Python 3.10 or newer (with Tkinter),
Git, and the [Codex CLI](https://learn.chatgpt.com/docs/codex/cli) first.

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

4. **Sign in to Codex.** Use your ChatGPT account in the CLI.

   ```powershell
   codex login
   codex login status
   ```

5. **Start the widget.** It reads account limits and local session data.

   ```powershell
   .\.venv\Scripts\python.exe main.py
   ```

6. **Use the widget.** Click to expand it, drag to move it, or right-click for
   settings and Exit. If account limits are unavailable, it can show recent
   limits from local Codex logs.

## Features

- 5-hour and weekly used/remaining percentages and live reset countdowns
- Active-session context usage and context-window size when available
- Current model, reasoning effort, plan, and project name when Codex records them
- Automatic detection of new and concurrently active rollout files
- Account rate limits from the Codex CLI, with recent session-log fallback
- Saved last-known-good rate limits when current sources are unavailable
- Compact and expanded views, drag positioning, configurable opacity, and
  always-on-top behavior
- Six taskbar-aware position presets
- Optional Windows notifications at 20%, 10%, and 5% 5-hour allowance remaining
- Per-user Windows startup support without administrator access
- Separate account polling and incremental, bounded session-log reads
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

Check the Codex CLI account-limit source without making a model request:

```powershell
python main.py --check-ratelimits
```

If this reports a sign-in problem, run `codex login` and try again. This check
uses the CLI's local App Server, which may contact OpenAI for account limits.

Illustrative `--once` output (your values and data state will differ):

```text
Work + Codex Usage Monitor

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

Source: app-server
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

The primary account source is the signed-in Codex CLI's local App Server
[`account/rateLimits/read`](https://learn.chatgpt.com/docs/app-server) method.
The monitor selects the Codex bucket's 300-minute and 10,080-minute windows for
5-hour and 7-day usage. If the account read is unavailable, it checks recent
local rollout files for valid rate-limit events. It calculates:

```text
remaining percentage = 100 - used percentage
```

Percentages are clamped to 0–100. Across multiple sessions, the newest valid
rollout snapshot wins for fallback. A null or incomplete newer snapshot never
replaces a valid value with zero or `N/A`. The last valid normalized rate values
are saved at `%APPDATA%\CodexUsageMonitor\rate_limits.json`; this file does not
contain raw account responses, credentials, or rollout events.

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

- `LIVE` — the Codex CLI account-limit read returned valid limits.
- `FALLBACK` — the account read is unavailable; recent rollout rate limits are
  being shown.
- `STALE` — only previously saved rate limits are available.
- `NO DATA` — no usable rate limits are available yet. Context may still appear.

The reset countdown updates once per second. Account limits are requested every
20 seconds by default (configurable from 15 to 300 seconds); session logs are
checked every four seconds by default. A reset reaching zero requests a refresh.

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

The last position, opacity, expanded state, session and account refresh
intervals, notification preferences, always-on-top state, and position preset
are saved locally at:

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

1. Run `codex login status` to check the CLI sign-in.
2. Run `python main.py --check-ratelimits` to check the account source.
3. Run the session-directory verification command above.
4. Run `python main.py --once` or `--debug` for safe diagnostics.

The app stays open with `NO DATA` when no rate limits can be read. It can still
show context if the session directory contains usable token-count events.

### Rate limit unavailable

If `codex` is not recognized, install the
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli) and reopen PowerShell.
Check its sign-in with `codex login status`; run `codex login` if needed. Then
run `python main.py --check-ratelimits`. An expired sign-in or network problem
can prevent the CLI from fetching account limits. The monitor can fall back to
recent rollout values, then to its saved rate cache. `N/A` means no valid value
has been found. Use `Refresh Now` after sign-in or connectivity recovers. A CLI
sign-in is separate from signing in to another Codex app.

### Context unavailable

Context appears only after Codex records token usage and a context-window size.
The account rate limits may therefore be visible before context data. A fresh
turn in the active Codex session usually creates the needed event.

### The widget says STALE

`STALE` means the widget has only its saved rate values. It does not mean those
values were reset to zero. Check CLI sign-in and connectivity with
`--check-ratelimits`, then use `Refresh Now`.

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

Codex Usage Monitor stores its data locally:

- It uses the signed-in local Codex CLI App Server to request account limits.
  The CLI may contact OpenAI for that read; the monitor does not make model
  requests or require an OpenAI API key.
- It has no telemetry, analytics, or advertising. It does not upload rollout
  contents.
- Rollout files are opened read-only; the app never modifies or deletes them.
- The parser extracts only quota, token, context, model, effort, plan, session,
  and working-directory metadata. It deliberately skips prompt/message fields.
- It does not log complete JSON events, prompts, responses, or credentials.
- The persistent app data is settings, normalized rate-limit cache, small
  rotating logs, notification deduplication state, and the optional per-user
  startup registry value. The app does not save CLI credentials.

`Open Codex Sessions Folder` and `Open Latest Session File` ask Windows to open
local paths with their normal associated applications. Toast notifications are
delivered locally through Windows notification facilities.

## Project layout

```text
main.py                 Command-line entry point and UI launcher
app/codex_app_server.py Local Codex CLI account-limit reader
app/parser.py           Privacy-conscious JSONL field extraction
app/monitor.py          Account and session monitoring with fallback
app/models.py           Immutable usage snapshots and domain models
app/settings.py         Validated settings and normalized rate-cache persistence
app/logging_config.py   Small rotating privacy-safe log
app/ui.py               Tkinter compact/expanded widget
app/windows.py          DPI, work-area, startup, and Windows helpers
app/notifications.py    Optional threshold notification provider
tests/                  Synthetic parser and monitor tests
build.bat               Standalone Windows build entry point
```

## Development credit

OpenAI Codex assisted with implementation, testing, and documentation.

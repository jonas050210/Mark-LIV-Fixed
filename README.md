# Mark-LIV

MARK LIV is a cross-platform JARVIS-style assistant with an optional local **Hey Jarvis** wake word.

## Run

```bash
python setup.py
python main.py
```

Before installing anything, validate the checkout and shipped assets without changing the environment:

```bash
python setup.py --check
python test_overall.py
```

`test_overall.py` is safe by default: it uses mocks and temporary files, does not open applications, and skips hardware checks. On a Windows machine, add `--windows` to opt into the real monitor/audio integration checks.

The wake-word engine is optional. Open the app's **⚙ settings drawer** and choose **WAKE WORD: DOWNLOAD**. The installer puts the model in the openwakeword package directory and runs native wake-word setup in a child process, so a broken ONNX runtime cannot close the main window.

If the settings drawer or download still has a problem, run the dependency diagnostic from the repository root:

```bash
python check_wake_word.py
```

The diagnostic checks NumPy, ONNX Runtime, and openwakeword in isolated subprocesses and reports native crashes separately. It does not require an API key or network access for the basic checks.

## Desktop control

The assistant now has named window and monitor control. Examples:

- `Minimize Chrome`
- `Move Discord to monitor 2`
- `Snap this window left on monitor 1`
- `List my open windows and monitors`
- `Restart MARK LIV`

The restart command saves the current session, stops audio/wake-word workers, launches a fresh MARK LIV process, and then exits the old one. Closing a named application is immediate and does not add a MARK LIV confirmation; the application can still present its own native save prompt when it genuinely has unsaved work. Power actions and Windows administrator/UAC operations are never silently bypassed.

The phone dashboard's **CONTROL** panel uses the same action registry as voice commands. It is the primary control surface rather than a hotkey collection: press **Ctrl/Cmd-K** for the command palette, use quick cards for windows, monitors, audio, Explorer, Task Manager, and relaunch, and use the per-window FOCUS/MIN/MAX/CLOSE controls. The FILE EXPLORER panel searches Home, Desktop, Downloads, Documents, or Pictures and offers separate OPEN and SELECT actions for each verified result. It shows live action IDs, progress, cancellation, confirmations, named-window occupancy, monitor tiles, audio health, and undo history. It is a control surface, not an unrestricted way around Windows security.

MARK LIV launches applications from a real index of what is installed on the
machine — Windows `App Paths` and Roblox protocol registry entries, Start-menu
shortcuts (including generic Chrome/Edge web apps such as Arena, Twitch, and
YouTube), versioned Roblox installations, `shell:AppsFolder` package ids, macOS
application bundles, and Linux desktop entries. Web apps keep their original
profile and app-id switches, so they open as standalone apps instead of ordinary
browser tabs. It never presses the Windows key and types a name into the Start menu,
so a launch cannot land in a search box, and it reports honestly when an
application is not installed or when no window appeared instead of claiming
success. A stale executable or shortcut triggers one automatic index rebuild
and one bounded retry; slow shortcuts and Store apps are verified with an
adaptive before/after window check. Say `rescan my apps` after installing
something new if you want to refresh proactively.

The launcher also remembers what you actually use: the dashboard's **APP
LAUNCHER** panel shows pinned and recently opened applications as icon buttons,
and the index refreshes itself when a Start-menu folder or desktop entry changes,
so a freshly installed program appears without waiting out the cache. Opening an
application is undoable — `undo` closes the window that launch created, and
refuses if you already closed it yourself.

Applications can be opened with a document or URL, for example
`open Chrome with youtube.com`. Arguments are passed as a real argv list, never
through a shell, and command-line switches are rejected.

Window arrangements can be saved and restored by name:

- `Save this layout as work`
- `Set up work`
- `List my layouts` / `Delete the layout gaming`

A layout stores each window's monitor, position, size and state, matched by
process name rather than by window handle, so it survives a restart. Windows
that are not running are reported, never launched behind your back.

Placement is part of the same request:

- `Open Chrome on monitor 2 in fullscreen`
- `Open Spotify in the background` (focus stays where it is)
- `Open Discord snapped left on monitor 1`

Closing, minimising and switching are always done by window handle. Focus-
dependent key combinations — `alt+f4`, `command+q`, `ctrl+w`, `alt+tab`, and the
bare Windows/Command key — are refused by `computer_control` and redirected to
`window_manager`, because they act on whichever window happens to have focus and
can close the wrong program.

Spotify is controlled through the Spotify Web API rather than its window: play,
pause, skip, search, queue, shuffle, repeat, seek, volume, and device selection.
When no Spotify Connect device is active, plain transport commands fall back to
the operating system's media keys. The dashboard's **SPOTIFY** panel shows the
current track, artwork, a seek bar, and those controls; the **APP LAUNCHER**
panel searches the same application index and opens an app on a chosen monitor
and state.

Application and Explorer control are deliberately conservative. `open Chrome` or `open Roblox` focuses an existing window instead of silently creating another one. A second Roblox client is only attempted for an explicit request such as `open another Roblox`; MARK LIV verifies the new window and moves it to the opposite monitor when a second display is available, otherwise it reports the limitation. File searches resolve Windows known folders, search the user's home folder by default, use Everything when installed, and show numbered candidates when more than one file matches. `open` and `select` only act on a unique exact or search result, so MARK LIV does not guess between similarly named files.

Audio can also be controlled by voice or the dashboard: say `list audio devices`, then `use JBL Quantum 400 microphone` or `use JBL Quantum 400 speakers`. The saved device name is resolved again after reconnects, so changing USB device indices does not silently select the wrong device. The admin panel reports selected input/output, connected/fallback state, host API, and sample rate. A reconnect request rebuilds both streams while keeping the conversation resumption handle.

On Windows, monitor enumeration opts into per-monitor DPI awareness before reading HWND coordinates and uses the current display mode for refresh rate. This keeps two 1920×1080 displays and high-refresh modes such as 180 Hz visible as physical pixel geometry rather than scaled logical coordinates. The native ctypes fallback still reports geometry when pywin32 is not installed.

## Memory and saved sessions

MARK LIV keeps durable facts—identity, preferences, projects, relationships, plans, and notes—separate from conversation bookmarks. Long-term facts are retrieved through the existing bounded memory index, while the automatic end-of-session summary only feeds the next startup briefing.

The `session_manager` action adds an explicit local Session Vault. Say `save this session as launch plan`, `list my saved sessions`, `resume launch plan`, or `delete launch plan`. A bookmark stores at most 40 sanitized turns plus an optional summary; at most 20 named sessions are retained, duplicate names update the same bookmark, and an ambiguous name must be replaced by the displayed ID. Resuming drops the provider resume token, opens a fresh Live conversation, and injects only the bounded historical context. Deletion requires human confirmation.

Saved sessions live in the private transactional `memory/sessions.json` store. The file is ignored by Git, protected from file actions, atomically updated, recoverable after corruption, and never sent to an external memory service merely for storage.

### Spotify background control

`media_control` uses the Spotify Web API and Spotify Connect rather than clicking the Spotify window. It can search, play, pause, skip, change volume, inspect the current track, and list devices while Spotify remains minimized or in the background. The one-time `connect` flow requires a Spotify developer client ID, the local redirect URI `http://127.0.0.1:8765/callback`, and a Spotify Premium account for playback control. Playback requires an active Spotify Connect device; MARK LIV does not falsely claim that a track started when Spotify has no available device.

## Optional capabilities

Browser automation (Playwright), screen/camera capture (NumPy/OpenCV/MSS/Pillow), and system metrics (psutil) are optional. MARK LIV starts without them: imports are lazy, the action registry keeps a capability record, and the dashboard/voice result identifies the missing package instead of rejecting the whole application. Native browser opening and the rest of desktop control remain available.

## Action safety and reliability

All discovered actions now pass through one registry contract. Results have explicit `succeeded`, `failed`, `busy`, `cancelled`, `forbidden`, `confirmation_pending`, `unavailable`, and `timed_out` states; handlers keep their old string API only at the Gemini boundary. The registry also owns per-action deadlines, bounded legacy-worker capacity, trusted/admin checks, confirmation metadata, and the dashboard capability manifest. Packaged action source is size-, ownership-, permission-, link-, and descriptor-checked before execution.

High-impact operations never accept a model-supplied `confirmed` flag. The HUD confirmation token is issued by the interface and protects file deletion, power actions, WiFi changes, and other destructive operations. Named and active-window close requests are immediate because MARK LIV already targets the intended window by handle instead of sending a focus-dependent shortcut. Pending confirmations actively expire after 90 seconds and remain bound to the action ID shown to the user. Reversible settings continue to use the shared undo stack.

The active action surface is intentionally small and PC-focused. Travel, weather, messaging, developer-agent, game-updater, generated-desktop-task, and YouTube-specific actions were removed instead of advertising unrelated or duplicated capabilities. Browser control covers normal HTTP(S) websites; local-file and script protocols are rejected. `media_control` uses Spotify Connect for playback without repeatedly foregrounding Spotify. `shortcut_manager` stores deterministic aliases such as `gd → Geometry Dash` and `roblox → Roblox Player`.

File actions stay inside approved user folders, reject credential/browser-profile paths, symbolic links, and Windows reparse points, and never silently replace a destination. Large parser, archive, media, and directory-copy workloads are bounded. Parser inputs are copied through bounded no-follow descriptors into private stable snapshots before third-party libraries reopen them. Generated files are written to private staging names and published without replacement, so a timeout or name race cannot expose partial output or delete someone else's file. Direct execution of model-produced source code is disabled.

## Dashboard and local-data security

The phone dashboard uses short-lived bearer sessions, one-time pairing codes, login throttling, authenticated encrypted command payloads, bounded uploads, and authenticated WebSockets. It generates a per-install TLS key pair for LAN access. If TLS cannot be initialized, plain HTTP binds to `127.0.0.1` only—the dashboard is not exposed unencrypted to the LAN. Pairing codes, auth sessions, and remembered-device sessions are capped and expire automatically.

Configuration, memory, shortcuts, and Spotify tokens use locked atomic JSON transactions with corruption recovery and private permissions where the platform supports them. Local LLM endpoints are restricted to loopback or private IP addresses. Spotify OAuth accepts only an explicit unprivileged `http://localhost:<port>/callback` redirect.

Plugins are executable Python and must be treated as trusted local code. Discovery isolates import failures so one broken plugin cannot stop startup, rejects symbolic links and group/world-writable plugin files, validates schemas, and applies deadlines plus structured result handling.

Your API keys and runtime memory are intentionally ignored by Git. Never commit `config/api_keys.json`, Spotify tokens, certificates/private keys, or personal data from `memory/`.

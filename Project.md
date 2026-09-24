# MARK LIV — Complete Project Record

**Repository:** `jonas050210/Mark-LIV-fixed`

**Working branch:** `arena/01a0d47f-mark-liv-fixed`

**Purpose of this document:** This is the complete project record for the current implementation. It explains the project’s purpose, architecture, files, decisions, safety rules, implemented features, tests, limitations, and the work completed during this development session. It is intentionally more detailed than `README.md`.

`README.md` is the concise user-facing setup guide. `readme.md` contains extended product and visual-design notes. This file is the engineering and project-history reference.

---

## 1. What MARK LIV is

MARK LIV is a cross-platform, JARVIS-style desktop assistant built around the Gemini Live API. It can:

- listen and speak in real time;
- display a holographic animated avatar;
- remember information across sessions;
- control applications, windows, files, audio, browsers, and selected system settings;
- expose the same computer-control actions through voice, the local GUI, and the authenticated remote dashboard;
- use optional local wake-word detection;
- report unavailable capabilities rather than crashing when optional dependencies are missing;
- provide confirmations for high-impact operations;
- provide undo support for selected reversible operations;
- expose live action progress, cancellation, status, and dashboard diagnostics.

The project is deliberately PC-focused. It is not being expanded with gaming modes, profiles, unrelated autonomous behavior, travel features, messaging integrations, or other feature categories that would make the action surface less reliable.

---

## 2. Main user requirements that drove the work

The important requirements were:

1. **Reliable application launching**
   - A normal application launch should not silently duplicate an application that is already open.
   - Existing windows should be focused when possible.
   - MARK LIV must not claim success when an operation failed.

2. **Roblox behavior**
   - `open Roblox` should focus the existing Roblox window rather than create a duplicate.
   - A second Roblox instance must only be attempted after an explicit request.
   - The new Roblox window should be moved to the monitor opposite the currently running Roblox window when possible.
   - MARK LIV must verify that a second window exists.
   - MARK LIV must verify the monitor move when possible.
   - If Roblox prevents multiple instances, MARK LIV must report that honestly.

3. **File Explorer reliability**
   - Resolve real Windows folders instead of guessing paths.
   - Search the user’s Home folder by default for omitted search locations.
   - Use Everything’s command-line search tool when available.
   - Use a bounded fallback search when Everything is unavailable.
   - Detect exact files.
   - Do not guess between multiple files with the same name.
   - Present numbered candidates.
   - Allow an explicit candidate number to be selected after the result list is shown.
   - Open folders, open files, or reveal/select files in Explorer.
   - Do not claim an Explorer action succeeded if the target does not exist.

4. **GUI quality**
   - Make live actions observable.
   - Show progress, cancellation, confirmations, and undo state.
   - Give the dashboard a useful Explorer control surface.
   - Avoid generic confirmations that hide the real target.

5. **Project quality**
   - Keep optional capabilities isolated.
   - Keep action schemas and implementation synchronized.
   - Add a complete project-wide verification runner.
   - Update setup and documentation.
   - Perform a deep bug hunt after the broad implementation pass.

---

## 3. Current implementation status

The implementation, documentation, verification runner, and several bug-hunt fixes are complete on the working branch.

Latest implementation commit before this document:

```text
a81125c Check dashboard route contract in overall runner
```

Documentation commit for this file:

```text
7e3d55e Document complete project architecture and history
```

The branch also contains the preceding implementation and hardening commits described later in this document.

The working tree is intended to remain clean after commits. The current sandbox is Linux, so Windows hardware behavior and the real Roblox/two-monitor flow cannot be physically verified here.

---

## 4. System architecture

### 4.1 High-level flow

```text
User voice / GUI / remote dashboard
                 |
                 v
        MARK LIV dispatch layer
                 |
                 v
     Core action registry and policy
                 |
       +---------+----------+
       |                    |
       v                    v
  Inline tools       Discovered actions
  in main.py          in actions/*.py
       |                    |
       +---------+----------+
                 |
                 v
       Action runtime records
       status/progress/cancel
                 |
       +---------+----------+
       |                    |
       v                    v
   Voice result       Dashboard events
                 |
                 v
     UI, logs, undo, confirmation
```

### 4.2 Action discovery

`core/action_loader.py` scans `actions/*.py` for a module-level `TOOL` dictionary. Each valid tool supplies:

- a unique name;
- a description for Gemini and the dashboard;
- a Gemini-compatible parameter schema;
- a callable handler;
- optional risk, confirmation, admin, undo, timeout, behavior, and scheduling metadata.

The registry is the policy boundary. It normalizes legacy string-returning handlers into `ActionResult` values and applies confirmation, admin, cancellation, timeout, and availability rules in one place.

### 4.3 Action execution lifecycle

An action can move through these states:

```text
queued
  -> running
  -> confirmation_pending
  -> succeeded
  -> failed
  -> cancelled
  -> timed_out
  -> unavailable
  -> forbidden
  -> confirmation_failed
```

The `core/action_runtime.py` registry stores bounded in-memory run records. It exposes:

- action ID;
- action name;
- parameters;
- source;
- status;
- progress;
- message;
- timestamps;
- cancellation state.

The dashboard receives action lifecycle events from `main.py` and can show or cancel actions.

### 4.4 Confirmation lifecycle

High-impact operations cannot rely on a model-provided `confirmed=true` field. The actual process is:

1. The action registry determines that confirmation is required.
2. `core/confirm.py` stores a pending confirmation and asks the UI to show a human-facing confirmation banner.
3. The action returns `confirmation_pending` without performing the irreversible work.
4. The user confirms or cancels through the UI.
5. The real operation runs only after confirmation.
6. Cancellation, expiration, headless failure, and post-confirmation crashes update the live action record correctly.

A headless process refuses a destructive operation when it cannot display a confirmation interface.

### 4.5 Undo

`core/undo.py` stores reversible operations. File and window actions use this shared mechanism where appropriate. Destructive operations are not made safe merely by claiming they are undoable; they also pass through confirmation rules.

---

## 5. Application launching and Roblox

### 5.1 `actions/open_app.py`

The launcher now:

- normalizes application aliases;
- resolves saved shortcuts;
- detects visible windows by title and process name;
- focuses an existing application instead of launching a duplicate by default;
- recognizes Roblox through titles and known Roblox process names;
- supports an explicit `new_instance` parameter;
- recognizes explicit natural-language second-instance requests such as “another Roblox”;
- captures the set of existing windows before launching;
- waits for a new window after a second-instance request;
- chooses a monitor different from the currently running Roblox window;
- moves the new window through `core.window_manager`;
- rechecks the window list after movement;
- verifies that the new window is physically positioned on the intended monitor;
- reports failure when Roblox refuses a second client;
- reports inability to move or verify honestly.

Ordinary application opening remains compatible with Windows, macOS, and Linux launch fallbacks.

### 5.2 `core/window_manager.py`

This shared module provides:

- visible window enumeration;
- process and title matching;
- native Windows HWND support;
- fallback support through `pygetwindow` or `wmctrl` where available;
- monitor enumeration;
- DPI-aware Windows geometry;
- refresh-rate information where exposed;
- focus, minimize, maximize, restore, close, move, resize, and monitor placement.

Roblox launch logic reuses this module instead of implementing a second window system.

---

## 6. File Explorer and file control

### 6.1 `core/explorer.py`

This is the central read-only Explorer/location/search helper. It provides:

- native Windows Known Folder API resolution using `SHGetKnownFolderPath`;
- aliases for Desktop, Documents, Downloads, Pictures, Music, Videos, Home, and common variations;
- subpath resolution such as `documents/reports`;
- safe argument-list subprocess calls without shell interpolation;
- optional Everything `es.exe`/`es` search;
- bounded fallback traversal;
- skipped directories for performance and privacy protection;
- extension filtering;
- exact-file detection;
- deterministic candidate ordering;
- Explorer opening;
- file selection/reveal;
- numbered match formatting.

The fallback search is bounded by time and result count. It avoids following directory symlinks and skips common cache, dependency, application-data, hidden, and source-control directories.

### 6.2 `actions/file_controller.py`

The file controller still supports the existing file operations, but Explorer behavior is now integrated through `core.explorer`.

Relevant actions include:

- `find`
- `open`
- `open_folder`
- `explorer`
- `select`
- `show_in_explorer`
- `reveal`
- `list`
- `create_file`
- `create_folder`
- `delete`
- `move`
- `copy`
- `rename`
- `read`
- `write`
- `largest`
- `disk_usage`
- `organize_desktop`
- `info`

Search behavior:

- omitted search location defaults to `home`, not Desktop;
- maximum search result count is bounded;
- exact paths are detected first;
- multiple matches are listed instead of guessed;
- `match_index` permits a later explicit candidate choice;
- Explorer open/select operations only act on the selected or unique result.

### 6.3 Dashboard Explorer panel

The remote dashboard now has a dedicated File Explorer card. It supports:

- location selector;
- filename query;
- extension filter;
- numbered results;
- path and basic metadata display;
- separate Open and Select buttons;
- authenticated search endpoint;
- authenticated open/select endpoint.

The dashboard endpoints are:

```text
GET  /api/explorer/search
POST /api/explorer/open
```

The endpoint only performs read-only search and opens an already-resolved path in the user’s Explorer. It does not expose delete, move, rename, or write operations through the Explorer card.

---

## 7. Dashboard and GUI

### 7.1 Existing dashboard capabilities

The dashboard already provides:

- authenticated login through a short-lived pairing PIN;
- bearer-token sessions;
- optional AES-256-CBC payload encryption;
- QR pairing support;
- live action records;
- action cancellation;
- capability listing;
- monitor information;
- audio health information;
- desktop window listing;
- focus, minimize, maximize, and close controls;
- undo history;
- command palette;
- upload support;
- live messages and system notices.

### 7.2 New dashboard improvements

The following were added during this pass:

- dedicated Explorer search/open/select panel;
- quote-safe HTML escaping for dynamic values;
- safer Windows firewall setup without Python `shell=True` subprocess calls;
- dashboard PIN login rate limiting;
- invalid login request handling;
- route contract checks in `test_overall.py` when dashboard dependencies are installed.

### 7.3 Dashboard security

- API endpoints require an authenticated bearer token.
- Pairing keys are short-lived and one-time use.
- Login attempts are rate limited by peer address.
- Persistent device tokens can be revoked.
- Dynamic dashboard values are HTML escaped, including quotes and apostrophes.
- File Explorer endpoints do not accept shell commands.
- Firewall setup uses a generated batch file only for the OS-level firewall operation and invokes it through `cmd.exe` with `shell=False` from Python.
- The dashboard is still a local/LAN control surface and should not be exposed to the public Internet without additional deployment security.

---

## 8. Setup, dependencies, and capability behavior

### 8.1 `setup.py`

Normal setup:

```bash
python setup.py
```

This:

- checks the Python version;
- installs `requirements.txt`;
- attempts to install Playwright Chromium and Firefox browsers;
- checks avatar assets;
- prints OS-specific notes;
- warns rather than failing the whole installation when Playwright browser downloads fail.

Safe check-only setup:

```bash
python setup.py --check
```

This does not install packages or browsers. It checks:

- supported Python compatibility;
- the presence and contents of `requirements.txt`;
- shipped avatar assets.

### 8.2 `requirements.txt`

Dependencies are grouped by purpose:

- PyQt6, audio, NumPy, and Gemini;
- browser and web search;
- automation and input control;
- vision and media;
- system and document handling;
- remote dashboard;
- Google plugin extras;
- Tuya plugin extras;
- Windows-only dependencies with platform markers;
- optional heavier packages;
- optional wake-word installation.

The wake-word package is deliberately not installed by default because it is opt-in.

### 8.3 Optional dependency policy

Optional imports are isolated. A missing optional package should disable only the relevant capability and expose a useful status/error rather than stopping the entire application.

The overall verification runner records capability limitations as skips when the environment does not provide the necessary package or hardware.

---

## 9. Wake word and audio

### 9.1 Wake word

`core/wake_word.py` manages optional local wake-word detection. The design isolates the native `openwakeword` import and model work from the main application so a broken native package cannot crash the GUI.

`check_wake_word.py` performs a safe diagnostic. It reports:

- Python version;
- package metadata status;
- readiness state;
- whether the GUI-safe check passed.

The current sandbox does not have `openwakeword` installed, which is an expected optional-capability state.

### 9.2 Audio

`core/audio_devices.py` handles named input/output device selection, reconnection, diagnostics, and fallback state.

`actions/audio_manager.py` exposes audio selection and diagnostics through the action registry.

The dashboard shows selected microphone/speaker state, connection status, host API, and sample rate.

---

## 10. Memory, voice, avatar, and AI components

### 10.1 Memory

- `memory/config_manager.py` stores configuration values.
- `memory/memory_manager.py` stores and retrieves persistent assistant memory.
- `memory/__init__.py` marks the package.

Runtime memory and credentials are intentionally excluded from Git.

### 10.2 Voice and language

- `core/gemini.py` provides model selection, fallback, and Gemini calls.
- `core/llm_client.py` contains client-level language-model interaction helpers.
- `core/stt.py` handles speech-to-text support.
- `core/tts.py` handles text-to-speech.
- `core/echo.py` helps prevent MARK LIV from responding to its own voice.
- `core/viseme.py` converts speech/text information into mouth-shape information.

### 10.3 Avatar

- `core/avatar.py` implements the animated face and rendering behavior.
- `core/avatar_mesh.py` builds and manipulates avatar geometry.
- `core/face_model.obj` contains the shipped face mesh.
- `config/jarvis.ico` contains the application icon.

The avatar is designed to render through the application UI without requiring a separate GPU-only rendering stack.

---

## 11. Complete file inventory

This section explains every source/document file currently in the repository.

### 11.1 Root files

#### `.gitignore`
Protects secrets, runtime memory, credentials, certificates, virtual environments, build files, logs, screenshots, test-result output, and Python cache files from being committed.

#### `LICENSE`
Project license text.

#### `README.md`
Canonical concise project guide. It covers installation, setup check mode, overall verification, dashboard behavior, Roblox behavior, Explorer behavior, optional capabilities, Spotify notes, and safety rules.

#### `readme.md`
Extended product and visual-design notes inherited from the broader MARK LIV product documentation. It now points readers to `README.md` for current supported behavior.

#### `requirements.txt`
Python dependency specification, grouped by core, optional, OS-specific, dashboard, document, browser, and plugin capabilities.

#### `setup.py`
OS-aware installer and asset checker. Supports normal installation and safe `--check` mode.

#### `main.py`
Primary MARK LIV application entry point. It coordinates:

- Gemini Live sessions;
- UI lifecycle;
- voice and audio streaming;
- wake word;
- action registry setup;
- plugin discovery;
- action dispatch;
- action runtime records;
- dashboard setup;
- session resumption;
- confirmation routing;
- restart/shutdown behavior;
- memory and proactive behavior.

#### `ui.py`
Main local desktop GUI. It renders the avatar, HUD, settings, confirmations, logs, status, input controls, audio controls, wake-word controls, and local control surface.

#### `check_wake_word.py`
Safe standalone diagnostic for optional wake-word readiness. It avoids importing or executing unsafe native functionality in the main GUI process.

#### `test_overall.py`
Project-wide safe verification runner. It is intentionally at the repository root so it can orchestrate the `tests/` suite without recursively discovering itself.

It checks:

- required files;
- Python compilation;
- AST parsing;
- action registry contracts;
- subprocess shell safety;
- dashboard JavaScript syntax;
- optional dashboard route construction;
- setup and requirements;
- secret hygiene;
- the complete unit-test suite;
- optional Windows integration checks.

### 11.2 `actions/`

#### `actions/README.md`
Documents the bundled action surface and explicitly lists capabilities that are intentionally not bundled.

#### `actions/audio_manager.py`
Audio device listing, selection, reconnect, and health/diagnostic actions.

#### `actions/background_monitor.py`
User-configured topic monitoring and notification behavior. It is intentionally constrained by the project’s monitoring policy.

#### `actions/browser_control.py`
Browser opening and browser interaction. It supports native browser launching and optional Playwright-based interactions.

#### `actions/computer_control.py`
Mouse, keyboard, clipboard, screenshots, and computer-level interaction helpers.

#### `actions/computer_settings.py`
System-level settings and controls, including volume, brightness, Wi-Fi, power operations, task manager, file explorer, settings, window-related operations, and confirmation-sensitive actions.

#### `actions/file_controller.py`
File and folder operations plus the integrated reliable Explorer search/open/select interface. It handles safe paths, confirmation-sensitive deletion, undo registration, exact file selection, and candidate-number selection.

#### `actions/file_processor.py`
Processing of uploaded and local files such as images, PDFs, documents, spreadsheets, JSON, archives, audio, video, and code. Optional dependencies are loaded lazily.

#### `actions/media_control.py`
Spotify Web API and Spotify Connect controls, including authentication, search, playback, pause, skip, volume, track status, and device status. It avoids falsely claiming playback when no device is available.

#### `actions/open_app.py`
Cross-platform application launcher with shortcut resolution, existing-window focusing, Roblox-specific second-instance behavior, monitor placement, and verification.

#### `actions/proactive.py`
Proactive assistant behavior and contextual check-ins.

#### `actions/reminder.py`
Cross-platform reminder scheduling through platform-native mechanisms where available.

#### `actions/screen_processor.py`
Screen and camera capture/processing. Vision dependencies are optional and imported lazily.

#### `actions/shortcut_manager.py`
Stores deterministic user shortcuts such as `gd` for Geometry Dash or other application/path aliases.

#### `actions/system_monitor.py`
CPU, memory, GPU, temperature, and system-health readings with optional dependency handling.

#### `actions/web_search.py`
Web search with provider fallback, quota handling, and structured result behavior.

#### `actions/window_manager.py`
Action-registry wrapper around `core.window_manager.py` for listing, focusing, minimizing, maximizing, moving, snapping, closing, and monitor operations.

### 11.3 `core/`

#### `core/__init__.py`
Marks the core directory as a Python package.

#### `core/action_loader.py`
Discovers action modules, validates `TOOL` declarations, exposes capability manifests, applies confirmation/admin/timeout policy, and normalizes action results.

It also avoids stale module reuse when separate directories contain files with the same module stem.

#### `core/action_result.py`
Defines the structured action-result contract. It converts legacy strings to statuses such as succeeded, failed, unavailable, cancelled, and confirmation-pending.

#### `core/action_runtime.py`
Tracks live action IDs, progress, status, cancellation events, bounded history, and event listeners.

#### `core/audio_devices.py`
Named audio input/output discovery, selection, reconnect logic, and diagnostics.

#### `core/avatar.py`
Avatar rendering, animation, state, and facial behavior.

#### `core/avatar_mesh.py`
Mesh construction, geometry, rigging, and avatar transformation helpers.

#### `core/confirm.py`
Human confirmation gate for irreversible operations. It fails closed without a bound interface and now reports cancellation, expiration, and post-confirmation failures back to the action runtime.

#### `core/echo.py`
Self-echo detection and suppression helpers so the assistant does not respond to its own speech.

#### `core/explorer.py`
Known-folder resolution, Everything search, bounded fallback search, exact-file detection, deterministic candidate ordering, Explorer opening, selection, and result formatting.

#### `core/face_model.obj`
Shipped face geometry used by the avatar.

#### `core/gemini.py`
Gemini model configuration, retries, model ladders, API interaction, and response helpers.

#### `core/hotkey.py`
Global/local keyboard shortcut and push-to-talk handling.

#### `core/installer.py`
Installation/download helpers, including optional wake-word setup and child-process isolation.

#### `core/llm_client.py`
Lower-level language-model client utilities and request handling.

#### `core/plugin_loader.py`
Plugin discovery, validation, capability exposure, timeout, and plugin execution.

#### `core/process_runner.py`
Bounded child-process execution, timeouts, output limits, and process-group cleanup.

#### `core/prompt.txt`
Base prompt and assistant behavior instructions used by MARK LIV.

#### `core/sandbox.py`
Deny-by-default validator and child-process runner for generated desktop code. It uses an AST allowlist, bounded execution, a restricted namespace, a user-home boundary, and no direct imports in generated code.

#### `core/shortcut_store.py`
Persistent deterministic shortcut storage and resolution.

#### `core/stt.py`
Speech-to-text helpers.

#### `core/tts.py`
Text-to-speech helpers and audio output behavior.

#### `core/undo.py`
Shared bounded undo stack and undoable-operation registration.

#### `core/viseme.py`
Speech/text-to-mouth-shape mapping used by the avatar lip-sync system.

#### `core/wake_word.py`
Optional wake-word lifecycle, worker isolation, readiness state, and model handling.

#### `core/window_manager.py`
Native and fallback desktop window/monitor enumeration and manipulation.

### 11.4 `dashboard/`

#### `dashboard/__init__.py`
Marks the dashboard directory as a Python package.

#### `dashboard/server.py`
FastAPI/uvicorn dashboard server. It provides authentication, pairing, encryption, command dispatch, uploads, action status, undo endpoints, desktop snapshots, capability manifests, Explorer search/open/select endpoints, and websocket updates.

It also contains platform firewall setup helpers. Windows firewall setup now avoids Python `shell=True` interpolation and validates ports.

#### `dashboard/static/app.html`
Authenticated remote dashboard UI. It contains:

- conversation feed;
- command input;
- microphone/wake controls;
- action capability list;
- live action status and cancellation;
- monitor/audio/window cards;
- undo history;
- command palette;
- Explorer search results and Open/Select controls;
- responsive mobile styling.

#### `dashboard/static/login.html`
Remote dashboard pairing/login page.

#### `dashboard/static/crypto-js.min.js`
Local CryptoJS browser dependency used for dashboard payload encryption when available.

### 11.5 `config/`

#### `config/__init__.py`
Configuration access helpers and OS configuration behavior.

#### `config/jarvis.ico`
Application icon.

Runtime secret/config files such as `api_keys.json`, Spotify tokens, OAuth credentials, dashboard certificates, and linked sessions are intentionally not committed.

### 11.6 `memory/`

#### `memory/__init__.py`
Marks the memory directory as a package.

#### `memory/config_manager.py`
Configuration file management and persisted settings.

#### `memory/memory_manager.py`
Persistent user memory storage, lookup, update, and session-related memory handling.

### 11.7 `plugins/`

#### `plugins/__init__.py`
Marks the plugin directory as a package.

#### `plugins/_template.py`
Template and example structure for creating a new plugin.

### 11.8 `tests/`

#### `tests/test_action_runtime.py`
Tests action result normalization, confirmation protection, confirmation cancellation lifecycle, headless fail-closed behavior, action cancellation, optional import availability, and timeout handling.

#### `tests/test_explorer_and_open_app.py`
Tests known-folder aliases, exact search, deterministic duplicate results, Windows Explorer argument-list selection, file-search default location, candidate-number selection, app focus behavior, process-name matching, Roblox second-instance movement, monitor verification, and honest failure reporting.

#### `tests/test_pc_actions.py`
Tests shortcut behavior and media-control authentication safety.

#### `tests/test_sandbox.py`
Tests that dangerous generated-code imports and destructive operations are rejected and that safe read-only inspection uses a child process.

#### `tests/test_wake_word.py`
Tests that wake-word readiness checks do not execute broken native packages in the main process.

#### `tests/test_window_manager.py`
Tests named-window matching, monitor snapping, monitor work-area use, and undo registration for window movement.

#### `tests/test_windows_integration.py`
Opt-in Windows integration tests for audio diagnostics, DPI-aware monitor geometry, and two-display refresh reporting. They skip outside Windows or unless explicitly enabled.

---

## 12. Verification and test commands

### Normal unit suite

```bash
python -m unittest discover -s tests -v
```

### Safe project-wide verification

```bash
python test_overall.py
```

This is non-destructive by default.

### JSON verification report

```bash
python test_overall.py --json test-results/overall-report.json
```

`test-results/` and `*.overall-report.json` are ignored by Git.

### Windows integration suite

Run on the actual Windows machine:

```bash
python test_overall.py --windows
```

This enables the hardware integration suite. It should not be treated as a substitute for manually testing Roblox and two monitors.

### Setup validation

```bash
python setup.py --check
```

### Dashboard JavaScript syntax

If Node.js is installed, the overall runner extracts inline dashboard scripts and runs `node --check` on them.

---

## 13. Latest verification result

The latest safe verification completed with:

```text
9 checks passed
0 checks failed
2 checks skipped
31 unit tests passed
3 Windows integration tests skipped
```

The skipped overall checks were:

1. **Dashboard route construction** because FastAPI/uvicorn are not installed in the current sandbox.
2. **Windows integration** because the current sandbox is Linux.

The current environment also does not provide:

- a physical Windows desktop;
- Roblox;
- two real monitors;
- Windows Known Folder runtime behavior;
- the optional wake-word package;
- full dashboard dependencies.

Those limitations are environmental, not claims that the corresponding code paths have been physically validated.

---

## 14. Security and safety decisions

### Confirmation

The model cannot confirm its own destructive action. Confirmation comes from the human UI.

### File safety

File mutation operations remain restricted to safe user paths through the existing file-controller boundaries. Explorer search/open/select is read-only except for launching the user’s file browser.

### Subprocess safety

User/model text is passed through argument lists where possible. The overall runner rejects ordinary application `subprocess` calls that use `shell=True`.

### Secrets

The repository ignores API keys, Spotify tokens, OAuth credentials, certificates, runtime memory, and related secret files. The overall runner allows protected local secret files but fails if they are unprotected.

### Generated code

Generated code runs only through the restricted sandbox path, with AST validation, limited calls, bounded child-process execution, and a home-directory boundary.

### Dashboard

The dashboard requires authentication, uses one-time pairing keys, rate limits PIN attempts, and keeps destructive control behind the same action/confirmation system rather than creating a hidden second control path.

### Honest results

The project avoids saying an action succeeded merely because a command was sent. Particularly strict verification exists for:

- second Roblox windows;
- monitor movement;
- Explorer target existence;
- confirmation outcomes;
- action runtime statuses.

---

## 15. Important implementation decisions

1. **No duplicate normal Roblox launch**
   - Normal open is idempotent.
   - Explicit second-instance behavior is separate.

2. **No guessing between files**
   - Multiple Explorer matches are listed.
   - Candidate order is deterministic.
   - A later candidate number is explicit.

3. **Registry as policy boundary**
   - Actions do not each invent their own confirmation and timeout rules.

4. **Optional capabilities remain optional**
   - Missing packages are exposed as unavailable instead of crashing startup.

5. **Dashboard and voice share actions**
   - The remote control panel does not maintain a separate unsupported action list.

6. **Safe defaults**
   - Overall testing is non-destructive.
   - Setup has a no-install check mode.
   - GUI Explorer operations are read-only.

7. **No unrelated expansion**
   - The project remains focused on useful, reliable PC control.

---

## 16. Development history

The major implementation history is:

- `74c9244` — named window control, admin panel, and safe restart foundation.
- `b0afa6f` — existing wake-word fixes merged.
- `9cf5a72` — action description formatting.
- `1180bac` — hardened action execution and generated code.
- `99352e4` — reliable controls and action administration.
- `6499026` — tracked confirmation state in live actions.
- `0116aa4` — focused actions on reliable PC control.
- `6499c7a` — application focus, Roblox instances, and Explorer search.
- `3be58ba` — file searches default to Home.
- `e0ee368` — Explorer defaults and ambiguity handling hardened.
- `db2b94d` — stable Explorer candidate selection.
- `87283d1` — expanded Explorer and launcher regression coverage.
- `21acdd9` — GUI confirmations synchronized with action status.
- `40ecb8f` — safe Explorer controls added to the dashboard.
- `9ecff7c` — project-wide verification runner, setup check, docs, and action-lifecycle hardening.
- `f4bd086` — dashboard firewall shell interpolation removed.
- `4c498ba` — action result reporting and firewall setup hardened.
- `2744f43` — subprocess-safety gate added to overall verification.
- `dfb8854` — dashboard PIN authentication rate limited.
- `a81125c` — dashboard route contract added to the overall runner.

All work is kept on the fixed branch:

```text
arena/01a0d47f-mark-liv-fixed
```

---

## 17. Remaining work

The next high-value work is a true cross-platform/dependency bug hunt rather than another broad feature expansion.

### Required next validation

- Install the full requirements set in a clean environment.
- Run `test_overall.py` with FastAPI/uvicorn installed.
- Exercise dashboard authentication and Explorer endpoints.
- Run on actual Windows.
- Test normal Roblox focusing.
- Test explicit second Roblox launch.
- Test Roblox behavior with one monitor and two monitors.
- Test Known Folder resolution against redirected Windows folders.
- Test Explorer search with Everything installed and unavailable.
- Test optional dependencies missing one at a time.
- Test fresh install and restart/reconnect behavior.

### Deep bug-hunt categories

- False success messages.
- Confirmation races and stale pending confirmations.
- Dashboard authentication and session expiry.
- Upload limits and path safety.
- File processor output paths and overwrite behavior.
- Action timeout cleanup and worker leaks.
- Window handle races.
- Audio reconnect races.
- Wake-word worker shutdown.
- Browser session cleanup.
- Plugin timeout and capability isolation.
- Configuration migration and corrupt files.
- Documentation drift against the live action registry.

### Things intentionally not planned

- Gaming mode.
- User profiles.
- Large autonomous background task systems.
- New unrelated integrations.
- A second action system outside the registry.
- Silent destructive automation.

---

## 18. Final project principle

MARK LIV should prefer:

```text
A smaller action surface that is observable, reversible, verified, and honest
```

over:

```text
A larger action surface that sometimes claims success without knowing.
```

That principle governs the launcher, Roblox handling, Explorer, dashboard, action registry, confirmation system, test runner, and the remaining bug hunt.

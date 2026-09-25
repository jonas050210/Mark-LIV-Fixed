# MARK LIV — Complete Project Record

**Repository:** `jonas050210/Mark-LIV-fixed`

**Working branch:** `arena/01a0d6a6-mark-liv-fixed`

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

The repository-wide implementation, security hardening, persistence work, regression suite, cross-platform CI, and local Session Vault are complete on the working branch.

The implementation is proposed for `main` in pull request #2. The latest local default suite passes 95 tests with five expected optional/platform skips, discovers 13 active actions, and reports 9 passed, 0 failed, and 2 skipped overall. Pull-request CI validates the same repository on Ubuntu and Windows with Python 3.11 and 3.13.

The current sandbox is Linux, so destructive Windows hardware integration, real Roblox behavior, physical multi-monitor placement, and actual audio-device behavior still require manual validation on suitable hardware. These are environmental validation limits, not unfinished repository code.

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

### 4.6 Persistence and filesystem boundaries

`core/json_store.py` provides bounded, validated, transactionally locked JSON state with atomic replacement, recovery backups, corruption quarantine, private permissions where supported, and Windows/POSIX advisory locking. Configuration, long-term memory, saved sessions, shortcuts, monitors, and Spotify tokens use this shared primitive rather than independent read/modify/write sequences.

`core/path_policy.py` provides the shared user-folder boundary, protected credential/browser paths, link and Windows reparse-point screening, bounded names and paths, fingerprints, no-replace publication, and race-aware atomic write helpers. File actions, processors, reminder scripts, dashboard uploads, and Explorer operations reuse that policy.

### 4.7 Bounded execution and trusted code

Action and plugin discovery validates schemas, source locations, file types, permissions, and links before loading trusted local Python. Execution has bounded worker capacity, deadlines, cancellation, bounded results, and sanitized diagnostics. Model-produced Python is not executed: `core/sandbox.py` rejects generated-code execution rather than presenting an AST filter as a security boundary.

---

## 4.8 Application index and launch safety

`core/app_index.py` holds the list of applications installed on the machine and
is the only component allowed to start one. It exists because the previous
launcher fell back to pressing the Windows key, typing the application name into
the Start menu, pressing Enter, and then returning `True` unconditionally. That
fallback sent keystrokes to whichever window had focus, could resolve to a web
search or a different program, and reported success for launches that never
happened.

Discovery sources, in preference order:

| Platform | Source | Covers |
| --- | --- | --- |
| Windows | `App Paths` registry keys (HKLM 64/32-bit and HKCU) | classic installers: Chrome, Firefox, VS Code, Steam |
| Windows | Start-menu `.lnk` trees under `ProgramData` and `APPDATA` | anything with a shortcut; launched through the `.lnk`, so no COM dependency |
| Windows | `Get-StartApps` AUMIDs launched via `shell:AppsFolder` | Store/UWP applications |
| macOS | `.app` bundles in the standard Applications folders | everything, launched with `open -a` |
| Linux | XDG desktop entries plus `PATH` resolution | everything with a `.desktop` file |

The index is cached in `config/app_index.json` with a 24-hour TTL, is capped at
4,000 entries, and is validated on read. A miss triggers exactly one silent
rebuild before the request is reported as a failure, which is what makes a
freshly installed application work without a manual step.

Matching is ordered: exact key, whole-name prefix/suffix, full token subset, and
only then a bounded similarity ratio above 0.62 (RapidFuzz when installed,
`difflib` otherwise — the same ordering either way, but fast enough to re-score
several thousand applications on every keystroke). The previous substring test
matched `code` inside `vscode` and `git` inside `digital`, and resolved to the
first alias in declaration order rather than the best one. When two candidates
are within 15 points of each other, MARK LIV asks which one is meant instead of
guessing.

Verification replaced the fixed `time.sleep()` calls. After a launch the caller
polls for a window owned by the spawned pid or any of its children — many
launchers hand off to a second process — and falls back to title matching. A
launch that produces no window within the budget is reported as exactly that.
Spawned `Popen` handles are reaped so a long session does not accumulate
zombies.

### Focus-dependent key combinations

`actions/computer_control.py` refuses `alt+f4`, `command+q`, `ctrl+w`,
`ctrl+shift+w`, `alt+tab`, `command+tab`, `ctrl+alt+delete`, `win+d`, `win+m`,
`win+l`, and the bare Windows/Command/Super key. Key names are canonicalised
first, so `cmd`+`Q` is caught as well as `command`+`q`. The refusal names
`window_manager` as the correct tool. This was not a code bug: the model chose
the generic hotkey tool over the window tool, and the resulting `alt+f4` closed
whichever application had focus rather than the one the user named. The guard
runs before the PyAutoGUI availability check, so a refusal is an explanation
rather than a missing-dependency error.

### Launcher usage, icons, and arguments

`config/app_usage.json` records pinned applications, a bounded recent list, and
launch counts; `quick_list()` resolves those names against the live index and
drops entries that no longer exist, so an uninstalled application cannot linger
as a dead button. Cache staleness is decided by a signature over the discovery
folders' modification times as well as the 24-hour TTL, which makes a newly
installed application appear immediately.

`core/app_icons.py` extracts 64×64 PNG icons — `ExtractAssociatedIcon` through
PowerShell on Windows, `.icns` from the bundle on macOS, XDG icon themes on
Linux — and caches them under `config/app_icons` keyed by a digest of the launch
target. Every path degrades to "no icon" rather than raising, because an icon
must never be able to break a launch.

`sanitise_arguments()` allows documents and URLs to be passed to an application
as a real argv list. Arguments beginning with a dash are rejected, so a model
cannot smuggle switches such as `--remote-debugging-port` into a browser launch,
and shortcut/Store launches that cannot carry arguments say so instead of
silently dropping them.

A successful launch pushes an undo entry that closes the specific window handle
it produced, and refuses when that window is already gone.

### Window layouts

`actions/layout_manager.py` saves and restores named arrangements. A layout
records each window's process, title fragment, monitor index, geometry and state
— never a window handle, which would not survive a restart. Shell windows such
as `explorer.exe` are skipped so a restore does not fight the desktop. Save,
apply and delete are all undoable, and applying reports the windows that are not
currently open rather than launching them.

### Reminder registry

`actions/reminder.py` hands each reminder to the operating system's own
scheduler — Task Scheduler, launchd, `systemd-run`, or `at` — which is what lets
it fire while MARK LIV is closed. The cost is that the assistant used to have no
idea what it had scheduled: a reminder could only be removed by opening the
scheduler by hand. `memory/reminders.json` now records the scheduler handle, the
time, and the message for each job, which is exactly what `cancel` needs and
nothing more.

- `action: list` prints the pending reminders, numbered, and prunes the ones
  whose time has passed by more than a minute.
- `action: cancel` takes that number or the exact message text; with a single
  reminder pending, neither is needed.
- A cancellation the scheduler refuses is reported as a failure, because the
  job is still registered and will still fire. Nothing is removed from the
  registry in that case.
- Both `set` and `cancel` are undoable. Cancelling otherwise destroys the
  scheduler job and the registry entry together, so "no, not that one" would
  mean dictating the date, the time and the message again; the undo
  re-schedules exactly what was removed, and refuses when that time has since
  passed.
- `at` is the one backend that can schedule a job it cannot describe: if it
  prints no job number, the reminder is still set, and the answer says plainly
  that it will not be cancellable.

### file_processor split into handlers

`actions/file_processor.py` was 1626 lines covering eleven unrelated formats,
so a change to video trimming was made in the same file as PDF extraction and
zip-bomb defence. The action keeps its name, its tool declaration and its
dispatcher — 250 lines — and the work moved into `actions/file_handlers/`:

| Module | Formats |
| --- | --- |
| `common.py` | path validation, stable-input snapshot, parameter coercion, output naming, size limits |
| `images.py` | describe, OCR, resize, convert, compress, crop |
| `documents.py` | PDF, Word, text and Markdown, PowerPoint |
| `data.py` | CSV and spreadsheets, JSON, XML |
| `code.py` | explain, review, fix, run, document |
| `media.py` | audio and video |
| `archives.py` | zip and tar, with the traversal and expansion guards |

Every import is explicit rather than a star import, so it is visible at the top
of each module which shared helpers it depends on. The action loader globs
`actions/*.py`, so the package directory is not mistaken for an action.

### One loop for recurring jobs

The topic monitor and the proactive check-in each ran their own `while True`
loop in `main.py`, and each re-implemented the question "may I speak right
now?" — with different answers: the monitor accepted 30 seconds of silence, the
check-in used its own gate and cooldown.

`core/background_scheduler.py` now owns the timing and the gate.
`JarvisLive._may_interrupt()` is the single definition of when a background job
may talk (live session, awake, not mid-sentence, 30 s since the user spoke), and
each job is a coroutine with an interval. A job that fails backs off
exponentially to at most eight intervals instead of repeating the same failure
every tick, and one failing job cannot stop the others.

Reminders stay outside this loop on purpose: they are registered with the
operating system's scheduler so that they fire while MARK LIV is closed, which
no in-process loop can do.

### Dashboard authentication

Every route under `/api` and `/uploads` requires a bearer token; a test walks
the route table and fails if a new endpoint appears without one. The generated
OpenAPI schema is disabled along with the interactive docs, so an
unauthenticated caller on the network cannot read the list of routes.

`POST /api/revoke-devices` ends **every** session, not just the remembered
devices: a phone that is already signed in holds its bearer token in memory,
and clearing only the device records left that token working while telling the
user access had been revoked. The session issuing the request keeps working,
open websockets are closed, and both counts are reported.

A request that arrives before the assistant has connected its action registry
answers `503` with "MARK LIV is not ready yet" rather than `500` with an
exception name: the dashboard is reachable during start-up, and that is not a
fault.

### Two browsers, one page

MARK LIV can put a web page on screen in two ways and they used to be unaware
of each other. Navigation (`browser_control` `go_to`/`search`/`new_tab`, and
`open_app` with a URL argument) opens the user's real browser — their profile,
their logins. The interactive actions (`click`, `type`, `get_text`,
`smart_click`) need a browser Playwright can drive, which is a separate window.

`core/browser_handoff.py` is the single line of shared state between them: the
last page opened natively, recorded by both surfaces and consumed once by the
automation window. "Open the BBC" followed by "click the top story" therefore
lands on the BBC rather than on `about:blank`.

When there is nothing to resume and the automation window has only just been
created, the action stops and says there is no page open yet, instead of
searching a blank page and blaming the element for not existing.

### Panels outside ui.py

`ui.py` was 5600 lines, and every panel added to it made the next one harder to
place. All floating panels now live in `ui_panels/`; `ui.py` is down to 4170
lines and only learns how to open them.

| Module | Panel |
| --- | --- |
| `ui_panels/base.py` | `HudPanel` base (ghost-frame repaint), palette proxy, shared widget styling |
| `ui_panels/launcher.py` | search the application index, pin, rescan, launch |
| `ui_panels/layouts.py` | save, restore and delete window layouts |
| `ui_panels/setup.py` | first-run API key entry |
| `ui_panels/customize.py` | accent colour wheel, avatar and identity |
| `ui_panels/plugins.py` | plugin manager and plugin settings |
| `ui_panels/confirm.py` | the confirmation gate for irreversible actions |
| `ui_panels/audio_devices.py` | microphone and speaker selection |
| `ui_panels/memory.py` | browse, search and forget memory entries |
| `ui_panels/remote_key.py` | dashboard pairing, QR code and PIN |

The palette is reached through a proxy that resolves `ui.C` at attribute-access
time rather than importing it, because `ui` imports this package; that also
means a live theme change is picked up the next time a panel is built. A widget
test constructs all of them, because a panel that fails to build is otherwise
noticed only when a user clicks the button that opens it.

The palette is read from `ui.C` at call time rather than imported, because
`ui` imports these modules; the indirection also means a live theme change is
picked up the next time a panel opens. Both panels call the same actions the
voice path uses (`actions.open_app`, `actions.layout_manager`), so a click and a
spoken command share one implementation, one store, and one undo entry — and
both panels print the action's own sentence rather than deciding for themselves
that the operation worked.

### Placement

`core/window_manager.place_window()` moves a window to a monitor and applies a
state (`normal`, `maximized`, `fullscreen`, `minimized`, or a snap side), then
re-reads the window so the result can be verified rather than assumed.
`open_app` exposes this through `monitor` and `state` parameters, and a
`foreground` parameter that records the previously focused window before the
launch and restores it afterwards.

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
- cross-platform enumeration through `pywinctl`, falling back to
  `pygetwindow` and then to `wmctrl`; `pywinctl` also reports the owning
  process id, so a window can be tied to the process MARK LIV started;
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

### 7.2 Dashboard improvements in the hardened implementation

The hardened dashboard includes:

- dedicated Explorer search/open/select controls;
- quote-safe HTML escaping for dynamic values;
- safer Windows firewall setup without Python `shell=True` calls;
- rate-limited PIN login and bounded authentication/session stores;
- bounded request bodies, uploads, messages, actions, and asynchronous tasks;
- authenticated WebSockets and command payloads;
- bounded, reparse-safe file access and no-replace upload publication;
- cancellation and lifecycle updates shared with the main action runtime;
- route, authentication, upload, task, and path-safety regression tests.

### 7.3 Dashboard security

- API endpoints and WebSockets require an authenticated bearer session.
- Pairing keys are short-lived, one-time use, capped, and actively expired.
- Login attempts are rate limited by peer address.
- Persistent device sessions are bounded, expire, and can be revoked.
- Command payloads are authenticated before decryption.
- Dynamic dashboard values are HTML escaped, including quotes and apostrophes.
- File Explorer endpoints do not accept shell commands.
- A per-install TLS certificate protects LAN access. If TLS initialization fails, plain HTTP binds only to loopback rather than exposing an unencrypted LAN service.
- The local CryptoJS asset is SHA-256 pinned and served only when its bytes match the expected digest.
- Firewall setup uses a generated batch file only for the OS-level operation and invokes it through `cmd.exe` with `shell=False` from Python.
- The dashboard remains a local/LAN control surface and is not intended for direct public-Internet exposure.

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

- `memory/config_manager.py` validates, bounds, redacts, and transactionally stores configuration values.
- `memory/memory_manager.py` stores durable facts, builds a bounded prompt core/index, supports lexical recall, and acknowledges automatic startup summaries without losing newer data.
- `memory/session_store.py` stores up to 20 explicitly named conversation bookmarks with at most 40 sanitized turns each. It supports save/update, list, unambiguous lookup, bounded resume context, and confirmed deletion.
- `memory/__init__.py` marks the package.

All persistent memory uses `core/json_store.py`, so concurrent updates do not silently overwrite unrelated fields and corrupt primaries can recover from a validated backup. Saved sessions are separate from provider resumption handles: resuming a bookmark drops the opaque handle, opens a fresh Live connection, and injects only bounded historical context as a narration-only turn. Runtime memory, session snapshots, sidecars, and credentials are intentionally excluded from Git.

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

#### `.gitattributes`
Keeps the integrity-pinned CryptoJS vendor asset byte-for-byte stable across operating systems.

#### `.github/workflows/verify.yml`
Pinned GitHub Actions workflow covering Ubuntu and Windows on Python 3.11 and 3.13. Every job validates setup inputs, compiles the repository, runs all unit tests, constructs the dependency-aware dashboard, and runs overall verification.

#### `.github/scripts/run_tests.py`
Cross-platform unit-test runner that emits concise GitHub annotations and step summaries when a test fails.

#### `.github/scripts/run_overall.py`
Cross-platform wrapper that preserves the overall verifier's report and exposes failed gates as GitHub annotations and step summaries.

#### `.gitignore`
Protects secrets, runtime memory, credentials, private-store sidecars, certificates, virtual environments, build files, logs, screenshots, test-result output, and Python cache files from being committed.

#### `LICENSE`
Project license text.

#### `Project.md`
This engineering record: current architecture, security decisions, complete tracked-file inventory, verification evidence, development history, and physical-validation limits.

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

#### `actions/session_manager.py`
Action-registry interface for explicitly saving, listing, resuming, and deleting private local conversation snapshots. Saving receives a copy of the completed-turn transcript; deletion is confirmation-protected.

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
Discovers action modules, validates strict `TOOL` schemas and trusted source files, exposes capability manifests, applies confirmation/admin/deadline/cancellation policy, bounds worker capacity, and normalizes bounded action results.

It also avoids stale module reuse when separate directories contain files with the same module stem, screens symbolic links/reparse points and unsafe permissions, and reports sanitized load failures.

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

#### `core/json_store.py`
Shared bounded JSON transaction layer with process-local and cross-process locking, strict validation, atomic replacement, private temporary files, validated backups, corruption quarantine, and recovery.

#### `core/llm_client.py`
Lower-level language-model client utilities with bounded requests, loopback/private endpoint policy, response validation, and sanitized failures.

#### `core/path_policy.py`
Shared filesystem boundary for user-directed paths, protected roots, names, links/reparse points, fingerprints, atomic writes, and no-replace moves/publication.

#### `core/plugin_loader.py`
Trusted local plugin discovery, strict schema and source validation, capability exposure, bounded worker capacity, deadlines, cancellation, dependency reporting, and sanitized plugin execution failures.

#### `core/process_runner.py`
Bounded child-process execution with process-group creation, cooperative cancellation, timeout escalation, output-tail limits, and deterministic cleanup.

#### `core/prompt.txt`
Base prompt and assistant behavior instructions used by MARK LIV.

#### `core/sandbox.py`
Fail-closed compatibility boundary for the former generated-code feature. Direct execution of model-produced source is disabled, including code that appears read-only; supported work must use a reviewed action or trusted local plugin instead.

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
FastAPI/uvicorn dashboard server. It provides bounded authentication and pairing state, authenticated encryption, TLS, command dispatch, uploads, action status, undo endpoints, desktop snapshots, capability manifests, Explorer search/open/select endpoints, authenticated WebSockets, and tracked asynchronous tasks.

Its file access reuses the shared path policy, uploads are bounded and published without replacement, and LAN exposure fails closed when TLS is unavailable. Platform firewall setup avoids Python `shell=True` interpolation and validates ports.

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
Local CryptoJS browser dependency used for dashboard payload encryption. Its exact SHA-256 digest is pinned by the server, and `.gitattributes` prevents line-ending conversion from changing its bytes.

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
Validated transactional configuration storage with bounded display names, atomic patches, private files, and sanitized diagnostics.

#### `memory/memory_manager.py`
Validated transactional long-term memory with bounded values, prompt-core and index budgets, session-summary save/peek/acknowledge behavior, and safe corruption recovery.

#### `memory/session_store.py`
Private bounded Session Vault with unique opaque IDs, named upserts, sanitized role-labelled turns, unambiguous lookup, no silent eviction, and injection-labelled resume context.

### 11.7 `plugins/`

#### `plugins/__init__.py`
Marks the plugin directory as a package.

#### `plugins/_template.py`
Template and example structure for creating a new plugin.

### 11.8 `tests/`

#### `tests/test_action_policy.py`
Tests trusted action loading, strict schemas, confirmation expiry and race handling, bounded web-search workers, reminder storage, process cancellation, process-group escalation, and bounded output tails.

#### `tests/test_action_runtime.py`
Tests result normalization and size bounds, confirmation protection and lifecycle, headless fail-closed behavior, deadlines, cancellation, listener isolation, bounded workers, source validation, and unavailable optional imports.

#### `tests/test_dashboard_safety.py`
Tests authentication, body/upload limits, encrypted command validation, bounded task tracking, safe file access, action cancellation, and fail-closed dashboard behavior.

#### `tests/test_explorer_and_open_app.py`
Tests known-folder aliases, exact and deterministic search, Windows Explorer argument-list selection, file-search defaults, explicit candidate selection, app focusing, process matching, Roblox second-instance movement, monitor verification, and honest failure reporting.

#### `tests/test_filesystem_safety.py`
Tests protected paths, symbolic-link rejection, no-overwrite mutation, stable parser snapshots, generated-output races, archive traversal/link/bomb rejection, bounded extraction, and cross-platform atomic writes.

#### `tests/test_pc_actions.py`
Tests deterministic shortcuts and media-control authentication safety.

#### `tests/test_persistence_safety.py`
Tests concurrent JSON/config updates, corruption recovery, backups, non-finite values, size bounds, private permissions, platforms without `fchmod`, memory prompt bounds, and transactional session acknowledgement.

#### `tests/test_plugin_safety.py`
Tests trusted plugin sources, schema validation, sanitized dependency reporting, execution deadlines, cancellation, worker limits, and fail-fast behavior.

#### `tests/test_sandbox.py`
Tests that generated-code execution is disabled, including source that appears read-only.

#### `tests/test_session_store.py`
Tests session save/update/list/resume/delete behavior, transcript and context bounds, malformed-primary recovery, selector ambiguity, capacity refusal, explicit deletion confirmation metadata, and empty-session rejection.

#### `tests/test_wake_word.py`
Tests that wake-word readiness checks do not execute broken native packages in the main process.

#### `tests/test_window_manager.py`
Tests named-window matching, monitor snapping, monitor work-area use, and undo registration for window movement.

#### `tests/test_windows_integration.py`
Opt-in Windows hardware tests for audio diagnostics, DPI-aware monitor geometry, and two-display refresh reporting. They skip outside Windows or unless explicitly enabled.

---

## 12. Verification and test commands

```bash
python test_overall.py              # ten gates, including the unit suite
python test_overall.py --coverage   # also measure coverage and enforce the floors
python test_overall.py --windows    # add the Windows hardware checks (Windows only)
python -m unittest discover -s tests
```

### Coverage floors

A single total percentage would say nothing useful here: the number is
dominated by the GUI and by platform branches that cannot execute on the
machine running the suite. What is gated instead is a per-module floor on the
fifteen modules that decide what MARK LIV is allowed to do — the JSON store,
the path policy, the action runtime, the launcher index, undo, the confirmation
gate, the session store, and the scheduler among them. `--coverage` fails the
run when any of them drops below its floor, which is how `core/app_index.py`
was found sitting at 48%.

### What cannot be verified here

The Windows-only paths — registry scanning, `.lnk` resolution through `pylnk3`,
icon extraction, Task Scheduler, WMI brightness, `user32` window handles — have
no equivalent on Linux or macOS. `tests/test_windows_integration.py` covers
them and runs on the `windows-latest` CI runner, where it indexes the real
machine, launches Notepad and checks the pid, extracts a real icon, and
schedules, lists and cancels a real reminder.

## 13. Latest verification result

Local default run:

```text
338 unit tests run, 38 guarded skips
9 overall checks passed, 0 failed, 3 skipped
```

With the optional tooling installed (`coverage`, `PyQt6`, `rapidfuzz`, FastAPI):

```text
338 unit tests run, 5 guarded skips
10 overall checks passed, 0 failed, 2 skipped
15 safety-critical modules at or above their coverage floor
```

The skips in the default run are the panel tests, which need a Qt platform
plugin, the dashboard route contract, which needs FastAPI, the coverage gate,
which needs `coverage`, and the Windows hardware suite.

### Continuous integration

Six jobs: Ubuntu, Windows and macOS against Python 3.11 and 3.13. Each runs
setup validation, compilation, the full unit suite, and the overall
verification with `--coverage`; the Linux jobs install the Qt runtime so the
panel tests execute rather than skip. The Windows jobs additionally run
`tests/test_windows_integration.py`, and every job uploads its JSON report as
an artifact.

A GitHub Windows runner has no desktop — it executes in session 0 — so the
tests that need one skip themselves there. What does run on every Windows
build: the registry scan, a check that every indexed executable exists on disk,
`.lnk` resolution through `pylnk3`, the refusal of slash-style switches, the
native `user32` window backend, and a real Task Scheduler reminder being
created, listed and cancelled.

### What is still unverified

No physical Windows desktop with two monitors, no Roblox, no real audio
hardware, no Store applications, and no wake-word model. Those are
environmental limits, and nothing in this document should be read as a claim
that they have been exercised.

---

## 14. Security and safety decisions

### Confirmation

The model cannot confirm its own destructive action. Confirmation comes from the human UI.

### File safety

File reads and mutations use a shared home-folder boundary, protected credential/browser roots, link and Windows reparse-point checks, bounded paths/names, no-replace publication, and race-aware fingerprints. Parser inputs use private stable snapshots, archive extraction is bounded and staged, and generated outputs do not silently replace an existing destination. Explorer search/open/select remains read-only except for launching the user’s file browser.

### Persistence

Configuration, long-term memory, saved sessions, shortcuts, monitor state, and Spotify tokens use bounded transactional JSON stores. Writes are locked, validated, flushed, atomically replaced, recoverable from a last-known-good backup, and private where the platform supports descriptor permissions. Corruption diagnostics are sanitized.

### Subprocess and network safety

User/model text is passed through argument lists rather than shell interpolation. Process execution has timeouts, bounded output, process-group cancellation, and escalation. The overall runner rejects application `shell=True` calls, and network requests use explicit timeouts and bounded responses.

### Secrets

The repository ignores API keys, Spotify tokens, OAuth credentials, certificates, runtime memory, and private-store sidecars. The overall runner permits protected local secret files but fails if it finds an unprotected secret-like file.

### Generated and plugin code

Model-produced source code is not executed. Plugins remain executable Python and are therefore trusted local code; discovery validates regular source files, links/reparse points, permissions, schemas, dependencies, and execution bounds before advertising a capability.

### Dashboard

The dashboard uses TLS for LAN access, expiring bearer sessions, one-time pairing keys, login throttling, authenticated encrypted commands, bounded uploads/tasks/state, authenticated WebSockets, and the same action/confirmation system as voice and local UI. Without usable TLS it binds plain HTTP to loopback only.

### Honest results

The project avoids saying an action succeeded merely because a command was sent. Particularly strict verification exists for:

- second Roblox windows;
- monitor movement;
- Explorer target existence;
- confirmation outcomes;
- action runtime statuses.

---

## 15. Important implementation decisions

### Removed sub-actions and why

An action is a liability when it answers confidently from nothing, or when a
second tool already does the same job properly. Five sub-actions were removed
for those reasons. None of them fails silently: each returns a sentence naming
the tool that replaces it, so the model recovers within the same turn instead of
reporting an unknown action to the user.

| Removed | Reason | Use instead |
| --- | --- | --- |
| `web_search.price` | Prices came from the model, not from the search results, and were stated with full confidence while being months out of date. | `web_search.search` — a price question is an ordinary search, answered from what the sources actually say. |
| `web_search.compare` | Same failure, one step worse: two invented specification sheets set side by side read as research. | `web_search.research` |
| `computer_control.screen_find` | Screenshot to the vision model to a pixel coordinate: seconds per call, and wrong whenever a theme, scale factor, or scroll position changed. | `browser_control.smart_click`, which addresses elements through the DOM. |
| `computer_control.screen_click` | The same guess, but it clicked on it. A wrong coordinate is not a failed action, it is an action performed on the wrong thing. | `browser_control.smart_click` |
| `computer_control.focus_window` | Duplicate of `window_manager.focus`, with a weaker title match and no monitor awareness. Two tools for one job means the model picks the worse one half the time. | `window_manager` with `action: focus` |
| `file_controller.organize_desktop` | Moved every desktop file into six category folders in a single step. It was reversible through the undo journal, but a user who cannot see where anything went does not know to ask for undo. | Ask for specific files to be moved. |



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

7. **Transactional state instead of ad-hoc JSON writes**
   - Persistent updates are validated, locked, bounded, atomic, and recoverable.

8. **No model-produced code execution**
   - New behavior must be implemented as a reviewed action or trusted local plugin.

9. **Facts, summaries, and saved sessions are separate**
   - Durable facts remain searchable across every conversation.
   - The automatic prior-session summary is consumed by the next briefing.
   - Explicit bookmarks are user-named, bounded, locally stored snapshots that can be resumed or deleted independently.

10. **No unrelated expansion**
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
- `70c62dd` — repository-wide runtime, persistence, filesystem, dashboard, plugin, and action hardening.
- `f4e94f4` — pinned cross-platform verification workflow added.
- `19d0b60` — Windows-console UTF-8 portability and current pinned workflow actions.
- `dabcdfb` — concise CI unit-test annotations and summaries.
- `0036df4` — Windows persistence portability, descriptor cleanup, overall diagnostics, and final cross-platform fixes.
- `9fab83e` — complete engineering record synchronized with the hardened implementation.

All current work is kept on the fixed branch:

```text
arena/01a0d6a6-mark-liv-fixed
```

---

## 17. Remaining work

No known repository implementation blocker remains for pull request #2. Automated Linux/Windows CI, dashboard-aware verification, adversarial persistence/filesystem tests, and the repository-wide hardening pass are complete.

### Optional physical validation

The following checks require the target machine or user accounts and cannot be proven in this sandbox:

- run `python test_overall.py --windows` on the intended Windows computer;
- verify microphone/speaker selection and reconnect behavior with real devices;
- test normal Roblox focusing and an explicitly requested second instance;
- test Roblox placement with one physical monitor and with two physical monitors;
- verify redirected Windows Known Folders and Everything search on the target profile;
- exercise wake-word setup with the optional native package installed;
- perform a fresh full-requirements installation and a manual restart/reconnect smoke test.

These checks may reveal hardware, driver, account, or application-specific behavior, but they are not hidden claims that the automated suite has already exercised physical devices.

### Future maintenance

- Keep action schemas, documentation, and runtime capabilities synchronized.
- Review dependency bounds and pinned GitHub actions periodically.
- Add regression tests before changing filesystem, authentication, confirmation, persistence, or worker-limit policy.
- Preserve the bounded, observable, reversible action model when adding a new capability.

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

That principle governs the launcher, Roblox handling, Explorer, persistence, dashboard, action and plugin registries, confirmation system, and verification pipeline.

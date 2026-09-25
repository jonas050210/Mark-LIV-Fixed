# MARK LIV — Project Documentation

> Created as an explicit, one-off exception to this project's standing rule of
> not creating or modifying Markdown files. The user asked by exact filename
> for a document covering the whole project; everything else about the
> "no Markdown" rule still applies (this file is the only intentional
> exception, and no other `.md` file was touched to produce it).

This document describes what MARK LIV is, how it is built, what changed
across the three improvement rounds performed in this workspace, what the
test suite actually proves, and what is honestly still unverified.

## 1. What this project is

MARK LIV is a cross-platform, JARVIS-style desktop voice/text assistant with:

- an LLM-driven action dispatcher (Ollama or an OpenAI-compatible local
  server, or Gemini via `google-genai`) that turns natural-language requests
  into calls against a fixed set of "tools" (the `actions/` modules);
- local speech recognition (`core/stt.py`) and text-to-speech
  (`core/tts.py`, Kokoro), plus an optional local "Hey Jarvis" wake word;
- a software-rendered holographic avatar (`core/avatar.py`,
  `core/avatar_mesh.py`, PyQt6 + NumPy, no GPU/OpenGL dependency);
- a local web dashboard (`dashboard/server.py`, FastAPI) for pairing a phone
  or browser to the same session, with its own auth/session/upload security
  model;
- deep desktop control on Windows first (window/monitor management, app
  launching via an indexed catalog of installed software, file operations,
  system settings, audio device selection, reminders via the OS scheduler),
  with Linux/macOS fallbacks kept correct wherever the underlying OS API
  allows it.

Everything reversible is undoable through one shared stack
(`core/undo.py`): an action captures the "before" state and hands back a
zero-argument closure that restores it, and the user can say "undo" instead
of the assistant asking "are you sure?" before every change. The handful of
actions that are not reversible (e.g. permanently deleting instead of
trashing) go through an explicit human-confirmation gate instead
(`core/confirm.py`).

## 2. Architecture

```
main.py                    entry point (Qt event loop, wires everything together)
ui.py                      Qt UI (chat window, avatar host)
setup.py                   dependency install / --check (offline validation)
test_overall.py            convenience wrapper around .github/scripts/run_overall.py

actions/                   one file per LLM-callable "tool" (see §3)
core/                       shared infrastructure, not directly LLM-callable:
  action_loader.py            discovers actions/*.py, validates each TOOL schema
  action_runtime.py           dispatches a validated tool call to its handler
  action_result.py            the normalized {ok, status, message, data} shape
  undo.py                     the shared undo stack described above
  confirm.py                  human-confirmation gate for irreversible actions
  window_manager.py           cross-platform window/monitor enumeration & control
  app_index.py                 the indexed catalog of installed applications
  audio_devices.py             JARVIS's OWN mic/speaker session (not system-wide)
  system_audio.py              NEW (round 3): system-wide default playback device
  text_match.py                 shared fuzzy string matching (RapidFuzz or difflib)
  llm_client.py / gemini.py     the two supported model backends
  stt.py / tts.py / wake_word.py   speech pipeline
  json_store.py                atomic, corruption-tolerant JSON persistence
  path_policy.py / sandbox.py / untrusted.py   filesystem safety primitives
  background_scheduler.py       retry/backoff scheduler for reminders etc.
dashboard/server.py         FastAPI app: pairing, auth, session revocation,
                             file upload, WebSocket bridge to the desktop app
memory/                     config_manager.py (API keys, device prefs),
                             memory_manager.py, session_store.py — all through
                             core/json_store.py for atomic writes
tests/                      35 files, 570 tests (see §5)
```

Design principles that recur throughout the codebase and were preserved in
every round of this work:

- **Honesty over optimism.** A command that the OS refused must return a
  failing/negative result, never a cheerful "Done" — this is enforced with
  dedicated regression tests almost everywhere (e.g. `volume_set`,
  `brightness_set`, `set_default_playback_device` all return `False`/an
  explicit failure message on a non-zero exit code or a raised exception,
  never a guess).
- **Undo instead of confirmation** for anything reversible; confirmation only
  for the genuinely irreversible.
- **Never silently execute a look-alike.** Fuzzy matching (app names, window
  titles, audio device names) always has a minimum-score floor and reports
  what it actually found when nothing crosses it, instead of guessing.
- **Windows is the primary target**, but nothing runs a Windows-only branch
  by accident: every platform branch is behind `platform.system()` and has a
  real (not stubbed) Linux/macOS behaviour where the OS provides an
  equivalent primitive.

## 3. Actions (LLM-callable tools)

18 modules currently expose a `TOOL` schema (validated and loaded by
`core/action_loader.py`); 4 further modules (`background_monitor.py`,
`proactive.py`, `screen_processor.py`, `system_monitor.py`) are internal
support code with no `TOOL`, so they are not directly callable.

| Action | Purpose |
|---|---|
| `open_app` | Opens/focuses an application, game, folder, or URL from the indexed catalog. Handles `.lnk` shortcuts, fullscreen/left/right/monitor placement, and saved-shortcut aliases. |
| `app_sequence` | Runs several steps for one command sequentially — app launches and/or media-control steps, one at a time, each verified, with retry-then-continue on failure. |
| `app_lifecycle` | Checks install/running state, diagnoses launch type and windows, restarts an app. |
| `app_catalog` | Lists or rescans the installed-application index. |
| `audio_manager` | Lists/selects JARVIS's own microphone and speakers, **and** (round 3) lists/switches the whole system's default playback device. |
| `browser_control` | Full browser automation: navigate, search, click, fill forms, scroll, screenshot. |
| `computer_control` | In-focus-window control: type, click, hotkeys, scroll, mouse, screenshot. |
| `computer_settings` | System volume/brightness/mute/sleep/lock/Wi-Fi. |
| `desktop_health` | Read-only diagnostic snapshot (monitors, window backend, app index size, saved shortcuts) — safe to call before retrying a failing multi-step command. |
| `file_controller` | Explorer/file operations: open, reveal, search, list, create, move, copy, delete (via Trash), with cross-drive move safety and rollback. |
| `file_processor` | Processes a file the user uploaded/dropped onto the dashboard. |
| `layout_manager` | Saves/restores named desktop window layouts. |
| `media_control` | Spotify Connect playback control (play/pause/next/previous/volume/seek/shuffle/repeat) without needing the visible Spotify window. |
| `reminder` | Timed reminders registered with the OS scheduler (fire even if MARK LIV isn't running). |
| `session_manager` | Local conversation bookmarks. |
| `shortcut_manager` | User-defined deterministic aliases ("Roblox" → a specific `.lnk`, etc.). |
| `web_search` | Web search / news / other lookup modes. |
| `window_manager` | Named window and monitor control: focus, move, snap, minimize, list windows/monitors. |

## 4. What changed, round by round

### Round 1 (baseline hardening)
Sequential multi-app command handling, semantic monitor references
(primary/main/secondary/second, numeric index, left/right), `.lnk` shortcut
launching, `desktop_health`, and a first broad bug-hunt pass. Result at the
end of round 1: 513 tests, 0 failures, 25 skipped; 31% coverage.

### Round 2 (open_app / file_controller / app_sequence overhaul)
Substantially rewrote `open_app.py` (launch-type diagnosis, fullscreen/side
placement, saved-shortcut integration, stale-process detection) and
`file_controller.py` (cross-device move with verified copy + rollback,
directory-mutation safety checks, Explorer integration), and added
`app_sequence.py`'s media-step support (a sequence can mix opening
applications and controlling Spotify in the same ordered plan) with a shared
retry-then-continue helper. `tests/test_app_launcher.py` and
`tests/test_filesystem_safety.py` grew substantially to cover the new
branches. Explicitly rejected: preset workspace "profiles" (asked for
repeatedly, refused every time — the user does not want fixed
Gaming/Work/Music presets).

### Round 3 (this pass): close the last known gap, document, deep bug hunt

**1. System-wide default output device switching — the gap identified at the
end of round 2.** `audio_manager.py` previously only switched *JARVIS's own*
microphone/speaker session (`core/audio_devices.py`, a PortAudio/`sounddevice`
concept); it had no way to change what the *whole OS* plays sound through,
which is what most people mean by "switch my audio output."

New module `core/system_audio.py`:

- `list_playback_devices()` / `get_default_playback_device()` /
  `set_default_playback_device(name)` — cross-platform, best-effort:
  - **Windows**: enumerates via `pycaw.pycaw.AudioUtilities.GetAllDevices()`
    (filtering to active endpoints), and switches the system default through
    the same **undocumented `IPolicyConfig` COM interface** the Settings app
    and classic Sound control panel themselves use (there is no public Win32
    API for this). Because it is undocumented, its exact vtable layout has
    drifted across Windows releases in community reverse-engineering; two
    known-working `(IID, CLSID, vtable-slot-count)` layouts are tried in
    order (the common Windows 7+ layout, then the older Vista-era layout),
    and `SetDefaultEndpoint` is called for all three roles (console,
    multimedia, communications) so every application picks up the change
    consistently.
  - **Linux**: `pactl list short sinks` / `pactl set-default-sink` (PulseAudio/PipeWire).
  - **macOS**: `SwitchAudioSource` (a widely-installed third-party CLI; there
    is no built-in command-line way to do this either).
  - A spoken/typed device name is matched against the real device list with
    `core.text_match.partial_ratio` (the same tool `window_manager.py` and
    `layout_manager.py` already use for "short spoken fragment vs. long real
    name" matching, e.g. "jbl" → "JBL Quantum 400 Wireless"), never guessed
    outright, and a failed OS/COM call is always reported as a failure —
    never assumed to have worked.
- Wired into `audio_manager.py` as two new actions, `list_system_outputs` and
  `set_system_output`, clearly distinguished in the tool description from
  the pre-existing `set_input`/`set_output` (JARVIS's own session). A
  successful switch registers an undo entry that restores the previous
  system default, consistent with every other reversible action in the
  project.
- **Honesty about verifiability**: this Windows COM path cannot be executed
  or verified on this Linux sandbox — only unit-tested via mocked
  `pycaw`/`comtypes` modules (`tests/test_system_audio.py`, 18 tests) that
  prove the *Python-level control flow* (which functions are called, with
  what arguments, in what order, and that any exception becomes an honest
  failure) is correct. They cannot prove the real Windows COM call behaves
  as the reverse-engineered documentation says. This is the same limitation
  every other Windows-only code path in this project already has (see
  `tests/test_windows_paths_simulated.py`'s own stated purpose), and is
  disclosed here rather than glossed over.

**2. Closed a pre-existing test-coverage gap found while implementing the
above**: `computer_settings.py`'s Windows/`pycaw` `volume_get()`/`volume_set()`
path had **zero test coverage** anywhere in the suite (confirmed by grep
before this round — nothing under `tests/` referenced `pycaw` or
`AudioUtilities`). Six regression tests were added to
`tests/test_windows_paths_simulated.py::WindowsSettingsTests` using the same
fake-module-injection technique, covering: percentage↔decibel conversion in
both directions, the −65.25 dB silence floor (not `log10(0)`), and that a
missing `pycaw` install is a reported failure, not a silent one.

**3. Deep bug hunt.** Findings and outcome:

- **Fixed — environment-coupled test.**
  `tests/test_filesystem_safety.py::CrossDeviceMoveTests::test_move_across_devices_rolls_back_if_the_original_cannot_be_trashed`
  asserted `self.assertFalse(file_controller._SEND2TRASH)` as a
  *precondition*, i.e. it only worked because the `send2trash` package
  happened not to be installed in whatever environment ran the suite. The
  moment it was installed (which happened in this very session, verifying
  round 3's other dependency-driven skips), the test failed — not because
  any product code was wrong, but because the test's assumption about the
  environment was never guaranteed. Fixed by patching `_safe_trash` directly
  to report unavailability, which exercises the exact same rollback branch
  in `_move_across_devices` deterministically, regardless of what happens to
  be `pip install`ed on the machine running the suite.
- **Fixed — a real coverage/dependency blind spot in the sandbox itself,
  not a code bug**: this sandbox was missing `numpy`, `fastapi`, `PyQt6`,
  `httpx`, `python-multipart`, `psutil`, `rapidfuzz`, and `google-genai` —
  all listed in `requirements.txt` but not pre-installed — which meant
  dozens of dashboard, avatar-adjacent, and fuzzy-matching tests were being
  silently skipped rather than actually run. Installing them (this session
  only; not a code change) raised the executed test count from an
  apparent "537 passed / lots skipped" picture to the real
  "570 passed, 0 failed, 25 skipped" described in §5, and confirmed that
  fuzzy matching (`core/text_match.py`) behaves identically whether the
  RapidFuzz accelerator or its `difflib` fallback is active — previously
  only the `difflib` path had ever actually been exercised in this sandbox.
- **Investigated, judged not a bug**: `pyflakes` static analysis was run
  across `actions/`, `core/`, `dashboard/`, `memory/`. Every finding was
  cosmetic and does not change behaviour — unused imports
  (`computer_control.py`, `computer_settings.py`, `file_processor.py`,
  `open_app.py`, `window_manager.py`), a few `f"..."` strings with no `{}`
  placeholder (`browser_control.py`, `file_controller.py`, `core/tts.py`,
  harmless leftover `f`-prefixes), three `except Exception as e:` in
  `core/llm_client.py` where `e` is caught but not printed (a lost
  diagnostic detail, not a functional defect — the code already prints a
  clear fallback message and returns/re-raises correctly), and a loop
  variable named `socket` in `dashboard/server.py::_close_other_sockets`
  that shadows the module-level `import socket` — but only within that one
  function's local scope, which never uses the `socket` module, so it is a
  naming smell with zero behavioural effect. None of these were touched, in
  line with the "not cosmetic" scope given for this bug hunt; they are
  listed here for transparency rather than fixed silently or hidden.
- Targeted manual review (not just static analysis) of the highest
  blast-radius recently-changed areas — `file_controller.py`'s cross-device
  move/rollback path, `open_app.py`'s launch-type diagnosis,
  `core/window_manager.py`'s new `resolve_monitor_token`,
  `desktop_health.py`, `memory/config_manager.py` — found no further
  functional defects.

## 5. Testing & verification — real numbers

Run from the repository root:

```bash
QT_QPA_PLATFORM=offscreen python3 .github/scripts/run_tests.py     # unit suite
QT_QPA_PLATFORM=offscreen python3 .github/scripts/run_overall.py --coverage
```

**End of round 3 (this session), with every `requirements.txt` dependency
actually installed in the sandbox:**

- Unit suite: **570 tests, 0 failures, 25 skipped.**
  - Skips are all legitimate and environment-scoped, not silently
    ignored failures: **13** require `RUN_WINDOWS_INTEGRATION=1` on real
    Windows hardware (`tests/test_windows_integration.py`); **12** need a
    working OpenGL runtime (`libGL.so.1`) for PyQt6, which this particular
    sandbox cannot provide (no root/`apt` access to install the system
    library) but a normal Windows/Linux desktop install already has.
- `run_overall.py --coverage`: **11 checks PASS, 0 failed, 1 skipped**
  (the Windows hardware integration suite, correctly gated behind
  `--windows` on real Windows). **33% overall line coverage**, **19
  safety-critical modules at or above their required floor**, **18 active
  actions**, **27 dashboard routes**.
- New tests added this round: 18 in `tests/test_system_audio.py`, 9 in
  `tests/test_audio_manager.py`, 6 in
  `tests/test_windows_paths_simulated.py::WindowsSettingsTests` (the
  `volume_get`/`volume_set` pycaw-path gap) — 33 new tests, all passing.

Round-over-round: 513 tests (round 1) → 537 (round 2) → **570 (round 3)**;
coverage 31% → 32% → **33%**.

## 6. Known limitations (stated honestly, not glossed over)

- **No Windows hardware exists in this environment.** Every Windows-only
  code path (registry scans, `.lnk` resolution, WMI brightness, pycaw
  volume, the new IPolicyConfig COM output-device switch, monitor
  enumeration via `EnumDisplayMonitors`) is only ever exercised through
  fakes/mocks in `tests/test_windows_paths_simulated.py` and
  `tests/test_system_audio.py`. These prove the Python-side logic asks for
  the right thing in the right order and handles the response correctly;
  they cannot prove the real Windows API/COM interface behaves exactly as
  the (reverse-engineered, in the IPolicyConfig case) documentation claims.
  `tests/test_windows_integration.py`, gated behind `--windows`, is the only
  thing that ever runs against a real machine, and only when someone runs it
  there.
- **The new system-wide output-device switch specifically** relies on an
  undocumented COM interface with a Windows-version-dependent vtable layout;
  two known layouts are tried, but a future Windows release could in
  principle introduce a third one this code does not yet know about. If both
  attempts fail, the action reports failure honestly rather than claiming
  success — it does not silently do nothing.
- **PyQt6 GUI/avatar-adjacent tests could not run in this sandbox** for lack
  of the system `libGL.so.1` library (no package-manager access here); the
  same code path is expected to run normally wherever PyQt6 already ships
  working OpenGL bindings, which is the case on a normal desktop install.
- **Preset workspace "profiles" remain deliberately unimplemented** — asked
  for by name during earlier discussion and explicitly rejected as
  out-of-scope every time.
- 33% overall line coverage means most of the codebase is *not* exercised by
  the automated suite; the 19 safety-critical modules tracked with an
  explicit floor (undo, path policy, sandboxing, action loading/validation,
  the audio/settings platform branches, etc.) are the ones this project
  currently commits to keeping covered.

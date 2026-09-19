# HUD, activities, files and gaming

## Display & HUD

Open **Settings → DISPLAY & HUD**. Changes are saved through the existing atomic
configuration writer. Mini Mode, Gaming Mode and Standard German speech are
independent options. Standard German speech defaults on; Mini and Gaming default
off. Other presentation options remain available: scale, font size, panel width,
reactor size, background transparency, density, anchor, animation, visualizer and
log verbosity.

Qt 6 owns Windows per-monitor DPI scaling. UI geometry uses logical pixels, text
uses native point-sized fonts (minimum 9 pt), and cached graphics carry their real
DPR. Do not add a second `QT_SCALE_FACTOR` to compensate for Windows scaling.
The settings form and chat sidebar scroll when their contents do not fit.

A long activity docks the face/reactor entirely inside the central surface,
separate from the system monitor and chat. The same task panel now occupies the
central workspace. Disabling animated task layout uses an immediate compact dock;
it does not remove task controls. On completion the centerpiece returns, with a
brief result footer. Failed tasks are not drawn as 100% complete.

## Activities and control

`core.tasks` remains the source of truth. States are running, queued, paused,
done (displayed as completed), failed and cancelled. Owners alone acknowledge
completion, cancellation and pause. Percentages are measured bytes or completed
plan steps; unknown work remains indeterminate.

- **Pause** is offered only for checkpoint-capable work (plans and file imports).
- A running tool call is not forcibly suspended. Plans pause between calls.
- **Cancel** requests a cooperative stop; it does not kill arbitrary processes.
- **Details** includes owner-reported output, errors and relevant events.
- The `task_control` tool exposes the same list/inspect/pause/resume/cancel actions
  to voice and chat, using exact task IDs.

Plans track individual steps, explicit backward dependencies and inferred app
prerequisites. Failed dependencies are blocked, while unrelated later steps still
run. Recovery is bounded to existing verified-failure investigation strategies.
Unparsed clauses and truncated work are reported as incomplete, never as success.
Malformed dependency graphs are rejected; discarded prerequisites cannot turn
dependent steps into independent work. Explicit “analyze task:” / “plan:” requests
produce a proposed sequence without executing tools. Cancellation during a tool
prevents recovery or further dispatch after that tool returns.
There is no general guarantee that an external utility supports pause or cancel.

## Mini Mode and Gaming Mode

Mini Mode is a movable/resizable tool window with a small HUD, chat, status,
activity summary, input, mute/interrupt buttons and **Open JARVIS**. Its chat view
shares the main transcript's actual `QTextDocument`, not a copied message store.
Closing Mini returns to the full application. Closing the full application ends
both presentations. Mini size and position are saved with a debounce; disconnected
monitors and smaller work areas are handled by clamping the full window frame.
Stale foreground observations are invalidated on settings changes and explicit
bring-forward, so they cannot hide a newly opened HUD. Confirmations are indicated in Mini; open JARVIS to review
and approve them. Mini never approves a confirmation automatically.

On Windows, Gaming Mode observes the foreground window's process and monitor
rectangle without changing focus. Known game executables are recognized directly;
unknown executables under common game-install directories require fullscreen.
Additional windowed games can be listed as executable basenames in the existing
`gaming_executables` config list. This is conservative detection, not exhaustive
game identification. A fullscreen browser or editor is not treated as a game.

When a game becomes foreground, Mini is shown without activation and the main
window is hidden. No utility is automatically raised when the game loses focus.
Use **Open JARVIS** to explicitly bring the assistant forward. Timers and workers
continue while the full window is hidden.

The App Controller requests minimized/no-activation startup for direct Windows
executables. Protocol/launcher/browser starts are refused while a game is
foreground because their handlers can ignore background requests. Applications
may also ignore documented Windows startup flags; there is deliberately no
focus-stealing/restoration hack. Exclusive-fullscreen and anti-cheat environments
may hide ordinary desktop overlays. The overlay is not injected into a game.

Window-control extensions reuse the App Controller: snap left/right, resize,
move to a numbered monitor and toggle always-on-top. Geometry uses monitor work
areas, supports negative monitor origins, uses `SWP_NOACTIVATE`, and verifies the
result. Ambiguous window titles require clarification. Focus is explicit;
maximizing/restoring utilities or making them topmost is blocked while gaming.
Existing resolver, close/restart safeguards and confirmation gates remain intact.

## Files

Use the existing drop zone or multi-file picker. Files are **local attachments**,
not automatic uploads to a cloud provider. Two workers copy bounded chunks into
a session-private temporary directory, measure actual bytes and compute metadata
and a SHA-256 hash. A changing source file fails rather than becoming a corrupt
attachment. Limits are 32 files and 512 MB per file.

Cards show name, MIME type, size, import/processing/ready/error state, real
progress, preview and cancel/remove. Text previews are limited to 4 KiB; image
previews are size-bounded and never launch an external application. Source files
are never deleted by removing an attachment. The legacy current-file accessor
also resolves only verified, still-attached staged files. Failed cards retain their
actual task outcome even after bounded registry history expires.

Only verified ready files enter the conversation's attachment context. IDs include
session and active agent ownership. Context is also included with subsequent chat
requests so the assistant can use the existing file-processing tools. Import
completion itself does not generate a user turn, interrupt speech or trigger tools. Filenames
and content are explicitly identified as untrusted data. Removing an attachment
prevents future use; an already running external tool cannot be forcibly recalled.

## Speech and transcript

`JarvisLive._publish_assistant` is the final response sink for the existing chat,
session memory and dashboard. Live commits output transcription only at completed
turns; interrupted partial output is discarded without discarding user input.
Local/silent announcements use the same sink. Live instructions are not themselves
inserted as assistant speech. Duplicate pending notifications are coalesced, and
complete chat responses are no longer revealed one character at a time.

The current native Live model auto-selects its pronunciation and does **not**
support an explicit language-code lock. Standard German mode therefore renders
the **final transcript** through `de-DE-ConradNeural`, then sends 24 kHz mono PCM
to the existing audio queue/player. Output-device selection, echo suppression,
visualization and interruption remain shared. If neural synthesis is unavailable,
Windows SAPI with an installed German (Germany), LCID 0407 voice is used. No German
voice installed means a visible speech-unavailable notice; chat remains intact.
Abandoned synthesis is generation-checked and cancelled on its event loop. Queue
backpressure cannot resurrect an interrupted audio chunk. Offline COM synthesis
has a bounded wait and receives a cooperative cancellation signal.

This adds a final-transcript/synthesis delay versus streaming native audio. The
native Live voice picker applies when Standard German mode is disabled; native
accent then remains provider-controlled. Offline OS speech is not neural-quality.
Audio quality and latency require listening tests on real Windows hardware.

## Validation

Run:

```text
python -m pytest tests/test_quality_pass.py tests/test_product_upgrade.py tests/test_native_hud.py
python test_overall.py
python -m pytest tests/
```

The native HUD tests explicitly skip if host Qt libraries are unavailable; that
is not evidence of Windows rendering quality. Before release, check 100/125/150/
200% Windows scaling, mixed-DPI monitors, actual foreground games (including
exclusive fullscreen), real speaker devices, German voice availability and live
network synthesis. The automatic setup still verifies applicable package versions
and browser launches; it has no browser-choice prompt. The overall runner streams
suite output, emits heartbeats and reports per-suite counts, elapsed time and
bounded timeouts.

## Presentation boundaries

`core/task_ui.py` owns compact task rows and scrollable plain-text details;
`core/attachment_ui.py` owns the multi-file input and cards. The drop zone no
longer has a second, contradictory single-file selection/removal state.
`core/activity_ui.py` uses the task panel's snapshots instead of copying registry
metadata at animation frequency. `core/ui_scaling.py` keeps authored control
height bounds so raising and lowering font scale is reversible. These modules
consume existing state and do not create separate task, transcript or audio stores.

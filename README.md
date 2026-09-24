# Mark-LIV

MARK LIV is a cross-platform JARVIS-style assistant with an optional local **Hey Jarvis** wake word.

## Run

```bash
python setup.py
python main.py
```

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

The restart command saves the current session, stops audio/wake-word workers, launches a fresh MARK LIV process, and then exits the old one. Closing another application requires confirmation; Windows administrator/UAC operations are never silently bypassed.

The phone dashboard's **CONTROL** panel uses the same action registry as voice commands. It is the primary control surface rather than a hotkey collection: press **Ctrl/Cmd-K** for the command palette, use quick cards for windows, monitors, audio, Explorer, Task Manager, and relaunch, and use the per-window FOCUS/MIN/MAX/CLOSE controls. It shows live action IDs, progress, cancellation, confirmations, named-window occupancy, monitor tiles, audio health, and undo history. It is a control surface, not an unrestricted way around Windows security.

Audio can also be controlled by voice or the dashboard: say `list audio devices`, then `use JBL Quantum 400 microphone` or `use JBL Quantum 400 speakers`. The saved device name is resolved again after reconnects, so changing USB device indices does not silently select the wrong device. The admin panel reports selected input/output, connected/fallback state, host API, and sample rate. A reconnect request rebuilds both streams while keeping the conversation resumption handle.

On Windows, monitor enumeration opts into per-monitor DPI awareness before reading HWND coordinates and uses the current display mode for refresh rate. This keeps two 1920×1080 displays and high-refresh modes such as 180 Hz visible as physical pixel geometry rather than scaled logical coordinates. The native ctypes fallback still reports geometry when pywin32 is not installed.

### Spotify background control

`media_control` uses the Spotify Web API and Spotify Connect rather than clicking the Spotify window. It can search, play, pause, skip, change volume, inspect the current track, and list devices while Spotify remains minimized or in the background. The one-time `connect` flow requires a Spotify developer client ID, the local redirect URI `http://127.0.0.1:8765/callback`, and a Spotify Premium account for playback control. Playback requires an active Spotify Connect device; MARK LIV does not falsely claim that a track started when Spotify has no available device.

## Optional capabilities

Browser automation (Playwright), screen/camera capture (NumPy/OpenCV/MSS/Pillow), and system metrics (psutil) are optional. MARK LIV starts without them: imports are lazy, the action registry keeps a capability record, and the dashboard/voice result identifies the missing package instead of rejecting the whole application. Native browser opening and the rest of desktop control remain available.

## Action safety and reliability

All discovered actions now pass through one registry contract. Results have explicit `succeeded`, `failed`, `forbidden`, `confirmation_pending`, `unavailable`, and `timed_out` states; handlers keep their old string API only at the Gemini boundary. The registry also owns per-action deadlines, trusted/admin checks, confirmation metadata, and the dashboard capability manifest.

High-impact operations never accept a model-supplied `confirmed` flag. The HUD confirmation token is issued by the interface and protects app/PC closing, file deletion, power actions, WiFi changes, and other risky operations. Reversible settings continue to use the shared undo stack.

The active action surface is intentionally small and PC-focused. Travel, weather, messaging, developer-agent, game-updater, generated-desktop-task, and YouTube-specific actions were removed instead of advertising unrelated or duplicated capabilities. Browser control covers normal websites; `media_control` uses Spotify Connect for playback without repeatedly foregrounding Spotify. `shortcut_manager` stores deterministic aliases such as `gd → Geometry Dash` and `roblox → Roblox Player`.

Your API keys and runtime memory are intentionally ignored by Git. Never commit `config/api_keys.json` or personal data from `memory/`.

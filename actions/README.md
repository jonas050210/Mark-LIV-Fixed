# Bundled actions

The bundled actions are the PC-control surface. Unrelated or duplicate
integrations were removed from this directory so discovery cannot advertise
capabilities that do not help operate the computer:

- `open_app`: focus existing applications, launch applications, and handle explicitly requested Roblox instances
- `window_manager`: named windows and monitors
- `computer_control`: mouse, keyboard, clipboard, screenshots, and UI targeting
- `system_control`: system/media settings and confirmed power actions
- `browser_control`: browser navigation and interaction
- `audio_manager`: microphone and speaker selection/diagnostics
- `media_control`: background Spotify playback/search and system media
- `file_controller`: known-folder Explorer search/open/select plus safe file and folder operations with confirmation/undo
- `file_processor`: work on user-uploaded files
- `shortcut_manager`: deterministic app/file aliases
- `web_search`, `reminder`: supporting assistant capabilities

Developer-agent, messaging, travel, weather, game-updater, YouTube-specific,
and generated-desktop-task actions are intentionally not bundled. Browser and
media control cover the useful portions without duplicating those actions.

Run the project-wide safe verification from the repository root with
`python test_overall.py`. It validates imports, action schemas, dashboard
JavaScript, setup inputs, secret hygiene, and the full unit-test suite. Use
`--windows` only on the Windows machine whose monitors and audio devices should
be tested.

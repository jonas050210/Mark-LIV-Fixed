# Bundled actions

The bundled actions are the PC-control surface. Unrelated or duplicate
integrations were removed from this directory so discovery cannot advertise
capabilities that do not help operate the computer:

- `open_app`: launch applications and remembered aliases
- `window_manager`: named windows and monitors
- `computer_control`: mouse, keyboard, clipboard, screenshots, and UI targeting
- `system_control`: system/media settings and confirmed power actions
- `browser_control`: browser navigation and interaction
- `audio_manager`: microphone and speaker selection/diagnostics
- `media_control`: background Spotify playback/search and system media
- `file_controller`: safe file and folder operations with confirmation/undo
- `file_processor`: work on user-uploaded files
- `shortcut_manager`: deterministic app/file aliases
- `web_search`, `reminder`: supporting assistant capabilities

Developer-agent, messaging, travel, weather, game-updater, YouTube-specific,
and generated-desktop-task actions are intentionally not bundled. Browser and
media control cover the useful portions without duplicating those actions.

# MARK LIV — Fix- & Verbesserungsreport (Kurzformat)

**Datum:** 26.09.2026 · **Branch:** `arena/01a0dd3a-mark-liv-fixed` · **Status:** ✅ fertig, 693 Tests grün, Linter sauber

---

## 1) Fehleranalyse: „JARVIS kann offene Apps nicht sehen"

Dein Mitschnitt (YouTube geöffnet → „Mache es Fullscreen auf meinen ersten Monitor" → *„konnte kein sichtbares Fenster finden, das 'YouTube' enthält"*) hat drei reale Schwachstellen im Fenster-Matching aufgedeckt:

| Ursache | Was passierte | Fix |
|---|---|---|
| **Title-Race** — direkt nach dem Öffnen heißt der Tab erst mal „New Tab"; erst nach 1–3 s lädt der Titel „YouTube - Chrome" | Suche lief zu früh → 0 Treffer | Fenster-Suche prüft jetzt bis ~2,5 s lang erneut nach, bis der Titel steht |
| **Tab in bestehendem Fenster** — Titel zeigt den *aktiven* Tab, nicht unbedingt „YouTube" | Titel-Matching schlägt fehl, obwohl die Seite offen ist | JARVIS merkt sich ab jetzt **jedes selbst geöffnete Fenster** (Name + Handle, 15 min). Folgebefehle finden es über diese Erinnerung, auch ohne Titel-Treffer |
| **„Browser" als gesprochenes Wort** — Chrome heißt im System `chrome.exe`, Edge `msedge.exe`; keinem ähnelt das Wort „Browser" | „Browser" erzielte 0 Punkte → „nicht gefunden" | Alias-Tabelle: „Browser/Internet/Web Browser" matcht jedes bekannte Browser-Fenster (Chrome, Edge, Firefox, Brave, Opera, Vivaldi …) mit 85 Punkten |

**Zusätzlich als Sicherheitsnetz:**

- **URL-Handoff:** Wurde gerade eine Seite geöffnet (z. B. `youtube.com`), aber der Tab-Titel enthält den Namen nie (z. B. laufendes Video), findet der Befehl das Fenster über die zuletzt geöffnete URL — aber nur, wenn genau **ein** Browser-Fenster offen ist. Bei mehreren wird ehrlich gefragt statt geraten.
- **Selbstkorrektur:** Findet JARVIS immer noch nichts, listet die Fehlerantwort jetzt **die aktuell sichtbaren Fenster** auf (Titel + Prozess) — das Modell kann im selben Gespräch mit dem echten Titel nachfassen, statt zu behaupten, es sei nichts offen. Ist der Desktop gar nicht lesbar, sagt die Meldung den Fenster-Backend-Namen — damit ist das Problem in 5 Sekunden diagnostiziert.
- **Keine Doppel-Launches mehr:** „Öffne YouTube" während der Titel noch lädt → JARVIS fokussiert das gerade geöffnete Fenster, statt eine zweite Instanz zu starten.

## 2) Shutdown / „Schließe dich" — sofort, ohne Bestätigung

- **`shutdown` / `close yourself` / `beende dich` / `schließe dich` / „das war's für heute"** → JARVIS schließt sich **sofort**: Session wird gespeichert, kurzer Abschied, Fenster zu. Kein Confirm-Banner, keine Rückfrage. (Starten genügt, um ihn zurückzuholen — deshalb ist das keine irreversible Aktion mehr.)
- **Trennschärfe:** Ein bloßes „Shutdown" meert jetzt eindeutig den **Assistenten**. Den **PC** herunterzufahren („fahre den PC herunter") bleibt wie vorher eine **bestätigte** Aktion — das ist irreversibel.
- Gleiches Verhalten im neuen **Tray-Menü** und im Dashboard (dort ist `shutdown_jarvis` nicht mehr als „requires confirmation" markiert).

## 3) GUI-Verbesserungen (neu)

| Feature | Wo | Was es tut |
|---|---|---|
| **OPEN-WINDOWS-Panel** | Einstellungen (⚙) → „🪟 OPEN WINDOWS" | Zeigt *exakt*, was JARVIS sieht: alle Fenster mit Titel, Prozess, Größe, Monitor, Min/Max-Zustand **und dem Backend-Namen**. Klick auf eine Zeile = Fenster fokussieren, ✕ = schließen, ⟳ = aktualisieren. Bei „er sieht meine Apps nicht" siehst du in 5 Sekunden, warum. |
| **Tray-Icon** | Benachrichtigungsbereich | JARVIS-Icon mit Menü: Anzeigen/Verstecken, Fensterliste öffnen, **„Shutdown JARVIS"** (sofort). Einzelklick blendet das HUD ein/aus. |
| **Log-Zeitstempel** | HUD-Log | Jede Logzeile bekommt `HH:MM:SS` (dezent grau). Abfolgen wie „Launch → Suche fehlgeschlagen" sind damit nachvollziehbar — bei deinem Fall hätte man den Race-Condition-Zeitpunkt direkt gesehen. |

## 4) Alle Änderungen im Überblick

| # | Typ | Datei(en) | Änderung | Nutzen |
|---|---|---|---|---|
| 1 | 🐞 Fix | `core/window_manager.py` | Browser-Aliase im Fenster-Scoring (85 pts) | „Browser" findet chrome/edge/firefox/… |
| 2 | 🐞 Fix | `core/window_manager.py` | Start-Speicher: `remember_last_window`, `recent_launch_window_for`, `watch_launched_window` | Fenster „gerade geöffnet als X" ist auffindbar |
| 3 | 🐞 Fix | `actions/window_manager.py` | `_strict_matches` (Retry ~2,5 s), `_named_window_candidates` (Retry + Start-Speicher + URL-Handoff) | Überlebt Title-Race & Tab-ohne-Name |
| 4 | 🐞 Fix | `actions/window_manager.py` | Fehlermeldung listet sichtbare Fenster + Backend-Name | Modell korrigiert sich selbst im gleichen Gespräch |
| 5 | 🐞 Fix | `actions/open_app.py` | URL-Start wartet kurz aufs Fenster & merkt es sich; Handoff-Notiz; kein Doppel-Launch | „YouTube öffnen" → „Fullscreen" funktioniert |
| 6 | 🐞 Fix | `actions/browser_control.py` | Native Browser-Opens werden im Hintergrund getrackt | Gleiches für `browser_control`-Seiten |
| 7 | ✨ Feature | `main.py` | `shutdown_jarvis` sofort ohne Confirm; `request_immediate_shutdown()` | „Schließe dich" = sofort zu |
| 8 | ✨ Feature | `main.py`, `actions/computer_settings.py`, `core/prompt.txt` | Begriffstrennung Assistent vs. PC; Prompt-Regeln aktualisiert | „Shutdown" meert JARVIS, „PC herunterfahren" bleibt bestätigt |
| 9 | 🎨 GUI | `ui_panels/windows.py` (neu), `ui.py` | OPEN-WINDOWS-Panel mit Fokus/Close/Refresh | Fenster sichtbar & steuerbar — für dich UND JARVIS |
| 10 | 🎨 GUI | `ui.py` | Tray-Icon (Anzeigen/Verstecken, Fenster, sofortiger Shutdown) | Schnellzugriff ohne HUD |
| 11 | 🎨 GUI | `ui.py` | Log-Zeitstempel | Abläufe nachvollziehbar |
| 12 | 📖 Doku | `README.md` | Abschnitt „Desktop control" erweitert | Verhalten dokumentiert |
| 13 | ✅ Tests | `tests/test_window_discovery.py` (neu), `test_boot_smoke.py`, `test_ui_panels.py` | **20 neue Tests** für alle Fixes | Regressionsschutz |

**Testergebnis:** 693 Tests, alle grün (3 vorbestehende Umgebungsfehler: `defusedxml` fehlt ×2, `core/explorer._OS` ×1 — identisch auf `main`, nicht durch diese Änderung). `ruff check .` sauber.

## 5) Nächste Verbesserungsvorschläge (noch nicht umgesetzt)

1. **Fenster-Events statt Polling** (WinEventHook): JARVIS reagiert sofort auf Öffnen/Schließen/Titelwechsel — kein Warten mehr, kein Retry nötig.
2. **Tab-Bewusstsein:** Browser via CDP/Playwright auslesen, damit „der YouTube-Tab" unter 20 offenen Tabs gezielt ansprechbar ist (nicht nur das Fenster).
3. **Suchfeld im Einstellungen-Drawer:** Die Schalterliste wächst — ein Filter würde die Navigation beschleunigen.
4. **Confirm-Banner mit Tastatur** (J/N) — für die verbleibenden Bestätigungen (PC-Aus, WLAN).
5. **Deutsches Wake-Word-Modell** (openWakeWord custom), damit „Jarvis" zuverlässiger reagiert.
6. **Pro-App-Lautstärke** (Windows Volume-Mixer): „Mach Spotify leiser, aber YouTube lauter" — ohne Systemlautstärke.

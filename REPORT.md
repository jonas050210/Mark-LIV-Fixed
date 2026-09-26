# MARK LIV — Fix- & Verbesserungsreport (Kurzformat)

**Datum:** 26.09.2026 · **Branch:** `arena/01a0dd3a-mark-liv-fixed` · **Status:** ✅ Runde 1–3 fertig · 749 Tests grün, Linter sauber

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

**Testergebnis (Runde 1):** 693 Tests, alle grün (3 vorbestehende Umgebungsfehler: `defusedxml` fehlt ×2, `core/explorer._OS` ×1 — identisch auf `main`, nicht durch diese Änderung). `ruff check .` sauber.

## 5) Runde 2 — umgesetzte Verbesserungen

| # | Typ | Datei(en) | Änderung | Nutzen |
|---|-----|-----------|----------|--------|
| 14 | ⚡ Perf | `core/window_events.py` (neu) | **WinEvent-Pumpe:** abonniert die OS-Fenster-Events (Öffnen/Schließen/Titel/Move/Fokus) | Kein Polling mehr — Reaktionen sofort |
| 15 | ⚡ Perf | `core/window_manager.py`, `actions/`, `main.py` | Alle Warteschleifen wachen per Event auf (statt fixer Sleep); Fensterliste wird zwischen Events gecacht | „Fullscreen" kommt Sekundenbruchteile früher; Enumeration wird gratis |
| 16 | ✨ Feature | `actions/audio_manager.py` | **Pro-App-Lautstärke** über den Windows-Mixer: `list app volumes`, `Spotify 50`, `Discord leiser`, `mute Chrome` — mit Undo | Eine App lauter/leiser ohne alles andere zu ändern |
| 17 | 🎨 GUI | `ui_panels/confirm.py` | Confirm-Banner per **[Y]/[N]**-Taste beantwortbar (Fokus-sicher: klaut keine Tasten aus dem Eingabefeld) | PC-Aus/WLAN schneller bestätigen |
| 18 | 🎨 GUI | `ui.py` | **Suchfeld im Einstellungen-Drawer** (Filter „wa" → Wake Word, „au" → Audio…) | 15 Buttons in 2 Tasten gefunden |
| 19 | 🎨 GUI | `ui_panels/windows.py` | OPEN-WINDOWS-Panel aktualisiert sich live (2 s, nur bei echten Änderungen — kein Flackern) | Liste bleibt aktuell, solange offen |

**Details:**
- **WinEventHook:** Ein Daemon-Thread installiert `SetWinEventHook` (OUTOFCONTEXT, eigene Prozesse ausgeschlossen) und betreibt eine Message-Loop. Der Callback ist minimal (Zähler + Event), enumeriert nie selbst. Läuft der Hook nicht (Linux/Fehler), verhält sich alles exakt wie vorher (Schlafen statt Warten, kein Cache). Der eigene Fenster-Cache wird bei eigenen Aktionen synchron geleert — die Verifikation nach einem Move liest nie alte Geometrie.
- **Pro-App-Lautstärke:** pycaw (bereits Abhängigkeit). Matching per Prozessname (exakt → Teilstring → scharfes Fuzzy ≥ 0,75). Mehrere Sessions einer App (z. B. Chrome) werden alle angepasst. `None` (pycaw fehlt) wird von `[]` (nichts spielt) unterschieden — ehrliche Meldungen. Undo stellt alte Werte wieder her; tote Sessions werden übersprungen, nicht behauptet.

## 6) Testergebnis Runde 2

- **713 Tests, alle grün** (3 vorbestehende Umgebungsfehler wie auf `main`: `defusedxml` ×2, `core/explorer._OS` ×1 — nicht durch diese Änderungen).
- Neu: `tests/test_window_events.py` (8 Tests: Fallback-Sleep, Revision, Cache-Gültigkeit/-Invalidierung, Bypass ohne Hook) und 12 Per-App-Volume-Tests in `tests/test_audio_manager.py` (fake pycaw-Modul, Setzen/Clamp/Mute/Undo/Mehrdeutigkeit/ehrliche Fehler).
- `ruff check .` sauber.

## 7) Runde 3 — umgesetzte Verbesserungen (Sprache & Tabs)

| # | Typ | Datei(en) | Änderung | Nutzen |
|---|-----|-----------|----------|--------|
| 20 | ✨ Feature | `core/window_manager.py` | **Deutsche Monitor-Namen:** „auf meinen **ersten** Monitor", „**zweiten**", … (jede Kasusform), „**Hauptmonitor**", „**links**/**rechts**", „**anderen** Monitor", „Bildschirm 2" | Der originalen Nutzer-Transkript-Satz funktioniert jetzt wörtlich |
| 21 | 🐛 Fix | `core/window_manager.py` | **Umlaut-Faltung** in `_normalise`: ä→a, ö→o, ü→u, ß→ss | „Müller" hieß bislang „m ller" — jedes Fenster mit Umlaut im Titel war nicht treffbar; jetzt matcht auch getipptes „muller" |
| 22 | ✨ Feature | `core/window_manager.py` | **Deutsche Browser-/Füllwörter:** „Browser(-)fenster", „Internetfenster" als generische Referenz; „öffne/bitte/und/fenster/das/die…" sind Rauschen bei der Label-Prüfung | „öffne bitte das Spotify Fenster" verwechselt nichts mehr — nur der echte Name zählt |
| 23 | ✨ Feature | `actions/browser_control.py` | **Tab-Bewusstsein Teil 1:** `list_tabs` (nummerierte Liste, aktiver Tab markiert) + `switch_tab` (Nummer oder Titel-/URL-Teil, Umlaut-faltend, bringt den Tab nach vorn) | Mehrere offene Seiten in der gesteuerten Session gezielt wechseln, ohne neu zu navigieren |
| 24 | 🛡️ Ehrlichkeit | `actions/browser_control.py` | list/switch_tabs **nur mit bestehender Session** — kein heimlich geöffnetes Automationsfenster; ohne Session ehrliche Antwort | Kein „ich sehe deine Tabs" über die Grenzen hinaus (echte Browser-Tabs bräuchten einen Debug-Port) |
| 25 | ✨ Feature | `actions/computer_settings.py` | **Deutsche System-Befehle:** „mach lauter/leiser", „ton aus", „lautstärke auf 30", „heller/dunkler", „bildschirm sperren", „dunkelmodus", „wlan", „neu laden", „vollbild", „pc ausschalten/herunterfahren", „neustart", … | Alltägliche deutsche Sprachbefehle treffen die richtige Aktion statt „unbekannter Befehl" |
| 26 | 🛡️ Schutz | `actions/computer_settings.py` | „ton" mit Wortgrenze erkannt | „button 3" wird nicht mehr als „Lautstärke 3" fehlinterpretiert |
| 27 | 📝 Doku | `actions/window_manager.py`, `actions/open_app.py`, `actions/browser_control.py` | Tool-Beschreibungen nennen die deutschen Monitor-Token bzw. `tab`-Parameter | Das Modell weiß, dass es Deutsch übergeben darf — ohne GUI-Änderung |

**Keine GUI-Änderungen** (wie gewünscht): `ui.py`, `ui_panels/`, Tray und Dashboard sind unberührt.

**Details:**
- **Deutsche Ordinalia** werden über Stämme (erst-, zweit-, dritt- … zehnt-) erkannt, damit jede Kasus-/Endungsform („ersten/erste/erster") trifft; die Zahl im Namen („Bildschirm 2") gewinnt weiterhin immer zuerst, genau wie „primary/secondary/left/right" (jetzt plus „Haupt-", „anderen", „links/rechts").
- **Tab-Bewusstsein Teil 1** sieht nur die Tabs des Fensters, das der Assistent selbst steuert (Playwright-Session) — die Tabs des normalen Benutzer-Browsers sind ohne Debug-Port prinzipiell nicht einsehbar. Genau das sagt die Meldung, statt etwas zu behaupten oder still ein Fenster zu öffnen.
- **Power-Befehle** („pc ausschalten" …) behalten die Bestätigungsgate wie englische Pendants; nur „shutdown"/„schließe dich" **über den Assistenten** schließt weiterhin sofort (Runde-1-Vertrag, unangetastet).

## 8) Testergebnis Runde 3

- **749 Tests grün** (752 gesamt; 3 vorbestehende Umgebungsfehler wie auf `main`: `defusedxml` ×2, `core/explorer._OS` ×1 — nicht durch diese Änderungen).
- Neu: `tests/test_german_commands.py` (38 Tests): Umlaut-Faltung, deutsche Monitor-Token (Ordinalia in allen Kasus, Ablehnung bei „dritten" mit 2 Monitoren, semantische Namen), generische Browser-Wörter, Füllwort-Rauschen, deutsche System-Aliase inkl. „button 3"-Schutz, Tab-Session-Methoden (Liste/Nummer/Titel/Umlaut/Fehlerfälle) und Routing (ehrliche Meldung ohne Session, Weiterleitung mit).
- Schema-/Dispatcher-Konsistenztest weiter grün (neue Aktionen in `_DIRECT_ACTIONS` deklariert).
- `ruff check .` sauber. Root-`test_overall.py` unverändert zum Baseline (8 bestanden, 1 vorbestehend fehlgeschlagen, 3 übersprungen).

## 9) Weitere Verbesserungsvorschläge (offen)

1. **Tab-Bewusstsein Teil 2:** Browser via CDP auslesen, damit „der YouTube-Tab" unter 20 Tabs des *normalen* Browsers gezielt ansprechbar ist (braucht Start mit `--remote-debugging-port`; Teil 1 deckt nur die gesteuerte Session ab).
2. **Deutsches Wake-Word-Modell** (openWakeWord custom), damit „Jarvis" zuverlässiger reagiert.
3. **Dashboard-Fernsteuerung:** OPEN-WINDOWS-Panel auch ins Phone-Dashboard legen (GUI-Änderung, deshalb in Runde 3 bewusst ausgelassen).
4. **Mikrofon-Routing pro Befehl** (z. B. Kommunikation vs. Standard) — erweiterbar über `audio_manager`.



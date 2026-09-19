# MARK LIV — Projektübersicht & Architekturstatus

## 1. Einführung & Projektziel
Mark LIV (54) ist ein plattformübergreifender, echtzeitfähiger persönlicher KI-Assistent für Windows, macOS und Linux. Das System basiert auf der Gemini Live API für native bidirektionale Audioübertragung und erweitert dieses Fundament um autonome Browsersteuerung, Code-Engineering, Systemautomatisierung sowie lokale Dokumenten- und Medienanalyse.

- **Vollständiges $0-Paradigma**: Ausschließlich lokale Open-Source-Engines und standardmäßig freie Kontingente. Keine versteckten Cloud-Abonnements oder kostenpflichtigen Drittanbieter-Services.
- **Plattformstrategie**: Windows als primäre Zielumgebung (mit Unterstützung für Windows UI Automation, native Benachrichtigungen und Taskplaner). Nahtlose Ausführung unter Linux und macOS über standardisierte Fallbacks.
- **Credits & Danksagung**: Original von FatihMakes. Repariert, modularisiert und verbessert von Jonas, unter Mitwirkung von ChatGPT (Director) und Arena.ai (Coding Agent).

---

## 2. Systemarchitektur

```
                               ┌─────────────────────────────────┐
                               │         PyQt6 HUD / UI          │
                               │  (Holographic Face, Waveform,   │
                               │   Overlays, Settings & Events)  │
                               └───────────────┬─────────────────┘
                                               │
                                               ▼
                               ┌───────────────────────────┐
                               │     Gemini Live Core      │
                               │ (Realtime Audio, Vision,  │
                               │  Continuous Tool Calls)   │
                               └─────────────┬─────────────┘
                                               │
                                               ▼
                               ┌───────────────────────────────┐
                               │ Action Loader & Tool Registry │
                               │   (22 Aktive Bundled Skills)  │
                               └───────────────┬───────────────┘
                                               │
               ┌───────────────────────────────┴───────────────────────────────┐
               ▼                               ▼                               ▼
┌─────────────────────────────┐ ┌─────────────────────────────┐ ┌─────────────────────────────┐
│    Automatisierung & UI     │ │    Dokumente & Analyse      │ │    Medien, Web & System     │
│ • computer_control (UIA)    │ │ • document_qa (PyMuPDF)     │ │ • youtube_video (yt-dlp)    │
│ • open_app (Shortcut-Cache) │ │ • summarize (trafilatura)   │ │ • web_search (Dual Tier)    │
│ • browser_control           │ │ • file_processor            │ │ • system_monitor            │
│ • code_agent (Scaffold/Fix) │ │ • file_controller           │ │ • audio_device / settings   │
│ • desktop_control / notify  │ │ • screen_vision (Find/Read) │ │ • timer / reminder          │
│ • app_inventory (Index)     │ │                             │ │                             │
│ • close_app (Prozess-Close) │ │                             │ │                             │
│ • agent_task (Multi-Step)   │ │                             │ │                             │
└─────────────────────────────┘ └─────────────────────────────┘ └─────────────────────────────┘
```

---

## 3. Die 22 aktiven Aktionen (`actions/`)

Alle 22 Aktionen deklarieren ein standardisiertes `TOOL`-Schema und werden zur Laufzeit von `core/action_loader.py` automatisch geladen:

1. `app_inventory`: Beantwortet „Was ist installiert / wo liegt X / ist Y ein Spiel und wie startet es" aus dem gemeinsamen App-Index. Rein lesend.
2. `audio_device`: Verwaltet und wechselt Ein- und Ausgabegeräte (Mikrofon/Lautsprecher) namentlich und prüft deren Hardware-Verfügbarkeit.
3. `browser_control`: Deterministische, skriptbasierte Browserautomatisierung mittels Playwright (Recherche, Navigation, Formulareingabe). Nur `http(s)`-URLs; `file:`/`javascript:`/`data:` werden abgewiesen.
4. `code_agent`: Vollwertiger Entwicklungs-Agent. Konsolidiert Einzeldatei-Codebearbeitung und -Ausführung mit Multi-File-Projekt-Scaffolding und Fix-Loops. Alle Pfade sind auf das Home-Verzeichnis begrenzt, Timeouts auf 5–300 s.
5. `computer_control`: Direkte Eingabesteuerung. Nutzt primär Windows UI Automation (`uiautomation`) für semantische Interaktion mit Buttons, Feldern und Fenstern, mit nahtlosem `pyautogui`-Koordinaten-Fallback.
6. `computer_settings`: Verwaltung von Systemeinstellungen (Lautstärke, Helligkeit, WLAN, Energieoptionen, Prozessverwaltung).
7. `desktop_control` (`desktop.py`): Desktop-Organisation, Wallpaper-Wechsel, Bereinigung und Fensterverwaltung.
8. `document_qa`: Fragestellungen zu PDF-, Word-, Excel- und PowerPoint-Dateien. Verwendet Tier 1 `PyMuPDF` (`pymupdf`) für bis zu 11-fach schnellere Textextraktion mit Fallbacks auf `pdfplumber` und `PyPDF2`.
9. `file_controller`: Datei- und Verzeichnisoperationen (Erstellen, Verschieben, Löschen mit Papierkorb-Schutz, Auflisten). Quelle UND Ziel jeder Operation werden gegen die Home-Sandbox geprüft.
10. `file_processor`: Parsing, Formatkonvertierung und Tabellenanalyse für Arbeitsdateien. Home-Sandbox, Zip-Slip-geschützte Archivextraktion, validierte Ausgabeformate.
11. `game_updater`: Überprüfung und Verwaltung von Spielaktualisierungen auf Steam und Epic Games. „Herunterfahren wenn fertig" erfordert eine HUD-Bestätigung; geplante Tasks laufen ohne Admin-Rechte.
12. `notify`: Desktop-Benachrichtigungen über das Betriebssystem mit mehrstufigen Rückfallpfaden (inkl. macOS AppleScript-Stufe mit entschärften Anführungszeichen).
13. `open_app`: Starten beliebiger Programme über `core/app_finder.py` (Aliase, Startmenü-Index, persistenter Cache `~/.jarvis_app_cache.json`, CLI-Argumente unter Linux). Installiert oder lädt niemals etwas herunter.
14. `reminder`: Zeitgesteuerte Erinnerungen über betriebssystemeigene Scheduler (Windows Taskplaner, macOS LaunchAgent, Linux systemd/`at`).
15. `screen_vision`: Bildschirmverständnis auf Abruf (beschreiben/lesen/finden) mit Cooldown, Schwärzung von Zugangsdaten und normalisierten Koordinaten.
16. `send_message`: Versand von Nachrichten über installierte Messenger wie WhatsApp oder Telegram.
17. `summarize`: Komprimiert Texte, Zwischenablage, URLs oder Dokumente. Nutzt `trafilatura` für saubere HTML-Extraktion ohne Navigations- und Cookie-Müll mit BeautifulSoup-Fallback und SSRF-Schutz (inkl. Redirect-Prüfung pro Hop).
18. `timer`: Gesprochene Countdown-Timer in Hintergrund-Daemon-Threads mit Sprachansage.
19. `web_search`: Parallele Websuche über Gemini Grounded Search und DuckDuckGo (ddgs).
20. `youtube_video`: Wiedergabe, Metadatenabruf und Transkript-Zusammenfassungen. Nutzt `yt-dlp` für robuste Video-Infos und automatischen Untertitel-Fallback.
21. `close_app`: Schließt Apps über ihre eigenen Prozesse (terminate → wait → kill, mit Nachprüfung) — niemals per Alt+F4/Ctrl+W oder globalen Shortcuts. Schließt niemals JARVIS selbst, eigene Geschwisterprozesse oder Systemprozesse.
22. `agent_task`: Multi-Step-Agent — führt ein Ziel („öffne Spotify und suche Jazz“) als Schrittfolge über den zentralen Tool-Dispatcher aus (Plan → Execute → Verify); stoppt ehrlich beim ersten unverifizierten Schritt.

---

## 4. Modernisierte Backends & Benchmarks

| Komponente | Vorherige Lösung | Modernisiertes Backend | Fallback-Ebene | Gemessener Benchmark (Mark LIV) |
|---|---|---|---|---|
| **PDF-Extraktion** (`document_qa.py`) | `pdfplumber` / `PyPDF2` | **PyMuPDF (`pymupdf`)** | `pdfplumber` $\to$ `PyPDF2` | **11,12 ms vs. 127,24 ms (11,4× schneller)** bei 50 Seiten |
| **Web-Extraktion** (`summarize.py`) | BeautifulSoup DOM-Dump | **Trafilatura (`trafilatura`)** | BeautifulSoup4 | **50–70 % weniger Boilerplate-Tokens** |
| **UI-Steuerung** (`computer_control.py`) | Reine PyAutoGUI-Koordinaten | **Windows UI Automation** | PyAutoGUI | Resilient gegen Auflösungs- & DPI-Änderungen |
| **YouTube-Extraktion** (`youtube_video.py`) | Regex-Scraping | **yt-dlp (`yt_dlp`)** | HTML-Regex + Transcript API | Unempfindlich gegen YouTube Layout-Updates |
| **App-Auflösung** (`open_app.py`) | Filesystem-Scans pro Aufruf | **Persistent Shortcut Cache** | Startmenü-/PATH-Scan | **<0,5 ms Lookup** im Cache |

---

## 5. Sicherheits- & Bestätigungsarchitektur

- **Unumkehrbare Aktionen (`core/confirm.py`)**: Herunterfahren, Neustart, WLAN-Umschaltung und das automatische Herunterfahren nach Spiele-Downloads (`game_updater`) legen einen Banner mit CONFIRM/CANCEL aufs HUD und kehren sofort zurück — die Aktion läuft nur, wenn ein Mensch CONFIRM drückt. Das Sprachmodell kann diese Schranke niemals selbst bestätigen; ohne gebundenes Interface wird fail-closed abgelehnt.
- **SSRF-Schutz (`actions/summarize.py`)**: `_blocked_host` verhindert den Zugriff auf private IP-Bereiche (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`) und Cloud-Metadaten-Endpunkte (`169.254.169.254`). Redirects werden manuell verfolgt (max. 5 Hops) und jeder Hop erneut geprüft.
- **URL-Schemata (`actions/browser_control.py`)**: Nur `http(s)` wird geöffnet; `file:`-, `javascript:`-, `data:`- und andere Schemata werden abgewiesen, damit keine lokalen Dateien ins Modellkontext gelesen und kein Seiten-JavaScript ausgeführt werden kann.
- **Home-Sandbox für Dateien**: `file_controller`, `file_processor`, `document_qa`, `summarize` und `code_agent` lösen Pfade über Symlinks auf und verweigern alles außerhalb des Home-Verzeichnisses (plus Temp-Verzeichnis für Leseaktionen). Archive werden Zip-Slip-geprüft entpackt, Ausgabeformate validiert.
- **Skript-Injektion**: Modelltexte, die in PowerShell/AppleScript-Quelltext interpoliert werden (Fensterfokus, Wallpaper, macOS-Benachrichtigungen), werden bei Anführungszeichen abgewiesen bzw. entschärft; Scheduler-XML wird escaped, `at`-Kommandos gequotet.
- **open_app installiert niemals**: Installer-/Update-/Repair-Absichten (DE+EN, inkl. „mach was du willst“) sowie Installer-ähnliche Ziele werden verweigert; der Start wird per Prozesstabelle verifiziert, Windows-Fehlercodes (z. B. WinError 1223) werden in eine Diagnose übersetzt.
- **Schließen nur über Prozesse**: `close_app`/`computer_settings` nutzen denselben Resolver wie `open_app`; Tastenkürzel-Schließungen (Alt+F4/Ctrl+W), Raten bei unbenanntem Ziel und das Schließen von JARVIS selbst, System- oder Shell-Prozessen sind ausgeschlossen; geteilte Runtimes (java/python/…) werden nie terminiert.
- **Kein Shell-Start**: Kein modelgesteuerter String erreicht je eine Shell (`shell=True` kommt nur noch in Kommentaren vor); alle Subprozesse laufen als Argumentvektoren.
- **Dashboard LAN-Schutz (`dashboard/server.py`)**: Das Web-Dashboard lauscht standardmäßig ausschließlich auf `127.0.0.1`. Die Freigabe ins lokale Netzwerk erfordert ein explizites Opt-in im Einstellungsmenü.

---

## 6. Lizenzhinweise

- **PyMuPDF (`pymupdf`)**: Lizenziert unter GNU AGPL v3. Bei Weiterverbreitung oder Bereitstellung als Netzwerkdienst müssen die Copyleft-Bedingungen beachtet werden. Für restriktivere Umgebungen stehen die permissiven Fallbacks `pdfplumber` (MIT) und `PyPDF2` (BSD) bereit.
- **trafilatura, uiautomation, yt-dlp**: Lizenziert unter Apache 2.0 bzw. The Unlicense (Public Domain). Vollständig kompatibel mit Open-Source-Standards.

---

## 7. Teststatus & Validierung
- **Vollständige Validierung**: `python3 test_overall.py` $\to$ **7 Passed, 1 Failed, 1 Skipped** in Minimal-Umgebungen (`wake_isolation` braucht `numpy`/`openwakeword`; mit installierten Deps 8/0/1), 206+ Checks grün.
- **Unit-Tests**: `pytest tests/` $\to$ Alttests wie zuvor (12/12 mit allen optionalen Deps); NEU und ohne Drittabhängigkeiten lauffähig: **89/89 PASS** (`test_agent` 27, `test_voice_local` 12, `test_reconnect` 12, `test_session_memory` 9, `test_app_lifecycle` 29).
- **Aktions-Discovery**: 22/22 `TOOL`-Deklarationen aktiv, 0 zurückgewiesen, alle Schema-Namen valide.

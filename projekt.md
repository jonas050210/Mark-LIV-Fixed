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
                        ┌──────────────────────┴──────────────────────┐
                        ▼                                             ▼
          ┌───────────────────────────┐                 ┌───────────────────────────┐
          │     Gemini Live Core      │                 │  Jev Ultrafast Agent Core │
          │ (Realtime Audio, Vision,  │                 │ (CDP Harness, Speculative │
          │  Continuous Tool Calls)   │                 │  Plan, DOM Tree Snapshot) │
          └─────────────┬─────────────┘                 └─────────────┬─────────────┘
                        │                                             │
                        └──────────────────────┬──────────────────────┘
                                               ▼
                               ┌───────────────────────────────┐
                               │ Action Loader & Tool Registry │
                               │   (19 Aktive Bundled Skills)  │
                               └───────────────┬───────────────┘
                                               │
               ┌───────────────────────────────┴───────────────────────────────┐
               ▼                               ▼                               ▼
┌─────────────────────────────┐ ┌─────────────────────────────┐ ┌─────────────────────────────┐
│    Automatisierung & UI     │ │    Dokumente & Analyse      │ │    Medien, Web & System     │
│ • computer_control (UIA)    │ │ • document_qa (PyMuPDF)     │ │ • youtube_video (yt-dlp)    │
│ • open_app (Shortcut-Cache) │ │ • summarize (trafilatura)   │ │ • web_search (Dual Tier)    │
│ • browser_agent (Jev)       │ │ • file_processor            │ │ • system_monitor            │
│ • code_agent (Scaffold/Fix) │ │ • file_controller           │ │ • audio_device / settings   │
│ • desktop_control / notify  │ │                             │ │ • timer / reminder          │
└─────────────────────────────┘ └─────────────────────────────┘ └─────────────────────────────┘
```

---

## 3. Die 19 aktiven Aktionen (`actions/`)

Alle 19 Aktionen deklarieren ein standardisiertes `TOOL`-Schema und werden zur Laufzeit von `core/action_loader.py` automatisch geladen:

1. `audio_device`: Verwaltet und wechselt Ein- und Ausgabegeräte (Mikrofon/Lautsprecher) namentlich und prüft deren Hardware-Verfügbarkeit.
2. `autonomous_browser_task` (`browser_agent.py`): Autonome Multi-Step-Browsersteuerung über die Jev Ultrafast Engine via direktes Chrome DevTools Protocol (CDP).
3. `browser_control`: Deterministische, skriptbasierte Browserautomatisierung mittels Playwright (Recherche, Navigation, Formulareingabe).
4. `code_agent`: Vollwertiger Entwicklungs-Agent. Konsolidiert Einzeldatei-Codebearbeitung und -Ausführung mit Multi-File-Projekt-Scaffolding und Fix-Loops.
5. `computer_control`: Direkte Eingabesteuerung. Nutzt primär Windows UI Automation (`uiautomation`) für semantische Interaktion mit Buttons, Feldern und Fenstern, mit nahtlosem `pyautogui`-Koordinaten-Fallback.
6. `computer_settings`: Verwaltung von Systemeinstellungen (Lautstärke, Helligkeit, WLAN, Energieoptionen, Prozessverwaltung).
7. `desktop_control` (`desktop.py`): Desktop-Organisation, Wallpaper-Wechsel, Bereinigung und Fensterverwaltung.
8. `document_qa`: Fragestellungen zu PDF-, Word-, Excel- und PowerPoint-Dateien. Verwendet Tier 1 `PyMuPDF` (`pymupdf`) für bis zu 11-fach schnellere Textextraktion mit Fallbacks auf `pdfplumber` und `PyPDF2`.
9. `file_controller`: Datei- und Verzeichnisoperationen (Erstellen, Verschieben, Löschen mit Papierkorb-Schutz, Auflisten).
10. `file_processor`: Parsing, Formatkonvertierung und Tabellenanalyse für Arbeitsdateien.
11. `game_updater`: Überprüfung und Verwaltung von Spielaktualisierungen auf Steam und Epic Games.
12. `notify`: Desktop-Benachrichtigungen über das Betriebssystem mit mehrstufigen Rückfallpfaden.
13. `open_app`: Starten beliebiger Programme. Nutzt einen In-Memory- und persistenten Cache (`~/.jarvis_app_cache.json`) für Auflösung unter 1 ms mit Startmenü-Indexierung.
14. `reminder`: Zeitgesteuerte Erinnerungen über betriebssystemeigene Scheduler (Windows Taskplaner, macOS LaunchAgent, Linux systemd).
15. `send_message`: Versand von Nachrichten über installierte Messenger wie WhatsApp oder Telegram.
16. `summarize`: Komprimiert Texte, Zwischenablage, URLs oder Dokumente. Nutzt `trafilatura` für saubere HTML-Extraktion ohne Navigations- und Cookie-Müll mit BeautifulSoup-Fallback und SSRF-Schutz.
17. `timer`: Gesprochene Countdown-Timer in Hintergrund-Daemon-Threads mit Sprachansage.
18. `web_search`: Parallele Websuche über Gemini Grounded Search und DuckDuckGo (ddgs).
19. `youtube_video`: Wiedergabe, Metadatenabruf und Transkript-Zusammenfassungen. Nutzt `yt-dlp` für robuste Video-Infos und automatischen Untertitel-Fallback.

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

- **Unumkehrbare Aktionen (`core/confirm.py`)**: Destruktive Systembefehle (Herunterfahren, Neustart, Trennen von Verbindungen) erfordern einen kryptografischen Token, der ausschließlich durch eine manuelle Benutzeraktion im HUD erzeugt wird. Das Sprachmodell kann diese Schranke niemals selbst bestätigen.
- **SSRF-Schutz (`actions/summarize.py`)**: `_blocked_host` verhindert den Zugriff auf private IP-Bereiche (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`) und Cloud-Metadaten-Endpunkte (`169.254.169.254`).
- **Dashboard LAN-Schutz (`dashboard/server.py`)**: Das Web-Dashboard lauscht standardmäßig ausschließlich auf `127.0.0.1`. Die Freigabe ins lokale Netzwerk erfordert ein explizites Opt-in im Einstellungsmenü.

---

## 6. Lizenzhinweise

- **PyMuPDF (`pymupdf`)**: Lizenziert unter GNU AGPL v3. Bei Weiterverbreitung oder Bereitstellung als Netzwerkdienst müssen die Copyleft-Bedingungen beachtet werden. Für restriktivere Umgebungen stehen die permissiven Fallbacks `pdfplumber` (MIT) und `PyPDF2` (BSD) bereit.
- **trafilatura, uiautomation, yt-dlp**: Lizenziert unter Apache 2.0 bzw. The Unlicense (Public Domain). Vollständig kompatibel mit Open-Source-Standards.

---

## 7. Teststatus & Validierung
- **Vollständige Validierung**: `python3 test_overall.py` $\to$ **8 Passed, 0 Failed, 1 Skipped** (`wake_word` optional).
- **Backend-Tests**: `tests/test_modern_backends.py` $\to$ **5/5 PASS**.
- **Fallback-Tests**: `tests/test_fallbacks_verification.py` $\to$ **4/4 PASS**.
- **Jev Integration & Failure Tests**: `tests/test_jev_integration.py` & `tests/test_failures.py` $\to$ **15/15 PASS**.

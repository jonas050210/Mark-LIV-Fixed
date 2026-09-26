# MARK LIV — Deep Bug Hunt (Kurzreport)

**Datum:** 26.09.2026 (UTC)  
**Branch:** `arena/01a0dddc-mark-liv-fixed`  
**Scope:** alle 179 getrackten Projektdateien geprüft, darunter 158 Python-Dateien und 56 Testdateien. Die Prüfung umfasste Root-/Setup-/CI-Dateien, `actions/`, `core/`, `dashboard/`, `memory/`, UI sowie Tests und Assets.

## Ergebnis

Es wurden **keine Produktionsfixes im Rahmen dieser Bug-Hunt-Runde** vorgenommen. Die folgenden Befunde sind reproduziert bzw. quellenbasiert bestätigt; sie sind von bloßen Stilhinweisen getrennt.

### Bestätigte Bugs

| ID | Priorität | Datei/Stelle | Befund und Auswirkung |
|---|---|---|---|
| B1 | Hoch | `core/app_index.py:605–614` | `load_index()` ruft `float(cache["built_at"])` außerhalb eines `try` auf. Ein formal akzeptierter Cache mit `built_at: "not-a-number"` beendet den Ladevorgang mit `ValueError`, statt den Cache zu verwerfen und neu aufzubauen. App-Suche/Launcher können dadurch fehlschlagen. |
| B2 | Mittel | `core/app_index.py:172–188` | `_source_signature()` prüft nur den Discovery-Ordner und dessen unmittelbare Unterordner. Änderungen in tieferen vorhandenen Verzeichnissen werden nicht erkannt; neue/entfernte Anwendungen können bis zum TTL-Ablauf unsichtbar bleiben. |
| B3 | Niedrig/Mittel | `core/app_index.py:617–629` | Ein gültiger, frischer Cache mit `entries: []` wird wegen `if raw` nicht als Cache-Hit akzeptiert. `load_index()` scannt und schreibt den Index bei jedem Aufruf erneut — unnötige I/O/Scans besonders auf Systemen ohne gefundene Apps. Repro: zwei Aufrufe führten zu `build_index.call_count == 2`. |
| B4 | Mittel | `core/app_index.py:610–614` | Eine nichtnumerische `signature` wird im Fehlerpfad als „nicht bewegt“ behandelt (`moved = False`). Ein beschädigter Cache mit `signature: "garbage"` wurde dadurch ohne Rescan als gültig ausgeliefert; veraltete Einträge können dauerhaft vertraut werden, bis TTL/Refresh greift. |
| B5 | Mittel | `actions/open_app.py:30–80`, `core/app_index.py:504–538` | Unter Linux übersetzen die Aliase `word`, `excel` und `powerpoint` zu `libreoffice --writer/calc/impress`. Der Desktop-Index speichert aber nur das Binary, und der Launcher übergibt den Alias-Suffix nicht als Argument. Instrumentierter Lauf: `word` matchte `LibreOffice`, `LAUNCH_ARGS == []`; damit startet die falsche LibreOffice-Komponente (generische Startseite statt Writer). |
| B6 | Mittel (latent) | `core/llm_client.py:363–401` | `call_llm_text()` ignoriert `get_llm_provider()` und sendet immer an Ollama `/api/chat`. Bei `llm_provider: "openai"` wird nicht — wie bei `call_llm()` und Streaming — `/v1/chat/completions` verwendet. Mock-Repro: OpenAI-Konfiguration, tatsächlich aufgerufener Endpoint `.../api/chat`. Im aktuellen Repository gibt es keine Aufrufer; der Legacy-Helper bleibt bei Nutzung defekt. |

### Separater, noch nicht reproduzierter Risiko-Kandidat

- `core/wake_word.py:336–364` schreibt `stop()` direkt in `process.stdin`, während `_send_loop()` in `:392–410` gleichzeitig Audio-Frames in denselben Stream schreiben kann. Ohne gemeinsamen Schreib-Lock besteht ein Protokoll-/Frame-Race beim Stoppen. Dieser Befund wurde statisch lokalisiert, aber in dieser Umgebung nicht durch einen echten parallelen Pipe-Lauf reproduziert und daher nicht als bestätigter Bug gezählt.

## Verifikation

- `QT_QPA_PLATFORM=offscreen python .github/scripts/run_tests.py`  
  **752 Tests, 0 Fehler, 28 legitime Skips**.
- `QT_QPA_PLATFORM=offscreen python .github/scripts/run_overall.py --coverage`  
  **PASS: 11 Checks bestanden, 0 fehlgeschlagen, 1 Windows-Integration übersprungen; 38 % Gesamt-Coverage (Floor 36 %); 26 Safety-Critical-Module über ihrem Floor; 27 Dashboard-Routen kontraktgeprüft.**
- Zusätzlich gezielte Regressionen für B1–B4, Office-Aliase (B5) und den LLM-Endpoint (B6) ausgeführt.
- Nicht reproduzierbare Umgebungsgrenzen: keine reale Windows-Hardware, kein echtes Windows-COM-/Audio-/Wake-Word-Setup. Die 28 Test-Skips sind umgebungs-/plattformbedingt.

## Arbeitsbaum

Die Hunt-Runde hat keine Produktionsdatei geändert. Die bereits vorhandene Änderung an `core/explorer.py` wurde weder erzeugt noch zurückgesetzt; sie ist nicht Teil dieses Reports.

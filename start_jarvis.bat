@echo off
rem Startet Mark LIII mit Python 3.12 (py-Launcher). Konsole bleibt bei Fehlern offen.
cd /d "%~dp0"
py -3.12 -u main.py
if errorlevel 1 (
    echo.
    echo Mark LIII wurde mit einem Fehler beendet. Siehe Ausgabe oben.
    pause
)

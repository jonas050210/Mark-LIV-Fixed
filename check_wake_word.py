"""
check_wake_word.py — findet in 10 Sekunden heraus, ob die Wake-Word-Engine
das ist, was MARK beim ⚙-Druck still abstürzen lässt.

Ausführen:   python check_wake_word.py

Jeder Import-Test läuft in seinem eigenen SUBPROCESS. Ein harter nativer
Absturz (Access Violation, DLL-Fehler) reißt den Unterprozess mit — aber
niemals diesen Checker selbst, der meldet es dann sauber. Genau diese
Crash-Klasse erzeugt keinen Python-Traceback und ist der Grund, warum die
App sich "ohne Fehler" geschlossen hat.
"""
import importlib.util
import subprocess
import sys

# German Windows consoles default to cp850, which cannot encode arrows,
# stars or em-dashes — the same trap main.py defuses at startup. Do the
# same here so the report always prints.
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# (Anzeigename, Import-Statement) — in dieser Reihenfolge, weil onnxruntime
# die Last ist, die openwakeword beim Import zieht.
CHECKS = [
    ("numpy",        "import numpy"),
    ("onnxruntime",  "import onnxruntime"),
    ("openwakeword", "import openwakeword"),
]


def run_import(stmt: str) -> subprocess.CompletedProcess:
    code = (
        "import faulthandler, sys; faulthandler.enable(); "
        + stmt +
        "; print('IMPORT_OK')"
    )
    try:
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        # Nachbau des CompletedProcess-Vertrags: returncode 0 wäre "ok",
        # also etwas Unmögliches wählen.
        class _R:
            returncode = -99
            stdout = ""
            stderr = "TIMEOUT nach 120s"
        return _R()


def verdict(proc) -> str:
    """'OK' | 'PY_ERROR' | 'NATIVE_CRASH' | 'TIMEOUT'"""
    if proc.returncode == 0 and "IMPORT_OK" in (proc.stdout or ""):
        return "OK"
    if proc.returncode == -99:
        return "TIMEOUT"
    err = (proc.stderr or "")
    if "Traceback" in err or "Error" in err or "ModuleNotFound" in err:
        return "PY_ERROR"
    # Prozess gestorben, keine Python-Spur → genau der gesuchte Fall.
    return "NATIVE_CRASH"


def main() -> None:
    print("=" * 64)
    print(" MARK LIV - Wake-Word Crash-Checker")
    print("=" * 64)

    try:
        installed = importlib.util.find_spec("openwakeword") is not None
    except Exception:
        installed = False

    print(f"\nPython:  {sys.version.split()[0]}  ({sys.executable})")
    print(f"openwakeword installiert: {'JA' if installed else 'NEIN'}\n")

    if not installed:
        print("ERGEBNIS: openwakeword ist NICHT installiert.")
        print()
        print("→ Der fruehere Crash-Ausloeser (import auf dem GUI-Thread beim")
        print("  ⚙-Klick) kann gar nicht mehr greifen. Mit dem Fix aus dem")
        print("  Repo oeffnet das Settings-Panel sofort, ohne Freeze.")
        print("→ Falls du Wake-Word spaeter nutzen willst: im App-UI unter")
        print("  ⚙ → WAKE WORD einmalig herunterladen (oder pip install openwakeword).")
        return

    any_native = False
    for name, stmt in CHECKS:
        sys.stdout.write(f"Teste import {name:<13} ... ")
        sys.stdout.flush()
        v = verdict(run_import(stmt))
        if v == "OK":
            print("OK")
        elif v == "PY_ERROR":
            print("Python-Fehler (siehe Hinweis unten)")
        elif v == "TIMEOUT":
            print("TIMEOUT (>120s) — laedt endlos")
        else:
            any_native = True
            print("★ NATIVER ABSTURZ (kein Traceback!) ← DAS ist dein Absturz")

    print()
    print("-" * 64)
    if any_native:
        print("BEFUND: Mindestens ein Paket crash nativ beim Import — genau")
        print("das schliesst die App still, weil kein Python-Traceback entsteht.")
        print()
        print("HEILUNG (in einer Konsole ausfuehren):")
        print("  1.  pip install --force-reinstall onnxruntime")
        print("  2.  Falls immer noch:  pip install --force-reinstall openwakeword")
        print("  3.  Wake-Word gar nicht im Einsatz? Dann reicht auch:")
        print("      pip uninstall -y openwakeword")
        print("      (danach zeigt der ⚙-Button nur noch DOWNLOAD an, nichts")
        print("       wird mehr importiert — der Absturz ist garantiert weg.)")
    else:
        print("BEFUND: Kein nativer Crash beim Import dieser Umgebung.")
        print("Die Pakete laden hier sauber. Mit dem ⚙-Fix aus dem Repo ist der")
        print("fruehere Einfrier-/Absturzpfad (import auf dem GUI-Thread) ohnehin")
        print("entfernt — der Klick importiert jetzt nichts Schweres mehr.")
        print()
        print("Falls die App trotzdem noch schliesst: python main.py aus der")
        print("Konsole starten, ⚙ druecken, und die Konsolenausgabe schicken")
        print("(der faulthandler in main.py zeigt jetzt auch native Abstuerze an).")


if __name__ == "__main__":
    main()

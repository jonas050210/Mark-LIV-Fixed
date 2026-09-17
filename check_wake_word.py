"""
check_wake_word.py — findet in 10 Sekunden heraus, ob die Wake-Word-Engine
das ist, was MARK beim ⚙-Druck still abstürzen lässt.

Ausführen:   python check_wake_word.py

Jeder Import-Test läuft in seinem eigenen SUBPROCESS. Ein harter nativer
Absturz (Access Violation, DLL-Fehler) reißt den Unterprozess mit — aber
niemals diesen Checker selbst, der meldet es dann sauber. Genau diese
Crash-Klasse erzeugt keinen Python-Traceback und ist der Grund, warum die
App sich "ohne Fehler" geschlossen hat.

Seit dem Prozess-Isolations-Fix läuft die Engine genau so: in einem eigenen
Prozess (core/wake_worker.py). Schritt 4 dieses Checkers startet deshalb
denselben Worker mit echten Modell-Dateien — wenn das hier klappt, klappt es
auch in der App, und wenn es hier crasht, crasht nur dieses Test-Programm.

Die zugehörigen Entwickler-Tests (Protokoll, Crash-Überleben, keine
Waisenprozesse, kein openwakeword-Import im App-Prozess) stehen in
check_wake_isolation.py.
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


def engine_selftest() -> tuple[str, str]:
    """Startet die echte Engine (core/wake_worker.py --mode selftest) in einem
    eigenen Prozess: import, Modell laden, eine Vorhersage auf 1 s Stille.

    Rückgabe: ('OK' | 'PY_ERROR' | 'NATIVE_CRASH' | 'TIMEOUT' | 'NO_MODELS',
               Klartext-Meldung).
    """
    try:
        from core.wake_word import is_ready, selftest
    except Exception as e:
        return "PY_ERROR", f"core/wake_word.py konnte nicht geladen werden: {e}"
    if not is_ready():
        return "NO_MODELS", ("openwakeword ist installiert, aber die Modelldateien "
                             "fehlen (resources/models). In der App: ⚙ → WAKE WORD "
                             "→ DOWNLOAD, oder `python -c \"import openwakeword.utils "
                             "as u; u.download_models(['hey_jarvis'])\"`.")
    try:
        # selftest() läuft selbst in einem Kindprozess: ein nativer Absturz
        # landet dort und nicht hier.
        ok, detail = selftest(logger=lambda _m: None)
        if ok:
            return "OK", detail
        # Die Meldung stammt aus core.wake_word.describe_exit() — an ihrer
        # Wortwahl ist erkennbar, ob der Kindprozess nativ gestorben ist
        # (kein Traceback) oder sauber mit einem Python-Fehler.
        low = (detail or "").lower()
        native = ("died during import" in low or "native failure" in low
                  or "access violation" in low or "dll" in low
                  or "killed by signal" in low)
        return ("NATIVE_CRASH" if native else "PY_ERROR"), detail
    except Exception as e:
        return "PY_ERROR", f"{type(e).__name__}: {e}"


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
        print("  ⚙-Klick) kann gar nicht mehr greifen: Ohne Paket zeigt der")
        print("  ⚙-Button nur DOWNLOAD an, und es wird nichts importiert.")
        print("→ Die App kann darüber nicht mehr abstürzen —Wake-Word ist dann")
        print("  einfach aus, der Rest von MARK läuft normal weiter.")
        print("→ Falls du Wake-Word nutzen willst: im App-UI unter")
        print("  ⚙ → WAKE WORD einmalig herunterladen (oder pip install openwakeword)")
        print("  und diesen Checker danach noch einmal laufen lassen.")
        return

    print("1) Importe in isolierten Unterprozessen")
    any_native = False
    for name, stmt in CHECKS:
        sys.stdout.write(f"   teste import {name:<13} ... ")
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
            print("★ NATIVER ABSTURZ (kein Traceback!) ← DAS war dein Absturz")

    # The engine test is the one that matters now: it exercises exactly the code
    # path the app uses (same worker script, same protocol), with the real model.
    print("\n2) Engine im isolierten Prozess (der Weg, den die App jetzt geht)")
    sys.stdout.write("   starte core/wake_worker.py --mode selftest ... ")
    sys.stdout.flush()
    ev, edetail = engine_selftest()
    if ev == "OK":
        print("OK")
        print(f"   {edetail}")
    elif ev == "NO_MODELS":
        print("MODELLE FEHLEN")
    elif ev == "TIMEOUT":
        print("TIMEOUT")
    elif ev == "NATIVE_CRASH":
        any_native = True
        print("★ NATIVER ABSTURZ")
    else:
        print("FEHLER")
    if ev != "OK":
        for line in (edetail or "").splitlines()[:6]:
            print(f"   {line[:200]}")

    print()
    print("-" * 64)
    if ev == "OK":
        print("BEFUND: Die Wake-Word-Engine läuft in einem isolierten Prozess.")
        print("Genau so startet die App sie jetzt auch — ein Absturz der Engine")
        print("beendet damit nur noch diesen Kindprozess: MARK bleibt offen,")
        print("meldet es im Aktivitäts-Log und hört einfach normal weiter zu.")
        print()
        print("In der App: ⚙ → WAKE WORD einschalten. Im Log steht danach")
        print("'Wake word: engine ready in child pid …' — das ist die Bestätigung.")
        if any_native:
            print()
            print("Hinweis: Mindestens ein Import crasht in dieser Umgebung nativ.")
            print("Die App überlebt das jetzt, aber Wake-Word geht damit nicht.")
            print("Heilung:  pip install --force-reinstall onnxruntime")
    elif ev == "NO_MODELS":
        print("BEFUND: Pakete sind da, Modelldateien fehlen.")
        print(f"→ {edetail}")
    elif any_native:
        print("BEFUND: Mindestens ein Paket crash nativ beim Import — das ist")
        print("die Fehlerklasse, die die App früher still beendet hat. Seit dem")
        print("Isolations-Fix stirbt dabei nur noch der Engine-Prozess, nicht MARK.")
        print()
        print("HEILUNG (in einer Konsole ausfuehren):")
        print("  1.  pip install --force-reinstall onnxruntime")
        print("      (ein anderer Build kollidiert oft schon nicht mehr)")
        print("  2.  Falls immer noch:  pip install --force-reinstall openwakeword")
        print("  3.  Wake-Word gar nicht im Einsatz? Dann reicht auch:")
        print("      pip uninstall -y openwakeword")
        print("      (danach zeigt der ⚙-Button nur noch DOWNLOAD an, nichts")
        print("       wird mehr importiert — der Absturz ist garantiert weg.)")
        print()
        print("  Danach diesen Checker noch einmal laufen lassen: Punkt 2 muss OK sein.")
    else:
        print("BEFUND: Die Engine ist in einem eigenen Prozess nicht sauber")
        print("hochgekommen. Die Meldung oben sagt, woran es lag.")
        print()
        print("Falls die App trotzdem noch schliesst: python main.py aus der")
        print("Konsole starten, ⚙ druecken, und die Konsolenausgabe schicken")
        print("(der faulthandler in main.py UND im Worker zeigt native")
        print("Abstuerze an; die Engine schreibt ihre Fehler in dieselbe Konsole).")


if __name__ == "__main__":
    main()

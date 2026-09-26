"""
MARK LIV — one-time setup.

Use ``python setup.py --check`` for an offline, non-destructive validation of
Python compatibility, requirements, and shipped assets. Without ``--check``
the script installs dependencies and optionally downloads Playwright browsers.

MARK LIV supports Windows only. Check mode remains portable so contributors and
CI can validate a checkout without installing or launching the application.
Normal setup refuses non-Windows hosts before invoking pip. It then installs the
Windows dependency set and Playwright browsers used for web automation.

Two things it deliberately does NOT install:
  * the optional local wake word ("Hey Jarvis") — one-click, opt-in, from
    ⚙ → WAKE WORD inside the app;
  * anything for the avatar — the holographic head renders in software on the
    PyQt6 and numpy already listed here. No GPU, no OpenGL, no extra packages.
"""
import platform
import subprocess
import sys
from importlib import metadata
from pathlib import Path

# Setup is commonly launched from legacy Windows consoles. Keep status symbols
# from turning a valid installation into a UnicodeEncodeError.
for _stream_name in ("stdout", "stderr"):
    try:
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

OS = platform.system()  # "Windows" | "Darwin" | "Linux"
CHECK_ONLY = "--check" in sys.argv[1:]
HERE = Path(__file__).resolve().parent

MIN_PY = (3, 11)        # hard floor: below this the syntax used here won't parse
MAX_PY = (3, 13)        # highest version this is actually tested on


def _run(label: str, args: list[str]) -> None:
    print(f"\n▶ {label}")
    subprocess.run(args, check=True, timeout=1800)


def _replace_deprecated_pynvml() -> None:
    """Migrate the retired ``pynvml`` distribution without changing its API.

    ``nvidia-ml-py`` intentionally exports the same ``pynvml`` import name.
    Old installs can contain both distributions; Python then imports the
    deprecated one and emits a warning every time MARK LIV starts. Remove only
    the retired distribution and reinstall the maintained provider's files.
    """
    try:
        metadata.version("pynvml")
    except metadata.PackageNotFoundError:
        return
    print("\n▶ Replacing deprecated pynvml with maintained nvidia-ml-py…")
    removed = subprocess.run(
        [sys.executable, "-m", "pip", "uninstall", "-y", "pynvml"],
        timeout=180, check=False,
    )
    if removed.returncode != 0:
        print("⚠️  Could not remove deprecated pynvml; GPU readings still work, but its warning may remain.")
        return
    try:
        _run("Restoring NVIDIA ML bindings…", [
            sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps",
            "nvidia-ml-py>=12,<14",
        ])
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("⚠️  NVIDIA ML bindings could not be restored automatically; GPU metrics are optional.")


def _check_python() -> None:
    """Fail immediately and clearly rather than deep inside a pip resolver.

    A wrong interpreter is the single most common way this install goes sideways,
    and the error it produces on its own names a wheel, not the real problem.
    """
    v = sys.version_info[:2]
    if v > MAX_PY:
        # Newer is a warning, not a wall. Turning away someone who installed
        # today's Python is a worse first impression than a version that
        # turns out to work fine, and if a wheel really is missing pip says
        # so plainly.
        print(f"\n⚠️  Python {v[0]}.{v[1]} is newer than the "
              f"{MAX_PY[0]}.{MAX_PY[1]} this is tested on. Continuing — if a "
              f"package has no wheel yet, install Python "
              f"{MAX_PY[0]}.{MAX_PY[1]} and run setup with that.")
        return
    if v < MIN_PY:
        print(f"\n❌ Python {v[0]}.{v[1]} detected — MARK LIV needs at "
              f"least Python {MIN_PY[0]}.{MIN_PY[1]}.")
        print("   Install a supported Windows Python version and run setup with it:")
        print(f"     py -{MIN_PY[0]}.{MIN_PY[1]} setup.py")
        sys.exit(1)


def _check_assets() -> None:
    """The avatar's face is a shipped file; a truncated clone should say so."""
    face = HERE / "core" / "face_model.obj"
    if not face.exists() or face.stat().st_size < 4096:
        print(
            "\n⚠️  core/face_model.obj is missing or truncated — the avatar will "
            "fall back to the plain glowing core.\n"
            "    Re-clone the repository, or fetch that one file again."
        )


def _check_install_inputs() -> None:
    """Validate setup inputs without changing the environment.

    This is intentionally separate from pip: it gives the overall test and a
    user troubleshooting an install a safe, offline check that cannot download
    packages or launch Playwright's browser installer.
    """
    requirements = HERE / "requirements.txt"
    if not requirements.is_file():
        raise FileNotFoundError(f"Missing {requirements}")
    entries: list[str] = []
    pending = [requirements]
    seen: set[Path] = set()
    while pending:
        source = pending.pop().resolve()
        if source in seen:
            continue
        seen.add(source)
        if not source.is_file() or source.parent != HERE:
            raise FileNotFoundError(f"Missing or unsafe requirements include: {source}")
        for raw in source.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith(("-r ", "--requirement ")):
                included = line.split(maxsplit=1)[1].strip()
                pending.append(HERE / included)
            else:
                entries.append(line)
    if not entries:
        raise ValueError("requirements files have no dependency entries")
    _check_assets()
    print(f"   requirements: {len(entries)} dependency entries across {len(seen)} files")
    print("   setup inputs and shipped assets are present")


def main() -> None:
    print(f"⚙  MARK LIV setup — detected OS: {OS or 'unknown'}, "
          f"Python {sys.version_info[0]}.{sys.version_info[1]}")
    _check_python()
    if CHECK_ONLY:
        print("\n🔎 Check-only mode — no packages or browsers will be installed.")
        if OS != "Windows":
            print("   Validation-only host: MARK LIV runtime support is Windows-only.")
        _check_install_inputs()
        print("\n✅ Setup check complete!")
        return

    if OS != "Windows":
        print("\n❌ MARK LIV supports Windows 10 and Windows 11 only.")
        print("   This host may run setup.py --check and the mock test suite, but not install or launch MARK LIV.")
        raise SystemExit(1)

    # requirements.txt aggregates the complete Windows dependency set.
    _run("Installing Python dependencies (OS-specific extras auto-filtered)…",
         [sys.executable, "-m", "pip", "install", "-r", str(HERE / "requirements.txt")])
    _replace_deprecated_pynvml()

    # Chromium covers Chrome/Edge/Opera/Brave/Vivaldi; Firefox for Firefox.
    # Not fatal: these are a few hundred megabytes from a CDN that a corporate
    # network or a flaky connection can refuse, and everything except browser
    # automation works without them. Failing the whole install there would send
    # a user away from a working app.
    try:
        _run("Installing Playwright browsers (chromium + firefox)…",
             [sys.executable, "-m", "playwright", "install", "chromium", "firefox"])
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"\n⚠️  Playwright browsers were not installed ({e}).")
        print("    Everything except browser automation works. Retry later with:")
        print(f'    {sys.executable} -m playwright install chromium firefox')

    _check_assets()

    try:
        import win32com.client  # noqa: F401
    except ImportError:
        postinstall = Path(sys.executable).parent / "Scripts" / "pywin32_postinstall.py"
        print(
            "\n⚠️  pywin32 did not register correctly — desktop-shortcut "
            "creation will use a slower fallback. To fix it, run:\n"
            f'    "{sys.executable}" -m pip install --force-reinstall pywin32\n'
            f'    "{sys.executable}" "{postinstall}" -install'
        )

    print("\n✅ Setup complete!")
    print("   1) Launch it:  python main.py")
    print("   2) Paste your free Gemini API key when the setup screen appears.")
    print("   3) (Optional) Enable 'Hey Jarvis' from ⚙ → WAKE WORD.")


if __name__ == "__main__":
    main()

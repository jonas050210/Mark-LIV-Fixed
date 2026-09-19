"""
MARK LIV — one-time setup.

Installs the Python dependencies for THIS operating system only: the OS-specific
packages in requirements.txt carry `sys_platform` markers, so a macOS or Linux
user never pulls Windows-only libraries (and vice-versa).

Setup is fully automatic: it skips satisfied Python requirements, installs
Chromium, Firefox and WebKit using Playwright's platform-specific builds, and
verifies packages and browser launches before reporting READY. No stdin is read.

    py setup.py                    automatic packages + all browser engines
    py setup.py --yes              compatibility alias for the default
    py setup.py --minimal          Python packages only, no browser binaries
    py setup.py --browsers firefox explicitly select a subset
    py setup.py --dry-run          show the plan without installing anything

Two things it deliberately does NOT install:
  * the optional local wake word ("Hey Jarvis") — one-click, opt-in, from
    ⚙ → WAKE WORD inside the app;
  * anything for the avatar — the holographic head renders in software on the
    PyQt6 and numpy already listed here. No GPU, no OpenGL, no extra packages.
"""
# Entry-point imports: build_parser() needs argparse before the first line of
# output, and the install steps need run_live. tests/test_setup_execution.py
# pins both, so a future edit that drops one of these fails the suite instead
# of shipping a setup script that crashes with a bare NameError.
import argparse
import importlib.metadata as importlib_metadata
import os
import platform
import subprocess
import sys
import traceback
from pathlib import Path

try:
    from core.command_runner import run_live
except ImportError as exc:
    # A truncated or partial clone must say so in plain words, not dump a
    # traceback the user has to decode.
    print(
        "\nNOT READY — setup.py cannot import core.command_runner "
        f"({exc.__class__.__name__}: {exc}).\n"
        "    A truncated or partial clone is the usual cause. Re-clone the "
        "repository and run setup again.",
        flush=True,
    )
    raise SystemExit(1)

OS = platform.system()  # "Windows" | "Darwin" | "Linux"
HERE = Path(__file__).resolve().parent

MIN_PY = (3, 11)        # hard floor: below this the syntax used here won't parse
MAX_PY = (3, 13)        # highest version this is actually tested on


# ── Non-interactive browser selection ────────────────────────────────────────
DEFAULT_BROWSERS = "all"
ENV_BROWSERS = "MARK_LIV_BROWSERS"
PLAYWRIGHT_ARGS = {
    "none": [],
    "chromium": ["chromium"],
    "firefox": ["firefox"],
    "webkit": ["webkit"],
    "chromium-firefox": ["chromium", "firefox"],
    "all": ["chromium", "firefox", "webkit"],
}
BROWSER_CHOICES = tuple(PLAYWRIGHT_ARGS)

_ALIASES = {
    "no": "none", "skip": "none", "minimal": "none", "0": "none",
    "chrome": "chromium", "cr": "chromium",
    "ff": "firefox",
    "both": "chromium-firefox",
    "chromium-firefox-webkit": "all",
    "firefox-chromium": "chromium-firefox",
}


def _run(label: str, args: list[str]) -> None:
    print(f"\n▶ {label}", flush=True)
    result = run_live(args, label=label, timeout=1200, heartbeat=5)
    if result.timed_out or result.returncode:
        raise subprocess.CalledProcessError(result.returncode or 1, args)
    print(f"  Done: {label} ({result.seconds:.1f}s)", flush=True)


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
        print("   Install a supported version and run setup with it, e.g.:")
        print(f"     py -{MIN_PY[0]}.{MIN_PY[1]} setup.py        (Windows)")
        print(f"     python{MIN_PY[0]}.{MIN_PY[1]} setup.py      (macOS / Linux)")
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


# ── Browser choice ───────────────────────────────────────────────────────────

def normalize_browsers(value) -> str | None:
    """Map user input onto one of BROWSER_CHOICES, or None if unrecognised."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    for sep in ("_", " ", "+", ",", "/"):
        text = text.replace(sep, "-")
    while "--" in text:
        text = text.replace("--", "-")
    text = _ALIASES.get(text, text)
    return text if text in BROWSER_CHOICES else None


def playwright_install_args(choice: str) -> list[str]:
    """The `playwright install` arguments for *choice* ([] means: skip)."""
    return list(PLAYWRIGHT_ARGS[normalize_browsers(choice) or "none"])


def browser_retry_command(choice: str) -> str:
    """Copy-pasteable command to fetch the chosen browsers later."""
    args = playwright_install_args(choice)
    if not args:
        return ""
    return f"{Path(sys.executable).name or 'python'} -m playwright install " + " ".join(args)


def resolve_browsers(args, interactive: bool | None = None, environ=None) -> str:
    """Resolve skip flags > --browsers > environment > all engines.

    ``interactive`` remains accepted for callers of the old API, but is ignored.
    Setup never reads stdin, including in a terminal and during dry runs.
    """
    environ = os.environ if environ is None else environ
    skip = bool(getattr(args, "minimal", False) or getattr(args, "skip_browsers", False))
    explicit = normalize_browsers(getattr(args, "browsers", None))

    if skip and explicit and explicit != "none":
        print("  ! --minimal/--skip-browsers wins over "
              f"--browsers {explicit}; no browser binaries will be downloaded.")
    if skip:
        return "none"
    if explicit:
        return explicit

    raw_env = (environ.get(ENV_BROWSERS) or "").strip()
    if raw_env:
        env_choice = normalize_browsers(raw_env)
        if env_choice:
            print(f"  Using {ENV_BROWSERS}={env_choice} from the environment.")
            return env_choice
        print(f"  ! Ignoring {ENV_BROWSERS}={raw_env!r} — expected one of: "
              f"{', '.join(BROWSER_CHOICES)}.")

    return DEFAULT_BROWSERS


def _describe_plan(choice: str) -> str:
    args = playwright_install_args(choice)
    if not args:
        return "no browser binaries (minimal — browser automation can be added later)"
    return " + ".join(args)


# ── Setup ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description="MARK LIV automatic setup: Python dependencies and all "
                    "supported Playwright browser engines. No prompts.",
        epilog=f"Environment override: {ENV_BROWSERS}=" + "|".join(BROWSER_CHOICES),
    )
    parser.add_argument("--minimal", action="store_true",
                        help="install Python packages only — no Playwright browsers")
    parser.add_argument("--skip-browsers", action="store_true",
                        help="same as --browsers none")
    parser.add_argument("--browsers", choices=BROWSER_CHOICES, default=None,
                        metavar="CHOICE",
                        help="which browser binaries to download "
                             f"({', '.join(BROWSER_CHOICES)})")
    parser.add_argument("--yes", "-y", action="store_true",
                        help=f"compatibility flag: setup is always non-interactive "
                             f"({DEFAULT_BROWSERS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be installed, then exit")
    parser.add_argument("--requirements", default=str(HERE / "requirements.txt"),
                        help="requirements file to install (default: ./requirements.txt)")
    # Mark LIV 54: by default setup.py detects what's already installed and
    # installs only the missing packages. --force-full-install skips that and
    # reinstalls every requirement (the old behaviour). --auto makes the
    # detect-only path explicit so a script can rely on the flag without the
    # default flipping under it.
    parser.add_argument("--force-full-install", action="store_true",
                        help="skip the install-check and reinstall every "
                             "package in the requirements file")
    parser.add_argument("--auto", action="store_true",
                        help="explicit alias for the default auto-detect path "
                             "(installed packages are skipped, only missing "
                             "ones are downloaded)")
    return parser


# ── Auto-detection of already-installed packages ──────────────────────────────
def _requirement(spec):
    """Parse PEP 508 using packaging, or pip's bundled copy during bootstrap."""
    try:
        from packaging.requirements import Requirement
    except ImportError:
        from pip._vendor.packaging.requirements import Requirement
    return Requirement(spec)


def _read_requirements_lines(req_path):
    """Return (pip requirement, distribution name), preserving markers/pins.

    Directives are kept for pip, with an empty name. Inline comments are
    removed, but URL fragments (which have no preceding whitespace) survive.
    Invalid requirements fail explicitly rather than being marked installed.
    """
    if not req_path.exists():
        return []
    import re
    out = []
    for raw in req_path.read_text(encoding="utf-8").splitlines():
        line = re.split(r"\s+#", raw.strip(), maxsplit=1)[0].strip()
        if not line or line.startswith("#"):
            continue
        out.append((line, "" if line.startswith("-") else _requirement(line).name))
    return out


def _is_installed(spec):
    """Check distribution metadata and version, not a guessed import name.

    importlib.metadata normalises distribution names. This accepts renamed,
    namespace, binary and data-only distributions without importing them.
    An unrelated importable module is not evidence of an installed package.
    Non-applicable platform markers are satisfied without a lookup. Extras
    are left intact in the pip spec; this checks the named distribution.
    This is installation verification, not a native-library/runtime smoke test.
    """
    try:
        req = _requirement(spec)
        if req.marker is not None and not req.marker.evaluate():
            return True
        dist = importlib_metadata.distribution(req.name)
        version = dist.version
        return bool(version) and req.specifier.contains(version, prereleases=True)
    except (importlib_metadata.PackageNotFoundError, ValueError, TypeError):
        return False


def _missing_requirements(req_path):
    """Return missing or version-incompatible requirements for this platform."""
    return [line for line, name in _read_requirements_lines(req_path)
            if not name or not _is_installed(line)]


def _install_requirements(missing, req_path):
    """Install only the missing lines, with one pip call.

    Passing the whole requirements file when nothing is missing would still
    work but takes seconds; this branch is a fast exit. We always run pip
    with --upgrade-strategy only-if-needed so reinstalling is a no-op for
    packages already at the pinned version.
    """
    if not missing:
        print("\n  All required Python packages already installed \u2014 nothing to do.")
        return True
    print(f"\n  Installing {len(missing)} missing package(s):")
    for line in missing:
        # Trim very long lines for readability.
        pretty = line if len(line) <= 64 else line[:61] + "..."
        print(f"    \u2022 {pretty}")
    cmd = [sys.executable, "-m", "pip", "install",
           "--no-input", "--upgrade-strategy", "only-if-needed", *missing]
    if any(line.startswith("-") for line in missing):
        cmd = [sys.executable, "-m", "pip", "install", "--no-input", "-r", str(req_path)]
    try:
        _run("Installing missing Python packages", cmd)
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n\u26a0\ufe0f  pip exited with status {e.returncode}.")
        print(f"    The full requirements file is still available at: {req_path}")
        print(f"    You can install it manually with:  {Path(sys.executable).name} "
              f"-m pip install -r {req_path}")
        return False


def _verify_requirements(req_path):
    """Verify every applicable distribution/version and report each failure."""
    print("\n  Verifying installation...", flush=True)
    if not req_path.is_file():
        print(f"  Requirements file not found: {req_path}")
        return False
    bad = _missing_requirements(req_path)
    if not bad:
        print("  ✅ All applicable package distributions and versions verified.")
        return True
    print(f"  ⚠️  {len(bad)} requirement(s) missing, incompatible or unverifiable:")
    for line in bad:
        print(f"     - {line}")
    print(f'    Retry: "{sys.executable}" -m pip install -r "{req_path}"')
    return False


def _auto_install(req_path_str: str) -> bool:
    """Install missing/incompatible requirements; propagate verification failure."""
    req_path = Path(req_path_str)
    if not req_path.is_file():
        print(f"\n  ⚠️  Requirements file not found at {req_path}.")
        return False
    print(f"\n  Scanning {req_path.name} for missing packages...")
    missing = _missing_requirements(req_path)
    return _install_requirements(missing, req_path) and _verify_requirements(req_path)


def _browser_locations(browser: str) -> list[Path]:
    """Ask the installed Playwright version for its exact platform/revision paths.

    Includes Chromium's headless shell and shared tools; no guessed cache path
    or stale browser revision can accidentally satisfy this check.
    """
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--dry-run", browser],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True, timeout=30,
    )
    paths = [Path(line.split(":", 1)[1].strip())
             for line in result.stdout.splitlines()
             if line.strip().lower().startswith("install location:")]
    if not paths:
        raise RuntimeError(f"Playwright did not report install locations for {browser}")
    return paths


def _install_browsers(browsers: list[str]) -> bool:
    """Skip complete downloads, then verify every selected engine can launch."""
    ok = True
    for index, browser in enumerate(browsers, 1):
        print(f"\n[{index}/{len(browsers)}] Playwright {browser}: checking installation...",
              flush=True)
        try:
            locations = _browser_locations(browser)
            if all((path / "INSTALLATION_COMPLETE").is_file() for path in locations):
                print(f"  {browser}: already installed — skipping download.", flush=True)
            else:
                _run(f"Installing Playwright {browser}",
                     [sys.executable, "-m", "playwright", "install", browser])
            if not all(path.is_dir() for path in locations):
                raise RuntimeError("browser installation directories are missing")
            # Launch checks also detect missing native host libraries or damaged
            # binaries. Run out of process so even a broken driver is bounded.
            script = (
                "import sys\nfrom playwright.sync_api import sync_playwright\n"
                "with sync_playwright() as p:\n"
                "    b = getattr(p, sys.argv[1]).launch(headless=True, timeout=15000)\n"
                "    b.close()\n"
                "print('Browser launch verified')\n"
            )
            print(f"  Verifying {browser} headless launch...", flush=True)
            result = run_live([sys.executable, "-u", "-c", script, browser],
                              label=f"Verify {browser}", timeout=45)
            if result.timed_out or result.returncode:
                raise RuntimeError("headless launch failed (see output above)")
            print(f"  {browser}: verified.", flush=True)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            ok = False
            print(f"  FAILED: {browser}: {exc}\n"
                  f"  Retry: {browser_retry_command(browser)}\n"
                  "  If native host libraries are missing, install the libraries "
                  "listed above (Linux: python -m playwright install-deps).", flush=True)
    return ok


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"⚙  MARK LIV setup — detected OS: {OS or 'unknown'}, "
          f"Python {sys.version_info[0]}.{sys.version_info[1]}")
    _check_python()

    choice = resolve_browsers(args)
    browser_args = playwright_install_args(choice)
    print(f"\nPlan: Python packages from {args.requirements}")
    print(f"      Playwright browsers: {_describe_plan(choice)}")

    if args.dry_run:
        print("\nDry run — nothing was installed.")
        # Show what auto-detection would do, the same way the live run will.
        req = Path(args.requirements)
        if req.exists():
            if getattr(args, "force_full_install", False):
                print(f"  --force-full-install: would reinstall every package in {req}")
            else:
                missing = _missing_requirements(req)
                if missing:
                    print(f"  Auto-detect: {len(missing)} of "
                          f"{len(_read_requirements_lines(req))} packages are missing.")
                    print(f"  would install: {Path(sys.executable).name or 'python'} "
                          f"-m pip install --upgrade-strategy only-if-needed")
                    for line in missing:
                        pretty = line if len(line) <= 64 else line[:61] + "..."
                        print(f"    - {pretty}")
                else:
                    print("  Auto-detect: every required package is already installed.")
        else:
            print(f"  would run: {Path(sys.executable).name or 'python'} -m pip install "
                  f"-r {args.requirements}")
        if browser_args:
            print(f"  would run: {browser_retry_command(choice)}")
        else:
            print("  no browser download")
        return 0

    # Mark LIV 54: detect installed packages first, install only missing ones.
    # This is the default path; pass --force-full-install to skip the check
    # and reinstall every requirement (the old v53 behaviour).
    if getattr(args, "force_full_install", False):
        print("\n  --force-full-install set: reinstalling every package in " +
              f"{args.requirements}")
        _run("Installing Python dependencies (full reinstall)…",
             [sys.executable, "-m", "pip", "install", "--no-input", "--upgrade",
              "-r", args.requirements])
        if not _verify_requirements(Path(args.requirements)):
            return 1
    else:
        if not _auto_install(args.requirements):
            print("\nNOT READY — Python package installation/verification failed.", flush=True)
            return 1

    if browser_args:
        if not _install_browsers(browser_args):
            print("\nNOT READY — browser installation/verification failed.", flush=True)
            return 1
    else:
        print("\nSkipping Playwright browsers (explicit minimal install).")

    # Recheck after all installation steps; do not claim readiness on failure.
    if not _verify_requirements(Path(args.requirements)):
        return 1

    _check_assets()

    # ── OS-specific post-install notes ────────────────────────────────────────
    if OS == "Windows":
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
    elif OS == "Linux":
        print(
            "\nℹ️  Linux note — a few voice-controlled OS actions shell out to "
            "native tools. Install the ones you'll use via your package manager:\n"
            "    • volume      → pulseaudio-utils   (pactl)\n"
            "    • brightness  → brightnessctl\n"
            "    • reminders   → systemd (systemd-run) or 'at'\n"
            "    • open URLs   → xdg-utils          (xdg-open)"
        )
    elif OS == "Darwin":
        print(
            "\nℹ️  macOS note — volume, brightness and reminders use the built-in "
            "'osascript' / LaunchAgents, so no extra tools are required."
        )

    print("\nREADY — Setup complete!" +
          (" (Python packages only; browsers explicitly skipped.)" if not browser_args else
           " Python packages and selected browsers verified."), flush=True)
    print("   1) Launch it:  python main.py")
    print("   2) Paste your free Gemini API key when the setup screen appears.")
    print("   3) (Optional) Enable 'Hey Jarvis' from ⚙ → WAKE WORD.")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    try:
        raise SystemExit(main())
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"\nNOT READY — {exc}", flush=True)
        raise SystemExit(1)
    except Exception as exc:
        # Any other crash must still look like a verdict: keep the traceback
        # for debugging, but never leave the user staring at one without a
        # NOT READY and a non-zero exit code.
        traceback.print_exc()
        print(f"\nNOT READY — setup crashed before verification finished: "
              f"{exc.__class__.__name__}: {exc}", flush=True)
        raise SystemExit(1)

"""
MARK LIV — one-time setup.

Installs the Python dependencies for THIS operating system only: the OS-specific
packages in requirements.txt carry `sys_platform` markers, so a macOS or Linux
user never pulls Windows-only libraries (and vice-versa).

It then offers the Playwright browsers needed for web automation. Those are the
single biggest thing setup ever downloads — each browser engine is a full build
of a real browser, ~225 MB for Chromium and ~86 MB for Firefox — so they are
asked for instead of pulled silently:

    py setup.py                    ask interactively (recommended: Chromium only)
    py setup.py --yes              non-interactive, take the recommended default
    py setup.py --minimal          Python packages only, no browser binaries
    py setup.py --browsers firefox pick one of: none, chromium, firefox,
                                   chromium-firefox

Nothing here is mandatory for the app to start: without a browser engine
everything works except browser automation, and it can be added later with
`python -m playwright install chromium`.

Two things it deliberately does NOT install:
  * the optional local wake word ("Hey Jarvis") — one-click, opt-in, from
    ⚙ → WAKE WORD inside the app;
  * anything for the avatar — the holographic head renders in software on the
    PyQt6 and numpy already listed here. No GPU, no OpenGL, no extra packages.
"""
import argparse
import importlib.util
import os
import platform
import subprocess
import sys
from pathlib import Path

OS = platform.system()  # "Windows" | "Darwin" | "Linux"
HERE = Path(__file__).resolve().parent

MIN_PY = (3, 11)        # hard floor: below this the syntax used here won't parse
MAX_PY = (3, 13)        # highest version this is actually tested on


# ── Playwright browser selection ──────────────────────────────────────────────
# A browser engine is a big download and most people only ever need one, so the
# choice is explicit and explained rather than a silent ~300 MB surprise.
BROWSER_CHOICES = ("none", "chromium", "firefox", "chromium-firefox")
DEFAULT_BROWSERS = "chromium"   # covers Chrome, Edge, Brave, Vivaldi, Opera
ENV_BROWSERS = "MARK_LIV_BROWSERS"  # non-interactive override for scripts/CI

# What each choice hands to `python -m playwright install`. Empty means skip.
PLAYWRIGHT_ARGS = {
    "none": [],
    "chromium": ["chromium"],
    "firefox": ["firefox"],
    "chromium-firefox": ["chromium", "firefox"],
}

# Approximate, current-OS download size — enough to make the question answerable.
# Every entry also pulls a small shared ffmpeg build (a few MB).
SHORT_SIZES = {
    "none": "0 MB",
    "chromium": "~225 MB",
    "firefox": "~86 MB",
    "chromium-firefox": "~311 MB",
}

# Long form, for the one place the size really needs justifying: right before
# several hundred megabytes start arriving.
SIZE_NOTES = {
    "none": "nothing is downloaded",
    "chromium": "~225 MB (Chromium ~136 MB + headless shell ~88 MB, plus a small ffmpeg)",
    "firefox": "~86 MB (Firefox, plus a small ffmpeg)",
    "chromium-firefox": "~311 MB (Chromium + Firefox, plus a small ffmpeg)",
}

# Menu order shown in an interactive terminal: most useful first.
_MENU = (
    ("1", "chromium", "Chromium only",
     "covers Chrome, Edge, Brave, Vivaldi, Opera — what browser automation needs"),
    ("2", "chromium-firefox", "Chromium + Firefox",
     "adds the real Gecko engine for Firefox-specific sites"),
    ("3", "firefox", "Firefox only",
     "only if you exclusively automate Firefox"),
    ("4", "none", "No browsers (minimal)",
     "browser automation stays unavailable until you install one later"),
)

_ALIASES = {
    "no": "none", "skip": "none", "minimal": "none", "0": "none",
    "chrome": "chromium", "cr": "chromium",
    "ff": "firefox",
    "both": "chromium-firefox", "all": "chromium-firefox",
    "firefox-chromium": "chromium-firefox",
}


def _run(label: str, args: list[str]) -> None:
    print(f"\n▶ {label}")
    subprocess.run(args, check=True)


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


def _stdin_is_interactive() -> bool:
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:      # stdin closed, redirected, or a non-standard object
        return False


def _print_browser_menu() -> None:
    print("\n── Playwright browsers " + "─" * 50)
    print("Browser automation needs a real browser engine downloaded from the")
    print("Playwright CDN. These are large, so pick what you actually want:")
    for key, choice, label, why in _MENU:
        marker = " (recommended)" if choice == DEFAULT_BROWSERS else ""
        print(f"   {key}) {label:<22} {SHORT_SIZES[choice]:>8}")
        print(f"      {why}{marker}")
    print("   Approximate download size for this OS; each also pulls a small ffmpeg.")
    print("   (Safari/WebKit is separate: python -m playwright install webkit)")


def prompt_browsers() -> str:
    """Ask in an interactive terminal. Never blocks a non-interactive run."""
    _print_browser_menu()
    by_key = {key: choice for key, choice, _label, _why in _MENU}
    try:
        for _attempt in range(3):
            raw = input("\n  Install which browsers? [1/2/3/4] (default: 1) ").strip()
            if not raw:
                return DEFAULT_BROWSERS
            if raw in by_key:
                return by_key[raw]
            choice = normalize_browsers(raw)
            if choice:
                return choice
            print(f"  ! '{raw}' is not one of 1, 2, 3, 4 — or type a name like 'chromium'.")
        print(f"  ! Too many invalid answers — using the recommended default "
              f"({DEFAULT_BROWSERS}).")
        return DEFAULT_BROWSERS
    except EOFError:
        # stdin ended under us (piped input, closed console): do not hang, and
        # do not abort an otherwise working install over a cosmetic question.
        print(f"\n  No input available — using the recommended default "
              f"({DEFAULT_BROWSERS}).")
        return DEFAULT_BROWSERS
    except KeyboardInterrupt:
        print("\n\nAborted — nothing was installed.")
        print(f"    Continue without browsers:  py setup.py --minimal")
        raise SystemExit(130)


def resolve_browsers(args, interactive: bool | None = None, environ=None) -> str:
    """Turn CLI flags + environment + (maybe) a prompt into one concrete choice.

    Precedence: explicit "skip" flags > --browsers > $MARK_LIV_BROWSERS >
    interactive prompt > recommended default. A non-interactive run therefore
    never waits for input, and always ends up with a defined answer.
    """
    environ = os.environ if environ is None else environ
    if interactive is None:
        interactive = _stdin_is_interactive()

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

    if interactive and not getattr(args, "yes", False):
        return prompt_browsers()

    # Non-interactive (piped output, CI, scheduled task): no question is
    # possible, so take the documented recommended default instead of hanging.
    if not getattr(args, "yes", False):
        print(f"  No terminal attached — using the recommended default "
              f"({DEFAULT_BROWSERS}, {SHORT_SIZES[DEFAULT_BROWSERS]}). "
              f"Use --minimal to skip it.")
    return DEFAULT_BROWSERS


def _describe_plan(choice: str) -> str:
    args = playwright_install_args(choice)
    if not args:
        return "no browser binaries (minimal — browser automation can be added later)"
    return " + ".join(args) + f"  ({SHORT_SIZES[choice]})"


# ── Setup ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description="MARK LIV one-time setup: Python dependencies plus the "
                    "Playwright browsers you choose.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "browser selection:\n"
            "  none               install no browser binaries\n"
            "  chromium           Chromium only   (~225 MB) — recommended\n"
            "  firefox            Firefox only    (~86 MB)\n"
            "  chromium-firefox   both engines    (~311 MB)\n"
            "\n"
            "examples:\n"
            "  py setup.py                        ask which browsers to fetch\n"
            "  py setup.py --yes                  no questions, Chromium only\n"
            "  py setup.py --minimal              packages only, no browsers\n"
            "  py setup.py --browsers firefox     Firefox only, no questions\n"
            "  py setup.py --dry-run              print the plan, change nothing\n"
            "\n"
            "Non-interactive runs never wait for input: they take the\n"
            f"recommended default ({DEFAULT_BROWSERS}) unless told otherwise.\n"
            f"Environment override for scripts: {ENV_BROWSERS}=none|chromium|"
            "firefox|chromium-firefox\n"
        ),
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
                        help=f"non-interactive: take the recommended default "
                             f"({DEFAULT_BROWSERS})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be installed, then exit")
    parser.add_argument("--requirements", default=str(HERE / "requirements.txt"),
                        help="requirements file to install (default: ./requirements.txt)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"⚙  MARK LIV setup — detected OS: {OS or 'unknown'}, "
          f"Python {sys.version_info[0]}.{sys.version_info[1]}")
    _check_python()

    # Ask before pip runs: the dependency install can take minutes, and nobody
    # should come back to a prompt that has been waiting the whole time.
    choice = resolve_browsers(args)
    browser_args = playwright_install_args(choice)
    print(f"\nPlan: Python packages from {args.requirements}")
    print(f"      Playwright browsers: {_describe_plan(choice)}")

    if args.dry_run:
        print("\nDry run — nothing was installed.")
        print(f"  would run: {Path(sys.executable).name or 'python'} -m pip install "
              f"-r {args.requirements}")
        if browser_args:
            print(f"  would run: {browser_retry_command(choice)}")
        else:
            print("  no browser download")
        return 0

    # requirements.txt filters OS-specific extras by itself via pip markers.
    _run("Installing Python dependencies (OS-specific extras auto-filtered)…",
         [sys.executable, "-m", "pip", "install", "-r", args.requirements])

    if browser_args:
        # Chromium covers Chrome/Edge/Opera/Brave/Vivaldi; Firefox for Firefox.
        # (Safari automation additionally needs: python -m playwright install webkit)
        # Not fatal: these are a few hundred megabytes from a CDN that a corporate
        # network or a flaky connection can refuse, and everything except browser
        # automation works without them. Failing the whole install there would send
        # a user away from a working app.
        try:
            _run(f"Installing Playwright browsers ({' + '.join(browser_args)}) — "
                 f"{SIZE_NOTES[choice]}; this is the big download…",
                 [sys.executable, "-m", "playwright", "install", *browser_args])
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"\n⚠️  Playwright browsers were not installed ({e}).")
            print("    Everything except browser automation works. Retry later with:")
            print(f"    {browser_retry_command(choice)}")
            if importlib.util.find_spec("playwright") is None:
                print("    (playwright itself is not importable — if pip could not "
                      "install it, run the line above after fixing pip.)")
    else:
        print("\n⏭  Skipping Playwright browsers (minimal install).")
        print("    Everything except browser automation works. Add them later with:")
        print(f"    {browser_retry_command('chromium')}")

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
            "'osascript' / LaunchAgents, so no extra tools are required.\n"
            "    For Safari automation only: python -m playwright install webkit"
        )

    print("\n✅ Setup complete!")
    print("   1) Launch it:  python main.py")
    print("   2) Paste your free Gemini API key when the setup screen appears.")
    print("   3) (Optional) Enable 'Hey Jarvis' from ⚙ → WAKE WORD.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

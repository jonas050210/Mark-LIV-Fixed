"""Setup CLI checks — the browser-download UX of setup.py.

Everything here is pure argument/selection logic: no pip call and, above all,
no Playwright browser download. The point is that `py setup.py` must explain
what it is about to download, offer a minimal path, and never ask for input.
"""
import contextlib
import io
import os
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import setup as S   # importing setup has no side effects: main() is guarded


@patch.dict(os.environ, {S.ENV_BROWSERS: ""})
def main():
    results = []


    def check(name, got, want):
        ok = got == want
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
        results.append(ok)


    P = S.build_parser()

    # ── 1. flags exist and parse ─────────────────────────────────────────────────
    args = P.parse_args([])
    check("default: no --browsers", args.browsers, None)
    check("default: --minimal off", args.minimal, False)
    check("--minimal parses", P.parse_args(["--minimal"]).minimal, True)
    check("--skip-browsers parses", P.parse_args(["--skip-browsers"]).skip_browsers, True)
    check("--browsers parses", P.parse_args(["--browsers", "firefox"]).browsers, "firefox")
    check("--yes parses", P.parse_args(["--yes"]).yes, True)
    check("-y parses", P.parse_args(["-y"]).yes, True)
    check("--dry-run parses", P.parse_args(["--dry-run"]).dry_run, True)
    try:
        P.parse_args(["--browsers", "nonsense"])
        check("unknown browser is rejected by argparse", "accepted", "error")
    except SystemExit:
        check("unknown browser is rejected by argparse", "error", "error")

    # ── 2. input normalisation (free text, env vars, typos) ──────────────────────
    for raw, want in [("chromium", "chromium"), ("Chrome", "chromium"), ("CHROMIUM", "chromium"),
                      ("firefox", "firefox"), ("ff", "firefox"),
                      ("chromium-firefox", "chromium-firefox"), ("chromium_firefox", "chromium-firefox"),
                      ("chromium, firefox", "chromium-firefox"), ("both", "chromium-firefox"),
                      ("none", "none"), ("minimal", "none"), ("skip", "none"),
                      ("webkit", "webkit"), ("all", "all"), ("nonsense", None), (None, None)]:
        check(f"normalize_browsers({raw!r})", S.normalize_browsers(raw), want)

    # ── 3. what would actually be handed to playwright ───────────────────────────
    check("install args: none", S.playwright_install_args("none"), [])
    check("install args: chromium", S.playwright_install_args("chromium"), ["chromium"])
    check("install args: firefox", S.playwright_install_args("firefox"), ["firefox"])
    check("install args: both", S.playwright_install_args("chromium-firefox"), ["chromium", "firefox"])
    check("install args: unknown -> skip", S.playwright_install_args("nonsense"), [])
    check("retry command mentions both engines",
          S.browser_retry_command("chromium-firefox").endswith("playwright install chromium firefox"), True)
    check("retry command empty for none", S.browser_retry_command("none"), "")

    # ── 4. precedence: skip flags > --browsers > env > default ──────────
    NO_ENV = {}
    check("resolve: --minimal", S.resolve_browsers(P.parse_args(["--minimal"]), False, NO_ENV), "none")
    check("resolve: --skip-browsers", S.resolve_browsers(P.parse_args(["--skip-browsers"]), False, NO_ENV), "none")
    for choice in S.BROWSER_CHOICES:
        check(f"resolve: --browsers {choice}",
              S.resolve_browsers(P.parse_args(["--browsers", choice]), False, NO_ENV), choice)
    check("resolve: --yes -> recommended default",
          S.resolve_browsers(P.parse_args(["--yes"]), False, NO_ENV), S.DEFAULT_BROWSERS)
    check("resolve: --yes never prompts (interactive stdin)",
          S.resolve_browsers(P.parse_args(["--yes"]), True, NO_ENV), S.DEFAULT_BROWSERS)
    check("resolve: no terminal -> default, no hang",
          S.resolve_browsers(P.parse_args([]), False, NO_ENV), S.DEFAULT_BROWSERS)
    check("resolve: env var used",
          S.resolve_browsers(P.parse_args([]), False, {S.ENV_BROWSERS: "firefox"}), "firefox")
    check("resolve: env var normalised",
          S.resolve_browsers(P.parse_args([]), False, {S.ENV_BROWSERS: "chromium, firefox"}),
          "chromium-firefox")
    check("resolve: env var empty -> ignored",
          S.resolve_browsers(P.parse_args([]), False, {S.ENV_BROWSERS: "  "}), S.DEFAULT_BROWSERS)
    check("resolve: nonsense env -> default, not a crash",
          S.resolve_browsers(P.parse_args([]), False, {S.ENV_BROWSERS: "nonsense"}), S.DEFAULT_BROWSERS)
    check("resolve: --browsers beats env",
          S.resolve_browsers(P.parse_args(["--browsers", "none"]), False, {S.ENV_BROWSERS: "firefox"}), "none")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = S.resolve_browsers(P.parse_args(["--minimal", "--browsers", "chromium"]), False, NO_ENV)
    check("resolve: --minimal beats --browsers", got, "none")
    check("resolve: conflicting flags are explained", "--minimal/--skip-browsers wins" in buf.getvalue(), True)

    # ── 5. setup must never prompt, even with an interactive Windows console ───
    with patch("builtins.input", side_effect=AssertionError("setup read stdin")):
        for interactive in (True, False):
            check(f"default never prompts (interactive={interactive})",
                  S.resolve_browsers(P.parse_args([]), interactive, {}), "all")
    check("default includes all engines", S.playwright_install_args(S.DEFAULT_BROWSERS),
          ["chromium", "firefox", "webkit"])
    check("WebKit is selectable", S.playwright_install_args("webkit"), ["webkit"])

    # ── 6. dry run touches nothing ───────────────────────────────────────────────
    REAL_RUN = S._run


    def forbidden(*a, **k):
        raise AssertionError("dry run must not install anything")


    S._run = forbidden
    try:
        req = str(Path(REPO) / "requirements.txt")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = S.main(["--dry-run", "--minimal", "--requirements", req])
        out = buf.getvalue()
        check("dry run: exit code 0", rc, 0)
        check("dry run: prints the plan", "no browser binaries" in out, True)
        check("dry run: says nothing was installed", "nothing was installed" in out, True)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = S.main(["--dry-run", "--browsers", "chromium-firefox", "--requirements", req])
        check("dry run: exit code 0 (browsers)", rc, 0)
        check("dry run: shows the playwright command",
              "playwright install chromium firefox" in buf.getvalue(), True)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = S.main(["--dry-run", "--yes", "--requirements", req])
        check("dry run: --yes exit code 0", rc, 0)
        check("dry run: --yes plans chromium", "playwright install chromium" in buf.getvalue(), True)
    finally:
        S._run = REAL_RUN

    # ── 7. first-launch installer: same choice, honest failure reporting ─────────
    import core.installer as I

    check("installer: default is chromium", I._browser_choice(), "chromium")
    for _raw, want in [("none", "none"), ("skip", "none"), ("minimal", "none"),
                       ("firefox", "firefox"), ("chromium-firefox", "chromium-firefox"),
                       ("chromium_firefox", "chromium-firefox"), ("  ", "chromium"),
                       ("webkit", "chromium")]:
        _old = os.environ.get(I.BROWSER_ENV)
        os.environ[I.BROWSER_ENV] = _raw
        try:
            check(f"installer: {I.BROWSER_ENV}={_raw!r}", I._browser_choice(), want)
        finally:
            if _old is None:
                os.environ.pop(I.BROWSER_ENV, None)
            else:
                os.environ[I.BROWSER_ENV] = _old

    _calls = {"run": []}


    class _R:
        def __init__(self, rc, err=b""):
            self.returncode, self.stderr = rc, err


    def _run_case(env_value=None, pip_ok=True, rc=0):
        """Drive install_for_config with pip and playwright stubbed out."""
        _calls["run"].clear()
        real_avail, real_pip, real_run = I._available, I._pip, I.subprocess.run
        real_cache = I.browser_cache_state
        I.browser_cache_state = lambda names: (True, "stubbed complete cache")
        I._available = lambda m: False          # pretend playwright is missing
        I._pip = lambda pkg, log=None: pip_ok
        I.subprocess.run = lambda args, **kw: (_calls["run"].append(list(args)),
                                               _R(rc, b"boom\n"))[1]
        _old = os.environ.get(I.BROWSER_ENV)
        if env_value is None:
            os.environ.pop(I.BROWSER_ENV, None)
        else:
            os.environ[I.BROWSER_ENV] = env_value
        lines = []
        try:
            I.install_for_config({"stt_engine": "whisper", "tts_engine": "edgetts"},
                                 log=lines.append)
        finally:
            if _old is None:
                os.environ.pop(I.BROWSER_ENV, None)
            else:
                os.environ[I.BROWSER_ENV] = _old
            I._available, I._pip, I.subprocess.run = real_avail, real_pip, real_run
            I.browser_cache_state = real_cache
        return list(_calls["run"]), "\n".join(lines)


    _ran, _log = _run_case(None)
    check("installer: fetches chromium by default",
          any(a[-1] == "chromium" for a in _ran), True)
    check("installer: names the download size", "225 MB" in _log, True)
    check("installer: reports success", "Playwright browser ready" in _log, True)

    _ran, _log = _run_case("none")
    check("installer: none downloads nothing",
          [a for a in _ran if "playwright" in a and "install" in a], [])
    check("installer: skip is explained", "Skipping Playwright browsers" in _log, True)
    check("installer: retry hint given", "playwright install chromium" in _log, True)

    _ran, _log = _run_case("chromium-firefox")
    check("installer: both engines honoured",
          any(a[-2:] == ["chromium", "firefox"] for a in _ran), True)

    _ran, _log = _run_case(None, pip_ok=False)
    check("installer: pip failure stops the download",
          [a for a in _ran if "playwright" in a and "install" in a], [])
    check("installer: pip failure is reported", "failed to install" in _log, True)

    _ran, _log = _run_case(None, rc=1)
    check("installer: download failure is reported", "download failed" in _log, True)
    check("installer: no false success after failure",
          "Playwright browser ready" not in _log, True)
    check("installer: failure prints the retry command",
          "playwright install chromium" in _log, True)
    check("installer: failure shows the reason", "boom" in _log, True)

    failed = results.count(False)
    print(f"\nchecks: {len(results)}  passed: {results.count(True)}  failed: {failed}")
    return 1 if failed else 0


def test_setup_cli_checks():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Tests for the Mark LIV 54 setup.py auto-install path.

These cover the detection-only pieces:
  * _read_requirements_lines: parse comments, markers, version pins, names.
  * _is_installed: existing-package detection (handles common aliases).
  * _missing_requirements: full file scan with marker filters honoured.
  * build_parser: the new --auto and --force-full-install flags.

The test never actually installs anything \u2014 the auto-detect logic is
pure Python; installation checks use distribution metadata without imports.
"""
import contextlib
import importlib.util
import io
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent
if REPO.name == "tests":
    REPO = REPO.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import setup as S   # imports setup.py; main() is guarded


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
    return ok


def test_parser_new_flags():
    P = S.build_parser()
    a = P.parse_args([])
    assert getattr(a, "auto", False) is False
    assert getattr(a, "force_full_install", False) is False
    a2 = P.parse_args(["--auto"])
    assert a2.auto is True
    a3 = P.parse_args(["--force-full-install"])
    assert a3.force_full_install is True
    print("[PASS] parser has --auto and --force-full-install")
    return True


def test_read_requirements_lines():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write("# top-of-file comment\n")
        tf.write("\n")
        tf.write("PyQt6>=6.6,<7\n")
        tf.write("opencv-python>=4.8,<5  # inline comment\n")
        tf.write("uiautomation>=2.0,<3; sys_platform == \"win32\"\n")
        tf.write("-e git+https://example.com/foo#egg=bar\n")
        path = Path(tf.name)
    try:
        lines = S._read_requirements_lines(path)
        names = [n for _, n in lines]
        # Comments (including inline comments) are stripped before passing to pip.
        assert "PyQt6" in names
        assert "opencv-python" in names
        assert all("inline comment" not in line for line, _ in lines)
        # The marker is preserved in the raw line (we keep the line so pip can
        # filter it on its own). The name is the bare PyPI token.
        raw = [ln for ln, _ in lines if "uiautomation" in ln]
        assert raw, "uiautomation line was dropped"
        assert 'sys_platform == "win32"' in raw[0], f"marker stripped: {raw[0]!r}"
        # Editable URLs are passed through; their name is empty and they are
        # left to pip.
        edit = [ln for ln, _ in lines if ln.startswith("-")]
        assert edit, "editable URL dropped"
        print("[PASS] _read_requirements_lines honours comments, markers, editables")
        return True
    finally:
        path.unlink()


def test_is_installed_handles_aliases():
    # Do not depend on which optional packages happen to be installed in CI.
    names = ("Pillow", "google-genai", "beautifulsoup4", "pywin32", "data-only")
    with patch.object(S.importlib_metadata, "distribution",
                      return_value=SimpleNamespace(version="2.0")) as lookup, \
         patch.object(importlib.util, "find_spec", side_effect=AssertionError("must not guess imports")):
        for name in names:
            assert S._is_installed(name)
            lookup.assert_called_with(name)
    with patch.object(S.importlib_metadata, "distribution",
                      side_effect=S.importlib_metadata.PackageNotFoundError), \
         patch.object(importlib.util, "find_spec", return_value=object()):
        assert not S._is_installed("missing-distribution"), "an unrelated module is not an install"
    print("[PASS] distribution/import name mismatches and missing distributions")
    return True


def test_is_installed_handles_distribution_metadata():
    # Exercise the real metadata API, including name normalisation, with a
    # temporary data-only distribution (no matching Python module at all).
    with tempfile.TemporaryDirectory() as td:
        info = Path(td) / "cleanup_data_only-2.5.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: cleanup-data-only\nVersion: 2.5\n", encoding="utf-8")
        with patch.object(sys, "path", [td, *sys.path]):
            assert S._is_installed("Cleanup.Data_Only>=2,<3")
            assert S._is_installed("cleanup-data-only[extra]>=2")
            assert not S._is_installed("cleanup-data-only>=3")
            assert not S._is_installed("cleanup-data-only<2")
            assert not S._is_installed("cleanup-data-only!=2.5")
    with patch.object(S.importlib_metadata, "distribution",
                      return_value=SimpleNamespace(version=None)):
        assert not S._is_installed("broken-metadata")
    print("[PASS] real metadata normalisation, data-only packages and version bounds")
    return True


def test_verification_is_strict_and_platform_aware():
    with tempfile.TemporaryDirectory() as td:
        req = Path(td) / "requirements.txt"
        req.write_text('different-name>=2,<3  # comment\n'
                       'not-for-this-platform; python_version < "0"\n', encoding="utf-8")
        with patch.object(S.importlib_metadata, "distribution",
                          return_value=SimpleNamespace(version="2.5")) as lookup:
            assert S._missing_requirements(req) == []
            with contextlib.redirect_stdout(io.StringIO()):
                assert S._verify_requirements(req)
            assert all(call.args == ("different-name",) for call in lookup.call_args_list)
        with patch.object(S.importlib_metadata, "distribution",
                          return_value=SimpleNamespace(version="1.0")):
            assert S._missing_requirements(req) == ["different-name>=2,<3"]
            with contextlib.redirect_stdout(io.StringIO()):
                assert not S._verify_requirements(req)
        with patch.object(S, "_missing_requirements", return_value=[]), \
             patch.object(S, "_install_requirements", return_value=True), \
             patch.object(S, "_verify_requirements", return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            assert not S._auto_install(str(req))
        with patch.object(S, "_auto_install", return_value=False), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            assert S.main(["--minimal"]) == 1
            assert "Setup complete" not in out.getvalue()
        with patch.object(S, "_run") as run, contextlib.redirect_stdout(io.StringIO()):
            assert S._install_requirements(["different-name>=2,<3"], req)
            assert run.call_args.args[1][-1] == "different-name>=2,<3"
        with contextlib.redirect_stdout(io.StringIO()):
            assert not S._verify_requirements(Path(td) / "absent.txt")
    print("[PASS] strict verification, platform markers, missing-only pip and failure propagation")
    return True


def test_missing_requirements_in_repo():
    """Scan the real requirements.txt and confirm the result is sensible:
    * every line is either an empty/comment skip, a known installed package,
      or an unknown (missing) one \u2014 never an error
    * the list of missing packages must be a subset of the parsed lines."""
    req = REPO / "requirements.txt"
    if not req.exists():
        print("[SKIP] requirements.txt not found in repo root")
        return True
    missing = S._missing_requirements(req)
    parsed = S._read_requirements_lines(req)
    parsed_names = {n for _, n in parsed if n}
    missing_names = set()
    for line in missing:
        # take the first token (no markers / extras)
        head = line.split(";", 1)[0].strip().split("[", 1)[0]
        for sep in (">=", "<=", "==", "!=", "~=", ">", "<"):
            if sep in head:
                head = head.split(sep, 1)[0].strip()
        missing_names.add(head)
    # every missing name should be one we parsed from the file
    extra = missing_names - parsed_names
    assert not extra, f"missing list contains un-parsed names: {extra!r}"
    # Re-running with no packages installed (via a fake dir) would return all
    # names \u2014 but we don't uninstall anything here. Just confirm the function
    # is idempotent within one process.
    missing2 = S._missing_requirements(req)
    assert sorted(missing) == sorted(missing2), "non-idempotent scan"
    print(f"[PASS] real requirements.txt scanned ({len(missing)} missing)")
    return True


def test_dry_run_with_autodetect():
    """The dry-run path must include the auto-detect plan, not just the old
    full-reinstall line. We suppress stdout so the test output stays clean."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), \
         patch("builtins.input", side_effect=AssertionError("setup must not read stdin")):
        try:
            rc = S.main(["--dry-run", "--yes"])
        except SystemExit as e:
            rc = e.code
    assert rc == 0
    out = buf.getvalue()
    # the auto-detect plan must be present
    assert "Auto-detect" in out or "would install" in out, \
        f"dry-run output missing auto-detect summary:\n{out}"
    print("[PASS] --dry-run reports the auto-detect plan")
    return True


def test_dry_run_with_force_full_install():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), \
         patch("builtins.input", side_effect=AssertionError("setup must not read stdin")):
        try:
            rc = S.main(["--dry-run", "--yes", "--force-full-install"])
        except SystemExit as e:
            rc = e.code
    assert rc == 0
    out = buf.getvalue()
    assert "--force-full-install" in out, \
        f"dry-run output missing force-full-install label:\n{out}"
    print("[PASS] --dry-run --force-full-install is honoured")
    return True


def main():
    results = []
    results.append(test_parser_new_flags())
    results.append(test_read_requirements_lines())
    results.append(test_is_installed_handles_aliases())
    results.append(test_is_installed_handles_distribution_metadata())
    results.append(test_verification_is_strict_and_platform_aware())
    results.append(test_missing_requirements_in_repo())
    results.append(test_dry_run_with_autodetect())
    results.append(test_dry_run_with_force_full_install())
    failed = sum(1 for r in results if not r)
    raise SystemExit(0 if not failed else 1)


if __name__ == "__main__":
    main()

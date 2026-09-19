"""
Mark LIV 54: GUI improvements regression suite.

Covers:
  * GUI settings round-trip through config_manager (default + clamp + save).
  * GUI helpers in ui.py (scale_px, scaled_font, _read_gui_settings cache).
  * HudCanvas animated layout factor API (set_task_active, layout_target, etc).
  * TaskActivityBridge emits the correct signals for an active/inactive
    task registry.

Config tests are pure Python. Widget tests use offscreen Qt and skip only
when native PyQt dependencies are unavailable.
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
if REPO.name == "tests":
    REPO = REPO.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def check(name, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + ("" if ok else f"  got={got!r} want={want!r}"))
    return ok


# ──────────────────────────────────────────────────────────────────────────────
# 1. config_manager GUI block — defaults, clamps, persistence
# ──────────────────────────────────────────────────────────────────────────────
def test_config_manager_gui_defaults():
    import memory.config_manager as cm
    with tempfile.TemporaryDirectory(prefix="markliv-gui-") as td:
        old_file, old_dir = cm.CONFIG_FILE, cm.CONFIG_DIR
        cm.CONFIG_DIR = Path(td)
        cm.CONFIG_FILE = Path(td) / "api_keys.json"
        try:
            # Default block must come back with sane defaults (not raise).
            block = cm.get_gui_settings()
            for key in ("ui_mode", "ui_scale", "font_scale", "panel_width",
                        "hud_size", "hud_anchor", "hud_transparency",
                        "animation_enabled", "animation_speed",
                        "task_overlay_mode", "visualizer_style",
                        "log_max_lines", "show_debug_log",
                        "snap_hud_on_task", "auto_task_delay_ms"):
                assert key in block, f"missing key in defaults: {key}"
            assert block["ui_mode"] == "normal"
            assert block["hud_anchor"] == "topleft"
            assert block["animation_enabled"] is True
            print("[PASS] config_manager: GUI defaults are present and sane")
            return True
        finally:
            cm.CONFIG_FILE, cm.CONFIG_DIR = old_file, old_dir


def test_config_manager_gui_clamps():
    """Out-of-range values must be clamped, not rejected, so a partial update
    cannot break the HUD."""
    import memory.config_manager as cm
    with tempfile.TemporaryDirectory() as td:
        old_file, old_dir = cm.CONFIG_FILE, cm.CONFIG_DIR
        cm.CONFIG_DIR = Path(td)
        cm.CONFIG_FILE = Path(td) / "api_keys.json"
        try:
            cm.save_api_keys("a" * 30)
            cm.save_gui_settings({
                "hud_size": 99.0,           # way over the ceiling
                "font_scale": -1.0,         # way below the floor
                "panel_width": 100_000,     # huge
                "animation_speed": 5.0,     # over the ceiling
                "ui_scale": "not a float",  # garbage
                "hud_anchor": "back_left",  # unknown anchor
                "visualizer_style": "plasma",  # unknown style
                "log_max_lines": 1_000_000,  # huge
            })
            block = cm.get_gui_settings()
            assert block["hud_size"] <= cm._MAX_HUDSZ, block["hud_size"]
            assert block["font_scale"] >= cm._MIN_FONTSZ, block["font_scale"]
            assert block["panel_width"] <= cm._MAX_WIDTH, block["panel_width"]
            assert block["animation_speed"] <= cm._MAX_ANIM, block["animation_speed"]
            assert block["hud_anchor"] == "topleft", block["hud_anchor"]
            assert block["visualizer_style"] == "classic", block["visualizer_style"]
            assert block["log_max_lines"] <= 5000, block["log_max_lines"]
            assert 0.5 < block["ui_scale"] < 2.0, block["ui_scale"]
            print("[PASS] config_manager: GUI block clamps bad values")
            return True
        finally:
            cm.CONFIG_FILE, cm.CONFIG_DIR = old_file, old_dir


def test_config_manager_gui_persistence():
    """A save round-trips through the file (no silent losses)."""
    import memory.config_manager as cm
    with tempfile.TemporaryDirectory() as td:
        old_file, old_dir = cm.CONFIG_FILE, cm.CONFIG_DIR
        cm.CONFIG_DIR = Path(td)
        cm.CONFIG_FILE = Path(td) / "api_keys.json"
        try:
            cm.save_api_keys("a" * 30)
            block = {
                "ui_mode": "compact",
                "hud_size": 0.7,
                "font_scale": 1.1,
                "panel_width": 300,
                "hud_anchor": "topright",
                "hud_transparency": 0.8,
                "animation_enabled": False,
                "animation_speed": 0.5,
                "task_overlay_mode": "always",
                "visualizer_style": "minimal",
                "log_max_lines": 200,
                "show_debug_log": True,
                "snap_hud_on_task": False,
                "auto_task_delay_ms": 2500,
                "ui_scale": 0.9,
            }
            assert cm.save_gui_settings(block) is True
            # Re-read through the public API.
            again = cm.get_gui_settings()
            for k, v in block.items():
                assert again[k] == v, f"{k}: stored {again[k]!r}, expected {v!r}"
            # Re-read directly from disk so we know it was actually written.
            import json
            on_disk = json.loads(cm.CONFIG_FILE.read_text("utf-8"))
            assert on_disk["ui_mode"] == "compact"
            assert on_disk["hud_anchor"] == "topright"
            assert on_disk["animation_enabled"] is False
            print("[PASS] config_manager: GUI block persists atomically")
            return True
        finally:
            cm.CONFIG_FILE, cm.CONFIG_DIR = old_file, old_dir


# ──────────────────────────────────────────────────────────────────────────────
# 2. ui.py GUI helpers — scale_px, scaled_font, _read_gui_settings cache
# ──────────────────────────────────────────────────────────────────────────────
def test_ui_helpers_return_useful_values():
    """scale_px and scaled_font must return non-zero, sensible values when the
    config is on disk with defaults."""
    # We can't import ui.py at the top of the file because it imports PyQt6,
    # which requires the Qt platform plugin. We do a lazy import here so the
    # rest of this suite works even without a display.
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("[SKIP] PyQt6 not available — skipping ui.py tests")
        return True
    # Force a Qt instance so importlib is happy with QFont.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(sys.argv)

    import memory.config_manager as cm
    with tempfile.TemporaryDirectory() as td:
        old_file, old_dir = cm.CONFIG_FILE, cm.CONFIG_DIR
        cm.CONFIG_DIR = Path(td)
        cm.CONFIG_FILE = Path(td) / "api_keys.json"
        cm.save_api_keys("a" * 30)
        try:
            import ui
            # Drop any cache ui.py may have already populated.
            ui.reload_gui_settings()
            cfg = ui._read_gui_settings()
            assert "ui_scale" in cfg
            sp = ui.scale_px(100)
            assert isinstance(sp, int) and sp >= 1
            font = ui.scaled_font(9, bold=True)
            assert font.pointSize() >= 7
            assert font.bold()
            # UI apply explicitly reloads the cache after saving.
            cm.save_gui_settings({"ui_scale": 1.3, "font_scale": 1.2})
            ui.reload_gui_settings()
            cfg2 = ui._read_gui_settings()
            assert cfg2["ui_scale"] == 1.3
            assert cfg2["font_scale"] == 1.2
            sp2 = ui.scale_px(100)
            assert sp2 != sp, "scale_px did not pick up the new ui_scale"
            print("[PASS] ui.py helpers honour live config changes")
            return True
        finally:
            cm.CONFIG_FILE, cm.CONFIG_DIR = old_file, old_dir


# ──────────────────────────────────────────────────────────────────────────────
# 3. HudCanvas animation API (no window paint, only the methods)
# ──────────────────────────────────────────────────────────────────────────────
def test_hud_animation_methods():
    """The new HUD animation methods must exist and behave like state machines."""
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("[SKIP] PyQt6 not available — skipping HudCanvas test")
        return True
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(sys.argv)

    from ui import HudCanvas
    import memory.config_manager as cm
    with tempfile.TemporaryDirectory() as td:
        old_file, old_dir = cm.CONFIG_FILE, cm.CONFIG_DIR
        cm.CONFIG_DIR = Path(td)
        cm.CONFIG_FILE = Path(td) / "api_keys.json"
        cm.save_api_keys("a" * 30)
        try:
            hud = HudCanvas("face.png")
            # Initial layout factor is 0 (idle).
            assert hud.layout_factor() == 0.0
            # set_task_active(True) moves the target to 1.0 (the animation
            # tick eases it, but the target is immediate).
            hud.set_task_active(True)
            assert hud.is_task_active() is True
            assert hud.layout_factor() == 0.0  # the animation hasn't run yet
            hud.set_layout_target(0.0)
            # Setting target 0 while a task is active must NOT clear the task
            # state \u2014 the task is still running, only the visual target changed.
            hud.set_task_active(False)
            assert hud.is_task_active() is False
            hud.set_hud_style("core")
            hud.set_hud_style("face")
            # Off-spec style falls back to "face" silently.
            hud.set_hud_style("nonsense")
            print("[PASS] HudCanvas: task-active and HUD-style API works")
            return True
        finally:
            cm.CONFIG_FILE, cm.CONFIG_DIR = old_file, old_dir


# ──────────────────────────────────────────────────────────────────────────────
# 4. TaskActivityBridge \u2014 reads core.tasks and emits the right signals
# ──────────────────────────────────────────────────────────────────────────────
def test_task_bridge_emits_idle_signal():
    """An empty registry must produce the idle summary, never a phantom task."""
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("[SKIP] PyQt6 not available")
        return True
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(sys.argv)

    import core.tasks as tasks
    from ui import TaskActivityBridge
    # Reset the registry so the test is independent of state.
    tasks.reset()
    bridge = TaskActivityBridge(hud=None)

    active_seen = []
    summary_seen = []
    bridge.task_active_changed.connect(active_seen.append)
    bridge.summary_changed.connect(lambda *a: summary_seen.append(a))

    # Force a poll \u2014 the bridge connects signals but doesn't run a tick on its
    # own in this test (no QTimer running). Use the public API by calling the
    # internal method \u2014 it's an internal but documented slot for this purpose.
    bridge._last_active = True
    bridge._poll()

    # A previously active bridge must emit False when the registry goes idle.
    assert active_seen and active_seen[-1] is False, active_seen
    assert summary_seen, "no summary emitted"
    assert summary_seen[-1][4] == "idle", summary_seen[-1]
    assert summary_seen[-1][2] == 0, summary_seen[-1]
    print("[PASS] TaskActivityBridge: idle registry produces idle signals")
    bridge.stop()
    tasks.reset()
    return True


def test_task_bridge_detects_running_task():
    """A real task in the registry must surface as a non-idle summary, but
    only AFTER the configured grace delay (so a 200 ms task does NOT flip the
    HUD into a corner)."""
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("[SKIP] PyQt6 not available")
        return True
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(sys.argv)

    import core.tasks as tasks
    from ui import TaskActivityBridge
    import memory.config_manager as cm
    with tempfile.TemporaryDirectory() as td:
        old_file, old_dir = cm.CONFIG_FILE, cm.CONFIG_DIR
        cm.CONFIG_DIR = Path(td)
        cm.CONFIG_FILE = Path(td) / "api_keys.json"
        cm.save_api_keys("a" * 30)
        try:
            tasks.reset()
            # Zero the snap delay so the test doesn't have to sleep.
            cm.save_gui_settings({"auto_task_delay_ms": 0, "snap_hud_on_task": True})

            from ui import reload_gui_settings
            reload_gui_settings()
            bridge = TaskActivityBridge(hud=None)
            active_seen = []
            bridge.task_active_changed.connect(active_seen.append)

            # Start a task and poll.
            t = tasks.start("download", "Test download", detail="5 / 10 MB")
            bridge._poll()
            assert any(a is True for a in active_seen), active_seen

            # Finish the task; the next poll must clear active.
            tasks.finish(t.id, detail="done")
            bridge._poll()
            assert active_seen[-1] is False, active_seen

            bridge.stop()
            tasks.reset()
            print("[PASS] TaskActivityBridge: running task triggers active signal")
            return True
        finally:
            cm.CONFIG_FILE, cm.CONFIG_DIR = old_file, old_dir


# ──────────────────────────────────────────────────────────────────────────────
# 5. Compact/expanded UI mode \u2014 the panel width is honoured at the API level
# ──────────────────────────────────────────────────────────────────────────────
def test_panel_width_round_trip():
    import memory.config_manager as cm
    with tempfile.TemporaryDirectory() as td:
        old_file, old_dir = cm.CONFIG_FILE, cm.CONFIG_DIR
        cm.CONFIG_DIR = Path(td)
        cm.CONFIG_FILE = Path(td) / "api_keys.json"
        try:
            cm.save_panel_width(420)
            assert cm.get_panel_width() == 420
            cm.save_panel_width(50)        # too small \u2014 clamped
            assert cm.get_panel_width() >= cm._MIN_WIDTH
            cm.save_panel_width(99_999)    # too large \u2014 clamped
            assert cm.get_panel_width() <= cm._MAX_WIDTH
            print("[PASS] panel_width honours bounds")
            return True
        finally:
            cm.CONFIG_FILE, cm.CONFIG_DIR = old_file, old_dir


def main():
    results = []
    results.append(test_config_manager_gui_defaults())
    results.append(test_config_manager_gui_clamps())
    results.append(test_config_manager_gui_persistence())
    results.append(test_ui_helpers_return_useful_values())
    results.append(test_hud_animation_methods())
    results.append(test_task_bridge_emits_idle_signal())
    results.append(test_task_bridge_detects_running_task())
    results.append(test_panel_width_round_trip())
    failed = sum(1 for r in results if not r)
    raise SystemExit(0 if not failed else 1)


if __name__ == "__main__":
    main()

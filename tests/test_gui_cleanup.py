"""Headless logic regressions: execute actual UI methods with small Qt doubles.

These checks do not claim to render Qt or validate Windows DPI. AST extraction
lets positioning, mouse state and polling logic run even without native Qt libs.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

REPO = Path(__file__).resolve().parent
if REPO.name == "tests":
    REPO = REPO.parent
TREE = ast.parse((REPO / "ui.py").read_text(encoding="utf-8"))


class Point:
    def __init__(self, x=0, y=0):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y

    def toPoint(self):
        return self

    def __add__(self, other):
        return Point(self.x() + other.x(), self.y() + other.y())

    def __sub__(self, other):
        return Point(self.x() - other.x(), self.y() - other.y())

    def manhattanLength(self):
        return abs(self.x()) + abs(self.y())


class Frame:
    def __init__(self, width=1000, height=700, parent=None):
        self.w, self.h = width, height
        self.parent = parent
        self.x, self.y = 0, 0
        self.hidden = True

    def parentWidget(self):
        return self.parent

    def width(self):
        return self.w

    def height(self):
        return self.h

    def pos(self):
        return Point(self.x, self.y)

    def move(self, x, y):
        self.x, self.y = x, y

    def isHidden(self):
        return self.hidden

    def show(self):
        self.hidden = False

    def hide(self):
        self.hidden = True

    def rect(self):
        return SimpleNamespace(contains=lambda p: 0 <= p.x() < self.w and 0 <= p.y() < self.h)


def load_class(name, methods, **namespace):
    source = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == name)
    selected = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    assert len(selected) == len(methods)
    cls = ast.ClassDef(name=name, bases=[ast.Name(id="Base", ctx=ast.Load())],
                       keywords=[], body=selected, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    env = {"Base": Frame, **namespace}
    exec(compile(module, str(REPO / "ui.py"), "exec"), env)
    return env[name]


def event(x, y, local_x=10, local_y=10):
    return SimpleNamespace(button=lambda: 1, buttons=lambda: 1,
                           globalPosition=lambda: Point(x, y),
                           position=lambda: Point(local_x, local_y), accept=Mock())


def make_overlay(parent):
    cls = load_class("TaskActivityOverlay", ["_move_within_parent", "reposition",
                     "mousePressEvent", "mouseMoveEvent", "mouseReleaseEvent"],
                     Qt=SimpleNamespace(MouseButton=SimpleNamespace(LeftButton=1)),
                     QApplication=SimpleNamespace(startDragDistance=lambda: 10))
    overlay = cls(140, 50, parent)
    overlay._manual_position = None
    overlay._press_global = None
    overlay._press_position = None
    overlay._dragging = False
    overlay.clicked = SimpleNamespace(emit=Mock())
    overlay.adjustSize = Mock()
    overlay.show_summary = Mock()
    overlay.raise_ = Mock()
    return overlay


def test_drag_click_and_resize():
    parent = Frame()
    chip = make_overlay(parent)
    chip.reposition(Point(600, 500))
    assert (chip.x, chip.y) == (600, 500)
    chip.mousePressEvent(event(620, 510))
    chip.clicked.emit.assert_not_called()  # No premature navigation during drag.
    chip.mouseMoveEvent(event(200, 100))
    chip.mouseReleaseEvent(event(200, 100))
    assert (chip.x, chip.y) == (180, 90)
    chip.clicked.emit.assert_not_called()
    chip.reposition(Point(500, 400))
    assert (chip.x, chip.y) == (180, 90), "resize snapped manual position"
    parent.w, parent.h = 250, 100
    chip.reposition(Point(5, 5))
    assert (chip.x, chip.y) == (110, 50), "must clamp to visible parent"
    parent.w, parent.h = 1000, 700
    chip.reposition(Point(600, 500))
    assert (chip.x, chip.y) == (180, 90), "restore after temporary window shrink"
    chip.w = 900
    chip.reposition(Point(600, 500))
    assert (chip.x, chip.y) == (100, 90), "content growth must stay in bounds"
    chip.w = 140
    chip.reposition(Point(600, 500))
    chip.mousePressEvent(event(190, 100))
    chip.mouseMoveEvent(event(193, 100))  # Below Qt's drag threshold: still a click.
    chip.mouseReleaseEvent(event(193, 100))
    chip.clicked.emit.assert_called_once_with()
    assert (chip.x, chip.y) == (180, 90)
    assert chip._press_global is None and not chip._dragging
    print("[PASS] overlay drag/click separation, resize preservation and clamping")


def test_anchor_summary_and_first_show():
    parent = Frame()
    chip = make_overlay(parent)
    panel = Frame(340, 700)
    panel.show()
    cls = load_class("MainWindow", ["_position_task_overlay", "_on_task_summary"],
                     QPointF=Point, _read_gui_settings=lambda: {"task_overlay_mode": "auto"})
    window = cls()
    window._task_overlay, window._right_panel = chip, panel
    window.centralWidget = lambda: parent
    # First update must position a still-hidden chip, not leave it at (0, 0).
    window._on_task_summary("Download", "5 MB", 1, "download", "running")
    assert not chip.hidden
    assert (chip.x, chip.y) == (500, 606)
    parent.w = 1200
    panel.w = 400  # Honour the actual configured panel width.
    window._position_task_overlay()
    assert (chip.x, chip.y) == (640, 606)
    chip._manual_position = Point(80, 100)
    window._on_task_summary("Download", "6 MB", 1, "download", "running")
    assert (chip.x, chip.y) == (80, 100), "summary update snapped manual position"
    window._on_task_summary("", "", 0, "task", "idle")
    assert chip.hidden
    window._on_task_summary("Next task", "", 1, "task", "running")
    assert (chip.x, chip.y) == (80, 100), "hide/show reset manual position"
    print("[PASS] first-show anchor, configured panel width and summary/hide/show preservation")


def test_bridge_delay_without_registry_revision():
    clock = SimpleNamespace(time=Mock(return_value=100.1))
    registry = SimpleNamespace(revision=Mock(return_value=1), snapshot=Mock(return_value=[
        {"state": "running", "kind": "task", "started_at": 100.0, "title": "Work"}]))
    settings = {"auto_task_delay_ms": 900, "snap_hud_on_task": True}
    cls = load_class("TaskActivityBridge", ["_poll"], time=clock,
                     _task_registry=registry, _read_gui_settings=lambda: settings)
    bridge = cls()
    bridge._revision, bridge._rows = -1, []
    bridge._last_active, bridge._last_summary = False, None
    bridge._active_started_at = 0.0
    bridge.task_active_changed = SimpleNamespace(emit=Mock())
    bridge.summary_changed = SimpleNamespace(emit=Mock())
    bridge._poll()
    bridge.task_active_changed.emit.assert_not_called()
    clock.time.return_value = 101.0
    bridge._poll()
    bridge.task_active_changed.emit.assert_called_once_with(True)
    registry.snapshot.assert_called_once_with(limit=32)  # Still cache snapshots.
    bridge.summary_changed.emit.assert_called_once()
    settings["snap_hud_on_task"] = False
    bridge._poll()
    assert bridge.task_active_changed.emit.call_args.args == (False,)
    assert bridge._active_started_at == 0.0
    print("[PASS] HUD grace delay and settings changes work without registry revisions")


def test_no_duplicate_methods_or_obsolete_dpi_calls():
    for cls in (n for n in TREE.body if isinstance(n, ast.ClassDef)):
        names = set()
        for method in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            # Properties intentionally share their name with their setter.
            if any(isinstance(d, ast.Attribute) and d.attr in ("setter", "deleter")
                   for d in method.decorator_list):
                continue
            assert method.name not in names, (cls.name, method.name)
            names.add(method.name)
    jarvis = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "JarvisUI")
    assert not any(isinstance(n, ast.Attribute) and n.attr in
                   ("setDevicePixelRatio", "AA_UseHighDpiPixmaps") for n in ast.walk(jarvis))
    log = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "LogWidget")
    prefixes = next(ast.literal_eval(n.value) for n in log.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "_DEBUG_PREFIXES" for t in n.targets))
    assert len(prefixes) == len(set(prefixes))
    print("[PASS] no duplicate GUI methods/debug prefixes or obsolete DPI calls")


if __name__ == "__main__":
    test_drag_click_and_resize()
    test_anchor_summary_and_first_show()
    test_bridge_delay_without_registry_revision()
    test_no_duplicate_methods_or_obsolete_dpi_calls()

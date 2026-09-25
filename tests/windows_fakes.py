"""Stand-ins for the Windows APIs, so Windows-only code can be executed here.

Roughly a fifth of this project only ever runs on Windows: registry scanning,
`.lnk` resolution, Store application ids, `user32` window handling, Task
Scheduler, WMI brightness. On Linux and macOS that code was never executed at
all — not by the test suite, not by the CI matrix, not by a developer. A
mistyped key name or a swapped pair of arguments in there survives every green
build and fails the first time a Windows user asks for it.

These fakes do not claim to reproduce Windows. They reproduce the *shape* of
the interfaces the project calls, which is enough to execute the code and
notice that it asks for the right things in the right order. What they cannot
tell anyone is whether the real API behaves as assumed; that still needs a
Windows machine, and the tests that use these fakes say so in their names.
"""
from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from pathlib import Path


class FakeRegistryKey:
    """One open key. Supports the context-manager use the scanner relies on."""

    def __init__(self, name: str, values: dict, children: dict) -> None:
        self.name = name
        self.values = values
        self.children = children
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True
        return False


class FakeWinreg(types.ModuleType):
    """Enough of ``winreg`` to drive the App Paths scan.

    The tree is supplied as {(root, subkey): {child: default_value}}, which is
    exactly the structure the scanner walks: open a key, enumerate children,
    read each child's default value.
    """

    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 0x20019
    KEY_WOW64_64KEY = 0x0100
    KEY_WOW64_32KEY = 0x0200

    def __init__(self, tree: dict | None = None) -> None:
        super().__init__("winreg")
        self.tree = tree or {}
        self.opened: list[tuple] = []
        self.closed = 0

    def OpenKey(self, root, subkey, reserved=0, access=0):  # noqa: N802 - Windows API name
        self.opened.append((root, subkey, access))
        if isinstance(root, FakeRegistryKey):
            if subkey not in root.children:
                raise OSError(2, "key not found")
            return FakeRegistryKey(subkey, {None: root.children[subkey]}, {})
        children = self.tree.get((root, subkey))
        if children is None:
            raise OSError(2, "key not found")
        return FakeRegistryKey(subkey, {}, children)

    def EnumKey(self, key: FakeRegistryKey, index: int):  # noqa: N802
        names = sorted(key.children)
        if index >= len(names):
            raise OSError(259, "no more data")
        return names[index]

    def QueryValueEx(self, key: FakeRegistryKey, name):  # noqa: N802
        if name not in key.values:
            raise OSError(2, "value not found")
        return key.values[name], 1

    def CloseKey(self, key) -> None:  # noqa: N802
        self.closed += 1


class FakeLink:
    def __init__(self, path: str = "", local_base_path: str = "") -> None:
        self.path = path
        self.local_base_path = local_base_path


class FakePylnk3(types.ModuleType):
    """``pylnk3.parse`` over a mapping of shortcut path to target."""

    def __init__(self, links: dict[str, FakeLink] | None = None) -> None:
        super().__init__("pylnk3")
        self.links = links or {}
        self.parsed: list[str] = []

    def parse(self, path: str):
        self.parsed.append(str(path))
        key = str(path)
        if key not in self.links:
            raise ValueError("not a shortcut")
        return self.links[key]


@contextmanager
def fake_modules(**modules):
    """Install fake modules under their real names for the duration of a test."""
    saved = {name: sys.modules.get(name) for name in modules}
    try:
        sys.modules.update(modules)
        yield modules
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def make_exe(directory: Path, name: str) -> Path:
    """A file that passes the scanner's 'is this really an executable' check."""
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ fake executable")
    return path


class FakeUser32:
    """A tiny stand-in for the handful of user32 calls the window list makes.

    Windows are described declaratively as dicts; the fake answers the same
    questions EnumWindows-driven code asks of the real API, including writing
    into the caller's buffers through ctypes.
    """

    def __init__(self, windows: list[dict]) -> None:
        self.windows = windows
        self.calls: list[str] = []

    def _find(self, hwnd):
        return next((w for w in self.windows if w["hwnd"] == hwnd), None)

    def IsWindowVisible(self, hwnd):  # noqa: N802 - Windows API name
        window = self._find(hwnd)
        return 1 if window and window.get("visible", True) else 0

    def GetWindowTextLengthW(self, hwnd):  # noqa: N802
        window = self._find(hwnd)
        return len(window.get("title", "")) if window else 0

    def GetWindowTextW(self, hwnd, buffer, _size):  # noqa: N802
        window = self._find(hwnd)
        buffer.value = window.get("title", "") if window else ""
        return len(buffer.value)

    def GetWindowThreadProcessId(self, hwnd, pid_pointer):  # noqa: N802
        window = self._find(hwnd)
        pid_pointer._obj.value = int(window.get("pid", 0)) if window else 0
        return 1

    def GetWindowRect(self, hwnd, rect_pointer):  # noqa: N802
        window = self._find(hwnd)
        if not window or not window.get("has_rect", True):
            return 0
        rect = rect_pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = window.get(
            "rect", (0, 0, 100, 100)
        )
        return 1

    def IsIconic(self, hwnd):  # noqa: N802
        window = self._find(hwnd)
        return 1 if window and window.get("minimized") else 0

    def IsZoomed(self, hwnd):  # noqa: N802
        window = self._find(hwnd)
        return 1 if window and window.get("maximized") else 0

    def EnumWindows(self, callback, _lparam):  # noqa: N802
        self.calls.append("EnumWindows")
        for window in self.windows:
            callback(window["hwnd"], 0)
        return 1

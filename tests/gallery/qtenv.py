"""Find MO2's bundled PyQt6 and start a QApplication, the way test_viewer.py does.

Nothing here shows a window; callers render with ``widget.grab()``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
DEFAULT_MO2 = SOURCE.parents[1] / "MO2-Skyrim-Logger"
_APP = None


def mo2_libs() -> Path | None:
    mo2 = Path(os.environ.get("FOMOD_LOGGER_MO2_ROOT", DEFAULT_MO2))
    libs = mo2 / "plugins" / "plugin_python" / "libs"
    if sys.version_info[:2] != (3, 12) or not (libs / "PyQt6" / "QtWidgets.pyd").is_file():
        return None
    return mo2


_OFF_SCREEN = None


def keep_off_screen(app) -> None:
    """Every window the tests make stays off the desktop, even the ones the code under test show()s (the compare
    dialog, the scan dialog): Qt's WA_DontShowOnScreen keeps a window fully working and "visible" to Qt while
    nothing reaches the screen. Set on each top-level widget when Qt polishes it, which is always before a show.
    MO2's Qt has only the windows platform (no offscreen), so this is how the tests stay invisible."""

    global _OFF_SCREEN
    if _OFF_SCREEN is not None:
        return
    from PyQt6.QtCore import QEvent, QObject, Qt
    from PyQt6.QtWidgets import QWidget

    class _OffScreen(QObject):
        def eventFilter(self, watched, event):  # noqa: N802 - Qt hook
            if event.type() == QEvent.Type.Polish and isinstance(watched, QWidget) and watched.isWindow():
                watched.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            return False

    _OFF_SCREEN = _OffScreen()
    app.installEventFilter(_OFF_SCREEN)


def desktop_windows() -> list[str]:
    """The titles of this process's windows that Windows shows on the desktop (none, while keep_off_screen works)."""

    import ctypes
    import ctypes.wintypes as wintypes

    user32 = ctypes.windll.user32
    found: list[str] = []
    pid = os.getpid()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _param):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(200)
            user32.GetWindowTextW(hwnd, title, 200)
            found.append(title.value)
        return True

    user32.EnumWindows(each, 0)
    return found


def load():
    """Return the ``viewer.window`` module, or None when PyQt6 isn't available.

    MO2's libs go at the END of sys.path (and stay there) so only PyQt6 comes
    from MO2 and nothing shadows ``_sqlite3``.
    """

    mo2 = mo2_libs()
    if mo2 is None:
        return None
    libs = mo2 / "plugins" / "plugin_python" / "libs"
    if str(libs) not in sys.path:
        os.add_dll_directory(str(mo2))
        os.add_dll_directory(str(mo2 / "dlls"))
        # Platforms sit in MO2's folder, image formats (SVG: the sayngwyn checkbox tick) in its dlls folder, as in MO2.
        os.environ.setdefault("QT_PLUGIN_PATH", os.pathsep.join((str(mo2), str(mo2 / "dlls"))))
        sys.path.append(str(libs))
    if str(SOURCE / "python") not in sys.path:
        sys.path.insert(0, str(SOURCE / "python"))
    from PyQt6.QtWidgets import QApplication

    global _APP
    if QApplication.instance() is None:
        _APP = QApplication(["fomod-gallery"])  # kept alive: Qt crashes if it is collected
    keep_off_screen(QApplication.instance())
    from fomod_logger_support.viewer import window

    return window

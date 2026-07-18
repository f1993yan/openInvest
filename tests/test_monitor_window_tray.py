from __future__ import annotations

import queue
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

from PIL import Image

from scripts.monitor_desktop_window import MonitorWindow
from scripts.monitor_window_tray import TrayController, find_apk_launcher_icon


def _write_launcher(root: Path, density: str) -> Path:
    path = root / "app" / "app" / "src" / "main" / "res" / f"mipmap-{density}" / "ic_launcher.webp"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), "#1677ff").save(path, format="WEBP")
    return path


def test_find_apk_launcher_icon_prefers_highest_density(tmp_path):
    expected = _write_launcher(tmp_path, "xxxhdpi")
    _write_launcher(tmp_path, "mdpi")
    assert find_apk_launcher_icon(tmp_path) == expected


def test_tray_controller_emits_commands_without_calling_tk(tmp_path, monkeypatch):
    _write_launcher(tmp_path, "xxxhdpi")
    commands = []

    class FakeMenuItem:
        def __init__(self, label, action, default=False):
            self.label = label
            self.action = action
            self.default = default

    class FakeMenu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items

    class FakeIcon:
        def __init__(self, name, image, title, menu):
            self.name = name
            self.image = image
            self.title = title
            self.menu = menu
            self.stopped = False

        def run_detached(self):
            return None

        def stop(self):
            self.stopped = True

    fake_pystray = types.SimpleNamespace(Menu=FakeMenu, MenuItem=FakeMenuItem, Icon=FakeIcon)
    monkeypatch.setitem(sys.modules, "pystray", fake_pystray)
    controller = TrayController(tmp_path, commands.append)

    assert controller.start() is True
    icon = controller.icon
    assert icon.title == "OpenInvest 标的监控"
    assert icon.menu.items[0].default is True
    icon.menu.items[0].action()
    icon.menu.items[2].action()
    assert commands == ["show", "quit"]
    controller.stop()
    assert icon.stopped is True


def test_window_hides_only_when_tray_is_available():
    win = MagicMock()
    win.closing = False
    win.tray_controller = types.SimpleNamespace(active=True)
    MonitorWindow._hide_to_tray(win)
    win.root.withdraw.assert_called_once_with()
    win._on_close.assert_not_called()


def test_window_close_falls_back_to_exit_without_tray():
    win = MagicMock()
    win.closing = False
    win.tray_controller = None
    MonitorWindow._hide_to_tray(win)
    win._on_close.assert_called_once_with()
    win.root.withdraw.assert_not_called()


def test_tray_show_temporarily_raises_window_then_restores_pin_state():
    win = MagicMock()
    win.closing = False
    win.pinned = False
    MonitorWindow._show_from_tray(win)
    win.root.deiconify.assert_called_once_with()
    win.root.lift.assert_called_once_with()
    win.root.attributes.assert_called_once_with("-topmost", True)
    callback = win.root.after.call_args.args[1]
    callback()
    assert win.root.attributes.call_args_list[-1].args == ("-topmost", False)


def test_tray_quit_command_uses_normal_shutdown_path():
    win = MagicMock()
    win.closing = False
    win.tray_commands = queue.Queue()
    win.tray_commands.put("quit")
    MonitorWindow._poll_tray_commands(win)
    win._on_close.assert_called_once_with()
    win.root.after.assert_not_called()

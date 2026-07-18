"""Windows system-tray integration for the desktop monitor."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_LAUNCHER_DENSITIES = ("xxxhdpi", "xxhdpi", "xhdpi", "hdpi", "mdpi")


def find_apk_launcher_icon(project_root: Path) -> Optional[Path]:
    """Return the highest-density launcher icon used by the Android APK."""
    resource_root = project_root / "app" / "app" / "src" / "main" / "res"
    for density in _LAUNCHER_DENSITIES:
        candidate = resource_root / f"mipmap-{density}" / "ic_launcher.webp"
        if candidate.is_file():
            return candidate
    return None


class TrayController:
    """Own a pystray icon without calling Tk from the tray worker thread."""

    def __init__(self, project_root: Path, command_sink: Callable[[str], None]) -> None:
        self.project_root = project_root
        self.command_sink = command_sink
        self.icon = None

    @property
    def active(self) -> bool:
        return self.icon is not None

    def start(self) -> bool:
        if self.active:
            return True
        icon_path = find_apk_launcher_icon(self.project_root)
        if icon_path is None:
            log.warning("APK launcher icon not found; tray mode disabled")
            return False
        try:
            import pystray
            from PIL import Image

            with Image.open(icon_path) as source:
                image = source.convert("RGBA").copy()

            def emit(command: str):
                def callback(_icon=None, _item=None) -> None:
                    self.command_sink(command)

                return callback

            menu = pystray.Menu(
                pystray.MenuItem("显示窗口", emit("show"), default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出 OpenInvest", emit("quit")),
            )
            self.icon = pystray.Icon(
                "OpenInvest",
                image,
                "OpenInvest 标的监控",
                menu,
            )
            self.icon.run_detached()
            return True
        except Exception as exc:
            self.icon = None
            log.warning("System tray initialization failed: %s", exc)
            return False

    def stop(self) -> None:
        icon, self.icon = self.icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception as exc:
                log.debug("System tray shutdown failed: %s", exc)


__all__ = ["TrayController", "find_apk_launcher_icon"]

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths


def app_data_dir() -> Path:
    """Writable per-user directory for this app's tmp/cache files.

    A double-clicked exe's CWD is wherever the exe file lives (e.g. under
    Program Files), which is often not writable -- see R2-9. Falls back to
    a dotfolder under the home directory on the rare platform where Qt
    can't resolve a standard location.
    """
    location = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    path = Path(location) if location else Path.home() / ".k2s-downloader"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_download_dir() -> Path:
    """Default pre-fill for the GUI's "save to" folder picker."""
    location = QStandardPaths.writableLocation(QStandardPaths.DownloadLocation)
    return Path(location) if location else Path.home()

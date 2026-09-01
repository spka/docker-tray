"""Typed mutable state for the Docker Tray process."""

import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ComposeScanState:
    running: bool = False
    results: list | None = None
    error: str | None = None
    spinner: Any = None
    label: Any = None
    search_root: Path = field(default_factory=Path.home)
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class CleanupState:
    spinner: Any = None


@dataclass
class UpdateCheckState:
    app_update: Any
    engine_update: Any
    image_updates: list[str] = field(default_factory=list)
    checking: bool = False
    last_checked: float | None = None
    errors: tuple[str, ...] = ()
    lock: threading.Lock = field(default_factory=threading.Lock)
    run_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class UpdatesDialogState:
    status: str = ""
    app_upgrading: bool = False
    engine_upgrading: bool = False
    pulling_images: set[str] = field(default_factory=set)

    def clear_if_idle(self):
        if not self.app_upgrading and not self.engine_upgrading and not self.pulling_images:
            self.status = ""


@dataclass
class ContainerHealthState:
    level: str = "ok"


@dataclass
class StatsHistoryState:
    initialized: bool = False
    peaks: dict = field(default_factory=dict)
    recent: deque = field(default_factory=deque)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def reset(self):
        self.initialized = False
        self.peaks = {}
        self.recent = deque()


@dataclass
class ContainerWatchState:
    ready: bool = False
    containers: tuple = ()
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class RemoteDigestCache:
    values: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class TrayMenuUpdateState:
    pending: bool = False
    tracked_root: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class DesktopNotificationState:
    connection: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)

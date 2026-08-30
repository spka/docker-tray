"""Explicit command paths for crossing Docker Tray's privilege boundary."""

import os
from pathlib import Path


PACKAGED_DOCKER_GATEWAY = Path("/usr/lib/docker-tray/docker")
PRIVILEGED_HELPER = Path("/usr/lib/docker-tray/docker_tray_privileged.py")
REAL_DOCKER = Path("/usr/bin/docker")


def docker_executable():
    """Return the packaged PolicyKit gateway, falling back for source development."""
    if PACKAGED_DOCKER_GATEWAY.is_file() and os.access(PACKAGED_DOCKER_GATEWAY, os.X_OK):
        return str(PACKAGED_DOCKER_GATEWAY)
    return "docker"


def docker_command(*args):
    return [docker_executable(), *args]


def privileged_command(action, *args):
    return ["pkexec", str(PRIVILEGED_HELPER), action, *args]

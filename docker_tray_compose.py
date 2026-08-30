"""Filesystem-only Docker Compose discovery helpers."""

import os
from pathlib import Path


COMPOSE_FILE_NAMES = {
    "compose.yml",
    "compose.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
}


def should_skip_scan_dir(root, dirname, home, skip_dirs):
    path = Path(root, dirname)
    try:
        relative = path.relative_to(home)
    except ValueError:
        relative = path
    normalized_relative = str(relative).lstrip("/")
    return (
        dirname in skip_dirs
        or str(relative) in skip_dirs
        or normalized_relative in skip_dirs
        or dirname.startswith(".")
    )


def scan_files(root, home, skip_dirs):
    compose_files = []
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            dirname
            for dirname in sorted(dirnames)
            if not should_skip_scan_dir(current_root, dirname, home, skip_dirs)
        ]
        for filename in sorted(filenames):
            if filename in COMPOSE_FILE_NAMES:
                compose_files.append(Path(current_root, filename))
    return sorted(compose_files, key=lambda path: sort_key(path, home))


def normalize_path(path):
    return Path(path).expanduser().resolve(strict=False)


def sort_key(compose_file, home):
    try:
        return str(compose_file.relative_to(home)).lower()
    except ValueError:
        return str(compose_file).lower()


def label(compose_file, home):
    try:
        relative = compose_file.relative_to(home)
    except ValueError:
        return str(compose_file)
    if compose_file.name in COMPOSE_FILE_NAMES:
        return str(relative.parent) if str(relative.parent) != "." else compose_file.name
    return str(relative)

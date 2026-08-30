#!/usr/bin/env python3
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gio

from PIL import Image

import docker_tray_platform


APP_VERSION = "0.2.7"
APP_LATEST_RELEASE_API_URL = "https://api.github.com/repos/spka/docker-tray/releases/latest"
APP_RELEASES_URL = "https://github.com/spka/docker-tray/releases"
APP_RELEASE_DOWNLOAD_URL_PREFIX = f"{APP_RELEASES_URL}/download/"
APP_UPDATE_TIMEOUT_SECONDS = 10
APP_UPGRADE_TIMEOUT_SECONDS = 10 * 60
PRIVILEGED_HELPER = "/usr/lib/docker-tray/docker_tray_privileged.py"
REAL_DOCKER = Path("/usr/bin/docker")
DOCKER_PS_FORMAT = "{{.Names}}\t{{.Status}}\t{{.Ports}}"
DOCKER_COMPOSE_LABEL_FORMAT = (
    "{{.Names}}\t"
    "{{.Status}}\t"
    "{{.Label \"com.docker.compose.project.config_files\"}}\t"
    "{{.Label \"com.docker.compose.project.working_dir\"}}"
)
HOST_PORT_RE = re.compile(r"(?:0\.0\.0\.0|127\.0\.0\.1):(\d+)->\d+/tcp")
MENU_REFRESH_SECONDS = 5
COMPOSE_START_POLL_SECONDS = 2
COMPOSE_START_POLL_ATTEMPTS = 30
UPDATE_CHECK_INTERVAL_SECONDS = 3600
DOCKER_CMD_TIMEOUT_SECONDS = 15
DOCKER_MANIFEST_TIMEOUT_SECONDS = 30
DOCKER_COMPOSE_UP_TIMEOUT_SECONDS = 3 * 60
DOCKER_ENGINE_UPGRADE_TIMEOUT_SECONDS = 30 * 60
DOCKER_IMAGE_UPDATE_TIMEOUT_SECONDS = 15 * 60
REMOTE_DIGEST_CACHE_SECONDS = 15 * 60
REMOTE_DIGEST_FAILURE_CACHE_SECONDS = 2 * 60
REMOTE_DIGEST_WORKERS = 4
COMMAND_ERROR_DETAIL_MAX_CHARS = 500
AUTOSTART_DESKTOP_FILE = Path.home() / ".config" / "autostart" / "docker-tray.desktop"
SYSTEM_AUTOSTART_DESKTOP_FILE = Path("/etc/xdg/autostart/docker-tray.desktop")
AUTOSTART_ENABLED_PREFIX = "X-GNOME-Autostart-enabled="
AUTOSTART_HIDDEN_PREFIX = "Hidden="
PLATFORM_INFO = docker_tray_platform.get_platform_info()
STATS_POLL_INTERVAL_SECONDS = 300
STATS_FILE = Path.home() / ".local" / "share" / "docker-tray" / "stats.jsonl"
CPU_COUNT = os.cpu_count() or 1
STATS_CPU_WARNING_CAPACITY = 0.60
STATS_CPU_CRITICAL_CAPACITY = 0.80
STATS_CPU_WARNING_PCT = 100.0 * CPU_COUNT * STATS_CPU_WARNING_CAPACITY
STATS_CPU_CRITICAL_PCT = 100.0 * CPU_COUNT * STATS_CPU_CRITICAL_CAPACITY
STATS_CPU_SUSTAINED_SAMPLES = 2
STATS_CPU_SUSTAINED_WINDOW_SECONDS = 15 * 60
STATS_MAX_SIZE_MB = 50
STATS_TRIM_DAYS = 30
COMPOSE_FILE_NAMES = {
    "compose.yml",
    "compose.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
}
COMPOSE_SCAN_SKIP_DIRS = {
    ".cache",
    ".git",
    ".local/share/Trash",
    "__pycache__",
    "dev",
    "node_modules",
    "proc",
    "run",
    "sys",
    "tmp",
    "var/lib/containerd",
    "var/lib/docker",
}


@dataclass(frozen=True)
class AppUpdate:
    available: bool
    latest_version: str = ""
    release_url: str = APP_RELEASES_URL
    package_url: str = ""
    package_digest: str = ""

    @property
    def can_install(self):
        return (
            bool(self.package_url)
            and bool(self.package_digest)
            and PLATFORM_INFO.is_debian_family
        )


compose_scan_lock = threading.Lock()
compose_scan_state = {
    "running": False,
    "results": None,
    "error": None,
}
compose_scan_loader = {
    "window": None,
    "spinner": None,
    "label": None,
    "content": None,
    "search_root": Path.home(),
}
cleanup_dialog = {
    "window": None,
    "content": None,
    "spinner": None,
}
update_check_state = {
    "app_update": AppUpdate(False),
    "engine_update": docker_tray_platform.EngineUpdate(False),
    "image_updates": [],
}
update_check_lock = threading.Lock()
update_check_run_lock = threading.Lock()
remote_digest_cache_lock = threading.Lock()
remote_digest_cache = {}
updates_dialog = {
    "window": None,
    "content": None,
    "status": "",
    "app_upgrading": False,
    "engine_upgrading": False,
    "pulling_images": set(),
}
container_stats_dialog = {
    "window": None,
    "content": None,
}
container_health_state = {
    "level": "ok",
}
stats_history_lock = threading.RLock()
stats_history_cache = {
    "initialized": False,
    "peaks": {},
    "recent": deque(),
}
tray_menu_update_lock = threading.Lock()
tray_menu_update_pending = False
container_watch_lock = threading.Lock()
container_watch_state = {
    "ready": False,
    "containers": (),
    "error": None,
}


ICON_NAMES = ("icon-dark.png", "icon-light.png")
ICON_DIRS = (
    Path(__file__).parent,
    Path("/usr/share/docker-tray"),
)
INSTANCE_LOCK_FILE = "docker-tray.lock"


def get_icon_dir():
    for icon_dir in ICON_DIRS:
        if all((icon_dir / name).exists() for name in ICON_NAMES):
            return icon_dir
    return ICON_DIRS[0]


def get_instance_lock_paths():
    fallback_path = Path("/tmp") / f"docker-tray-{os.getuid()}.lock"
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return (Path(runtime_dir) / INSTANCE_LOCK_FILE, fallback_path)
    return (fallback_path,)


def acquire_instance_lock():
    lock_file = None
    for lock_path in get_instance_lock_paths():
        try:
            flags = os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(lock_path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                os.close(descriptor)
                continue
            os.fchmod(descriptor, 0o600)
            lock_file = os.fdopen(descriptor, "w")
            break
        except OSError:
            continue
    if lock_file is None:
        return None

    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None

    lock_file.write(str(os.getpid()))
    lock_file.truncate()
    lock_file.flush()
    return lock_file


def is_dark_mode():
    return docker_tray_platform.is_dark_mode(Gio, PLATFORM_INFO)


def make_icon():
    name = "icon-dark.png" if is_dark_mode() else "icon-light.png"
    return Image.open(get_icon_dir() / name).convert("RGBA")


def watch_theme(icon):
    icon._theme_settings = docker_tray_platform.watch_theme(
        Gio,
        PLATFORM_INFO,
        lambda: setattr(icon, "icon", make_icon()),
    )


def get_containers():
    with container_watch_lock:
        if container_watch_state["ready"]:
            if container_watch_state["error"]:
                raise RuntimeError(container_watch_state["error"])
            return list(container_watch_state["containers"])

    result = subprocess.run(
        ["docker", "ps", "-a", "--format", DOCKER_PS_FORMAT],
        capture_output=True,
        text=True,
        timeout=DOCKER_CMD_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "docker ps failed"
        raise RuntimeError(format_docker_error(message))
    return parse_containers(result.stdout)


def format_docker_error(message):
    lower = message.lower()
    if "permission denied" in lower and "docker.sock" in lower:
        return "Docker socket permission denied. Reinstall Docker Tray's PolicyKit helper."
    if "no such file or directory" in lower and "docker.sock" in lower:
        return "Docker daemon is not running. Start docker.service."
    if "cannot connect to the docker daemon" in lower:
        return "Docker daemon is not running."
    return message


def is_docker_installed():
    return REAL_DOCKER.is_file() and os.access(REAL_DOCKER, os.X_OK)


def parse_container_watch_message(line):
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("invalid container status message")
    error = message.get("error")
    if error is not None:
        return (), str(error)[:300]
    raw_containers = message.get("containers")
    if not isinstance(raw_containers, list):
        raise ValueError("container status is missing")
    containers = []
    for container in raw_containers:
        if not isinstance(container, list) or len(container) != 3:
            raise ValueError("invalid container status entry")
        name, running, port = container
        if (
            not isinstance(name, str)
            or not isinstance(running, bool)
            or (port is not None and not isinstance(port, str))
        ):
            raise ValueError("invalid container status values")
        containers.append((name, running, port))
    return tuple(containers), None


def set_container_watch_state(containers=(), error=None):
    with container_watch_lock:
        previous = (
            container_watch_state["ready"],
            container_watch_state["containers"],
            container_watch_state["error"],
        )
        container_watch_state.update({
            "ready": True,
            "containers": tuple(containers),
            "error": error,
        })
        current = (
            container_watch_state["ready"],
            container_watch_state["containers"],
            container_watch_state["error"],
        )
    return current != previous


def parse_containers(output):
    containers = []
    for line in output.strip().splitlines():
        name, status, ports = parse_container_line(line)
        containers.append((name, is_running(status), extract_web_port(ports)))
    return containers


def parse_container_line(line):
    parts = line.split("\t", 2)
    name = parts[0]
    status = parts[1] if len(parts) > 1 else ""
    ports = parts[2] if len(parts) > 2 else ""
    return name, status, ports


def is_running(status):
    return status.startswith("Up ")


def container_sort_key(container):
    return container[0].lower()


def extract_web_port(ports_str):
    match = HOST_PORT_RE.search(ports_str)
    return match.group(1) if match else None


def get_command_failure_detail(result=None, error=None):
    if isinstance(error, subprocess.TimeoutExpired):
        detail = f"timed out after {error.timeout} seconds"
    elif error is not None:
        detail = f"{type(error).__name__}: {error}"
    elif result is not None:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
    else:
        detail = "unknown failure"
    detail = format_docker_error(detail)
    if len(detail) > COMMAND_ERROR_DETAIL_MAX_CHARS:
        return detail[:COMMAND_ERROR_DETAIL_MAX_CHARS].rstrip() + "…"
    return detail


def notify_command_failure(icon, operation, result=None, error=None):
    if icon is None:
        return
    detail = get_command_failure_detail(result=result, error=error)
    notify_user(icon, f"{operation}: {detail}")


def notify_user(icon, message):
    try:
        icon.notify(message, "Docker Tray")
    except Exception:
        pass


def run_docker_action(action, name, icon=None):
    def _run():
        try:
            result = subprocess.run(
                ["docker", action, name],
                capture_output=True,
                text=True,
                timeout=DOCKER_CMD_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                notify_command_failure(icon, f"Could not {action} {name}", result=result)
        except Exception as error:
            notify_command_failure(icon, f"Could not {action} {name}", error=error)
        if icon is not None:
            update_tray_menu(icon)

    threading.Thread(target=_run, daemon=True).start()


def run_compose_up(compose_file, icon=None, on_finished=None):
    def _run():
        success = False
        try:
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "up", "-d"],
                cwd=Path(compose_file).resolve().parent,
                capture_output=True,
                text=True,
                timeout=DOCKER_COMPOSE_UP_TIMEOUT_SECONDS,
            )
            success = result.returncode == 0
            if not success:
                notify_command_failure(icon, f"Could not start {compose_file}", result=result)
        except Exception as error:
            notify_command_failure(icon, f"Could not start {compose_file}", error=error)
        if icon is not None:
            update_tray_menu(icon)
        if on_finished is not None:
            GLib.idle_add(on_finished, success)

    threading.Thread(target=_run, daemon=True).start()


def run_docker_capture(args, check=True):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=check,
        timeout=DOCKER_CMD_TIMEOUT_SECONDS,
    )


def should_skip_compose_scan_dir(root, dirname):
    path = Path(root, dirname)
    try:
        rel = path.relative_to(Path.home())
    except ValueError:
        rel = path
    normalized_rel = str(rel).lstrip("/")
    return (
        dirname in COMPOSE_SCAN_SKIP_DIRS
        or str(rel) in COMPOSE_SCAN_SKIP_DIRS
        or normalized_rel in COMPOSE_SCAN_SKIP_DIRS
        or dirname.startswith(".")
    )


def scan_compose_files(root=Path.home()):
    compose_files = []
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            dirname for dirname in sorted(dirnames)
            if not should_skip_compose_scan_dir(current_root, dirname)
        ]

        for filename in sorted(filenames):
            if filename in COMPOSE_FILE_NAMES:
                compose_files.append(Path(current_root, filename))

    return sorted(compose_files, key=compose_file_sort_key)


def normalize_compose_path(path):
    return Path(path).expanduser().resolve(strict=False)


def get_compose_file_states_from_containers():
    result = subprocess.run(
        ["docker", "ps", "-a", "--format", DOCKER_COMPOSE_LABEL_FORMAT],
        capture_output=True,
        text=True,
        check=True,
        timeout=DOCKER_CMD_TIMEOUT_SECONDS,
    )
    compose_states = {}

    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 4:
            continue

        status = parts[1].strip()
        config_files = parts[2].strip()
        working_dir = parts[3].strip()
        if not config_files:
            continue

        for config_file in config_files.split(","):
            config_file = config_file.strip()
            if not config_file:
                continue
            config_path = Path(config_file)
            if not config_path.is_absolute() and working_dir:
                config_path = Path(working_dir, config_path)
            normalized_path = normalize_compose_path(config_path)
            compose_states[normalized_path] = (
                compose_states.get(normalized_path, False) or is_running(status)
            )

    return compose_states


def get_scanned_compose_files_with_states(root):
    compose_files = scan_compose_files(root)
    try:
        compose_states = get_compose_file_states_from_containers()
    except Exception:
        compose_states = {}

    return [
        (compose_file, compose_states.get(normalize_compose_path(compose_file)))
        for compose_file in compose_files
    ]


def get_compose_file_running_state(compose_file):
    try:
        compose_states = get_compose_file_states_from_containers()
    except Exception:
        return None
    return compose_states.get(normalize_compose_path(compose_file))


def compose_file_sort_key(compose_file):
    try:
        return str(compose_file.relative_to(Path.home())).lower()
    except ValueError:
        return str(compose_file).lower()


def compose_file_label(compose_file):
    try:
        rel = compose_file.relative_to(Path.home())
    except ValueError:
        return str(compose_file)

    if compose_file.name in COMPOSE_FILE_NAMES:
        return str(rel.parent) if str(rel.parent) != "." else compose_file.name
    return str(rel)


def run_pending_tray_menu_update(icon):
    global tray_menu_update_pending
    try:
        icon.update_menu()
    except Exception:
        pass
    with tray_menu_update_lock:
        tray_menu_update_pending = False
    return GLib.SOURCE_REMOVE


def update_tray_menu(icon):
    global tray_menu_update_pending
    with tray_menu_update_lock:
        if tray_menu_update_pending:
            return
        tray_menu_update_pending = True
    GLib.idle_add(run_pending_tray_menu_update, icon)


def count_unique_output_lines(output):
    return len({line.strip() for line in output.splitlines() if line.strip()})


def output_line_set(output):
    return {line.strip() for line in output.splitlines() if line.strip()}


def get_container_image_ids():
    container_result = run_docker_capture([
        "docker",
        "ps",
        "-a",
        "-q",
    ])
    container_ids = sorted(output_line_set(container_result.stdout))
    if not container_ids:
        return set()

    result = run_docker_capture([
        "docker",
        "inspect",
        "--format",
        "{{.Image}}",
        *container_ids,
    ])
    return output_line_set(result.stdout)


def get_removable_dangling_image_count():
    dangling_result = run_docker_capture([
        "docker",
        "images",
        "--filter",
        "dangling=true",
        "--no-trunc",
        "-q",
    ])
    dangling_images = output_line_set(dangling_result.stdout)
    container_images = get_container_image_ids()
    return len(dangling_images - container_images)


def get_docker_cleanup_report():
    stopped_result = run_docker_capture([
        "docker",
        "ps",
        "-a",
        "--filter",
        "status=exited",
        "--filter",
        "status=created",
        "-q",
    ])
    unused_networks_result = run_docker_capture([
        "docker",
        "network",
        "ls",
        "--filter",
        "dangling=true",
        "-q",
    ])

    report = [
        ("Stopped containers", count_unique_output_lines(stopped_result.stdout), None),
        ("Dangling images", get_removable_dangling_image_count(), None),
        ("Unused networks", count_unique_output_lines(unused_networks_result.stdout), None),
    ]

    try:
        df_result = run_docker_capture([
            "docker",
            "system",
            "df",
            "--format",
            "{{.Type}}\t{{.Reclaimable}}",
        ])
        for line in df_result.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2 or parts[0] != "Build Cache":
                continue
            reclaimable = parts[1].strip()
            if not reclaimable.startswith("0B"):
                report.append(("Build cache", 1, reclaimable))
            break
    except Exception:
        pass

    return report


def cleanup_report_has_work(report):
    return any(count > 0 for _label, count, _detail in report)


def run_docker_cleanup():
    result = subprocess.run(
        ["pkexec", PRIVILEGED_HELPER, "cleanup"],
        capture_output=True,
        text=True,
        timeout=4 * DOCKER_CMD_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Docker cleanup failed"
        raise RuntimeError(detail)
    return result.stdout.strip() or "Docker cleanup completed"


def clear_cleanup_dialog():
    cleanup_dialog["window"] = None
    cleanup_dialog["content"] = None
    cleanup_dialog["spinner"] = None
    return GLib.SOURCE_REMOVE


def destroy_cleanup_dialog():
    window = cleanup_dialog["window"]
    if window is not None:
        window.destroy()
    clear_cleanup_dialog()
    return GLib.SOURCE_REMOVE


def ensure_cleanup_dialog():
    if cleanup_dialog["window"] is not None:
        cleanup_dialog["window"].present()
        return cleanup_dialog["window"]

    window = Gtk.Window(title="Docker Cleanup")
    window.set_default_size(520, 300)
    window.set_resizable(True)
    window.set_keep_above(True)
    window.set_skip_taskbar_hint(True)
    window.set_position(Gtk.WindowPosition.CENTER)
    window.connect("destroy", lambda w: clear_cleanup_dialog())

    cleanup_dialog["window"] = window
    return window


def set_cleanup_dialog_content(content):
    window = cleanup_dialog["window"]
    old_content = cleanup_dialog["content"]
    if window is None:
        return
    if old_content is not None:
        window.remove(old_content)
    cleanup_dialog["content"] = content
    window.add(content)
    window.show_all()
    window.present()


def show_cleanup_progress(message):
    ensure_cleanup_dialog()

    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.set_border_width(16)

    spinner = Gtk.Spinner()
    spinner.start()
    label = Gtk.Label(label=message)
    label.set_xalign(0)

    box.pack_start(spinner, False, False, 0)
    box.pack_start(label, True, True, 0)

    cleanup_dialog["spinner"] = spinner
    set_cleanup_dialog_content(box)
    return GLib.SOURCE_REMOVE


def make_cleanup_report_row(label, count, detail):
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

    name = Gtk.Label(label=label)
    name.set_xalign(0)
    name.set_hexpand(True)

    if detail:
        value_text = detail
    else:
        value_text = str(count)
    value = Gtk.Label(label=value_text)
    value.set_xalign(1)

    row.pack_start(name, True, True, 0)
    row.pack_start(value, False, False, 0)
    return row


def add_cleanup_output(box, cleanup_output):
    if not cleanup_output:
        return

    label = Gtk.Label(label="Cleanup output")
    label.set_xalign(0)
    box.pack_start(label, False, False, 0)

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroller.set_min_content_height(120)

    output_label = Gtk.Label(label=cleanup_output)
    output_label.set_xalign(0)
    output_label.set_yalign(0)
    output_label.set_selectable(True)
    output_label.set_line_wrap(True)

    scroller.add(output_label)
    box.pack_start(scroller, True, True, 0)


def show_cleanup_results(icon, report=None, error=None, cleaned=False, cleanup_output=None):
    ensure_cleanup_dialog()

    spinner = cleanup_dialog["spinner"]
    if spinner is not None:
        spinner.stop()
    cleanup_dialog["spinner"] = None

    box = make_dialog_box()

    if error:
        title = Gtk.Label(label=f"Docker cleanup check failed: {error}")
        title.set_xalign(0)
        title.set_line_wrap(True)
        box.pack_start(title, False, False, 0)
    elif not cleanup_report_has_work(report):
        title_text = "Everything is fine"
        if cleaned:
            title_text = "Cleanup finished. Everything is fine"
        title = Gtk.Label(label=title_text)
        title.set_xalign(0)
        box.pack_start(title, False, False, 0)
        add_cleanup_output(box, cleanup_output)
    else:
        title_text = "Docker cleanup opportunities"
        if cleaned:
            title_text = "Some Docker cleanup candidates remain"
        title = Gtk.Label(label=title_text)
        title.set_xalign(0)
        box.pack_start(title, False, False, 0)

        for label, count, detail in report:
            if count > 0:
                box.pack_start(make_cleanup_report_row(label, count, detail), False, False, 0)

        note = Gtk.Label(
            label=(
                "Cleanup prunes stopped containers, dangling images, unused "
                "networks, and builder cache. Volumes are not removed."
            )
        )
        note.set_xalign(0)
        note.set_line_wrap(True)
        box.pack_start(note, False, False, 0)
        add_cleanup_output(box, cleanup_output)

    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    buttons.set_halign(Gtk.Align.END)

    close_button = Gtk.Button(label="Close")
    close_button.connect("clicked", lambda button: destroy_cleanup_dialog())
    buttons.pack_start(close_button, False, False, 0)

    if not error and cleanup_report_has_work(report):
        cleanup_button = Gtk.Button(label="Cleanup")
        cleanup_button.connect("clicked", lambda button: start_docker_cleanup(icon))
        buttons.pack_start(cleanup_button, False, False, 0)

    add_bottom_button_row(box, buttons)
    set_cleanup_dialog_content(box)
    return GLib.SOURCE_REMOVE


def start_docker_cleanup_check(icon, cleaned=False, cleanup_output=None):
    GLib.idle_add(show_cleanup_progress, "Checking Docker cleanup state...")

    def _check():
        try:
            report = get_docker_cleanup_report()
            error = None
        except Exception as e:
            report = []
            error = f"{type(e).__name__}: {e}"

        update_tray_menu(icon)
        GLib.idle_add(show_cleanup_results, icon, report, error, cleaned, cleanup_output)

    threading.Thread(target=_check, daemon=True).start()


def start_docker_cleanup(icon):
    GLib.idle_add(show_cleanup_progress, "Cleaning Docker...")

    def _cleanup():
        try:
            cleanup_output = run_docker_cleanup()
        except Exception as e:
            GLib.idle_add(show_cleanup_results, icon, [], f"{type(e).__name__}: {e}", False, None)
            return

        update_tray_menu(icon)
        start_docker_cleanup_check(icon, cleaned=True, cleanup_output=cleanup_output)

    threading.Thread(target=_cleanup, daemon=True).start()


def open_cleanup_dialog(icon):
    ensure_cleanup_dialog()
    start_docker_cleanup_check(icon)
    return GLib.SOURCE_REMOVE


def clear_compose_scan_window():
    compose_scan_loader["window"] = None
    compose_scan_loader["spinner"] = None
    compose_scan_loader["label"] = None
    compose_scan_loader["content"] = None
    return GLib.SOURCE_REMOVE


def destroy_compose_scan_window():
    window = compose_scan_loader["window"]
    if window is not None:
        window.destroy()
    clear_compose_scan_window()
    return GLib.SOURCE_REMOVE


def ensure_compose_scan_window(icon):
    if compose_scan_loader["window"] is not None:
        compose_scan_loader["window"].present()
        return compose_scan_loader["window"]

    window = Gtk.Window(title="Docker Tray")
    window.set_default_size(680, 260)
    window.set_resizable(True)
    window.set_keep_above(True)
    window.set_skip_taskbar_hint(True)
    window.set_position(Gtk.WindowPosition.CENTER)
    window.connect("destroy", lambda w: clear_compose_scan_window())

    compose_scan_loader["window"] = window
    return window


def set_compose_scan_content(content):
    window = compose_scan_loader["window"]
    old_content = compose_scan_loader["content"]
    if window is None:
        return
    if old_content is not None:
        window.remove(old_content)
    compose_scan_loader["content"] = content
    window.add(content)
    window.show_all()
    window.present()


def make_dialog_box():
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.set_border_width(16)
    return box


def add_bottom_button_row(box, buttons):
    spacer = Gtk.Box()
    spacer.set_vexpand(True)
    box.pack_start(spacer, True, True, 0)
    box.pack_start(buttons, False, False, 0)


def get_compose_scan_locations():
    candidates = [
        ("Home", Path.home()),
        ("Development", Path.home() / "development"),
        ("Documents", Path.home() / "Documents"),
        ("Downloads", Path.home() / "Downloads"),
        ("Desktop", Path.home() / "Desktop"),
        ("Projects", Path.home() / "Projects"),
    ]
    locations = []
    seen = set()
    for label, path in candidates:
        if not path.exists() or not path.is_dir():
            continue
        normalized = str(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        locations.append((label, path))
    return locations


def build_scan_location_dropdown():
    locations = get_compose_scan_locations()
    combo = Gtk.ComboBoxText()
    for label, path in locations:
        combo.append(str(path), f"{label} ({path})")
    combo.set_active_id(str(Path.home()))
    if combo.get_active_id() is None and locations:
        combo.set_active(0)
    return combo


def get_selected_scan_root(combo):
    active_id = combo.get_active_id()
    return Path(active_id) if active_id else Path.home()


def open_compose_scan_dialog(icon):
    window = ensure_compose_scan_window(icon)
    window.resize(680, 220)

    box = make_dialog_box()

    title = Gtk.Label(label="Search for compose files?")
    title.set_xalign(0)

    location_label = Gtk.Label(label="Search directory")
    location_label.set_xalign(0)

    location = build_scan_location_dropdown()

    detail = Gtk.Label(label="After scanning, each compose file will show whether it is running.")
    detail.set_xalign(0)
    detail.set_line_wrap(True)

    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    buttons.set_halign(Gtk.Align.END)

    cancel_button = Gtk.Button(label="Cancel")
    cancel_button.connect("clicked", lambda button: destroy_compose_scan_window())

    search_button = Gtk.Button(label="Search")
    search_button.connect(
        "clicked",
        lambda button: start_compose_scan(icon, get_selected_scan_root(location)),
    )

    buttons.pack_start(cancel_button, False, False, 0)
    buttons.pack_start(search_button, False, False, 0)

    box.pack_start(title, False, False, 0)
    box.pack_start(location_label, False, False, 0)
    box.pack_start(location, False, False, 0)
    box.pack_start(detail, False, False, 0)
    add_bottom_button_row(box, buttons)
    set_compose_scan_content(box)
    return GLib.SOURCE_REMOVE


def show_compose_scan_progress(icon):
    window = ensure_compose_scan_window(icon)
    window.resize(680, 180)

    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.set_border_width(16)

    spinner = Gtk.Spinner()
    spinner.start()

    label = Gtk.Label(label=f"Searching {compose_scan_loader['search_root']}...")
    label.set_xalign(0)

    box.pack_start(spinner, False, False, 0)
    box.pack_start(label, True, True, 0)

    compose_scan_loader["spinner"] = spinner
    compose_scan_loader["label"] = label
    set_compose_scan_content(box)
    return GLib.SOURCE_REMOVE


def stop_compose_scan_spinner():
    spinner = compose_scan_loader["spinner"]
    if spinner is not None:
        spinner.stop()
    compose_scan_loader["spinner"] = None


def set_compose_dialog_action_running(row, action):
    if row.get_parent() is None:
        return GLib.SOURCE_REMOVE
    row.remove(action)
    running_label = Gtk.Label(label="Running")
    running_label.set_xalign(1)
    row.pack_start(running_label, False, False, 0)
    row.show_all()
    return GLib.SOURCE_REMOVE


def set_compose_dialog_action_failed(button):
    if button.get_parent() is None:
        return GLib.SOURCE_REMOVE
    button.set_label("Run")
    button.set_sensitive(True)
    return GLib.SOURCE_REMOVE


def poll_compose_start_state(compose_file, icon, row, action):
    for _attempt in range(COMPOSE_START_POLL_ATTEMPTS):
        time.sleep(COMPOSE_START_POLL_SECONDS)
        if get_compose_file_running_state(compose_file):
            GLib.idle_add(set_compose_dialog_action_running, row, action)
            update_tray_menu(icon)
            return

    waited_seconds = COMPOSE_START_POLL_ATTEMPTS * COMPOSE_START_POLL_SECONDS
    notify_user(
        icon,
        f"Could not start {compose_file}: no running container appeared within {waited_seconds} seconds",
    )
    GLib.idle_add(set_compose_dialog_action_failed, action)


def run_compose_file_from_dialog(compose_file, icon, button):
    button.set_label("Starting")
    button.set_sensitive(False)
    update_tray_menu(icon)
    row = button.get_parent()

    def _finished(success):
        if not success:
            return set_compose_dialog_action_failed(button)
        threading.Thread(
            target=poll_compose_start_state,
            args=(compose_file, icon, row, button),
            daemon=True,
        ).start()
        return GLib.SOURCE_REMOVE

    run_compose_up(compose_file, icon, _finished)


def close_compose_scan_window_countdown(label, remaining):
    window = compose_scan_loader["window"]
    if window is None:
        return GLib.SOURCE_REMOVE
    if remaining <= 0:
        return destroy_compose_scan_window()

    label.set_text(f"No compose files found. Closing in {remaining}...")
    GLib.timeout_add(1000, close_compose_scan_window_countdown, label, remaining - 1)
    return GLib.SOURCE_REMOVE


def show_compose_scan_results(icon, results, error):
    window = ensure_compose_scan_window(icon)
    window.resize(620, 420)
    stop_compose_scan_spinner()

    box = make_dialog_box()

    if error:
        message = Gtk.Label(label=f"Compose search failed: {error}")
        message.set_xalign(0)
        message.set_line_wrap(True)
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda button: destroy_compose_scan_window())
        close_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        close_row.set_halign(Gtk.Align.END)
        close_row.pack_start(close_button, False, False, 0)
        box.pack_start(message, False, False, 0)
        add_bottom_button_row(box, close_row)
        set_compose_scan_content(box)
        return GLib.SOURCE_REMOVE

    if not results:
        message = Gtk.Label(label="No compose files found. Closing in 3...")
        message.set_xalign(0)
        box.pack_start(message, False, False, 0)
        set_compose_scan_content(box)
        GLib.timeout_add(1000, close_compose_scan_window_countdown, message, 2)
        return GLib.SOURCE_REMOVE

    title = Gtk.Label(label=f"Found {len(results)} compose file(s)")
    title.set_xalign(0)
    box.pack_start(title, False, False, 0)

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_min_content_height(220)

    list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    for compose_file, running in results:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row_label = Gtk.Label(label=compose_file_label(compose_file))
        row_label.set_xalign(0)
        row_label.set_hexpand(True)
        row_label.set_line_wrap(True)

        if running:
            action = Gtk.Label(label="Running")
            action.set_xalign(1)
        else:
            action = Gtk.Button(label="Run")
            action.connect(
                "clicked",
                lambda button, path=compose_file: run_compose_file_from_dialog(path, icon, button),
            )

        row.pack_start(row_label, True, True, 0)
        row.pack_start(action, False, False, 0)
        list_box.pack_start(row, False, False, 0)

    scroller.add(list_box)
    box.pack_start(scroller, True, True, 0)

    close_button = Gtk.Button(label="Close")
    close_button.connect("clicked", lambda button: destroy_compose_scan_window())
    close_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    close_row.set_halign(Gtk.Align.END)
    close_row.pack_start(close_button, False, False, 0)
    add_bottom_button_row(box, close_row)

    set_compose_scan_content(box)
    return GLib.SOURCE_REMOVE


def start_compose_scan(icon, root):
    with compose_scan_lock:
        if compose_scan_state["running"]:
            show_compose_scan_progress(icon)
            return
        compose_scan_state["running"] = True
        compose_scan_state["results"] = None
        compose_scan_state["error"] = None
        compose_scan_loader["search_root"] = root

    show_compose_scan_progress(icon)
    update_tray_menu(icon)

    def _scan():
        try:
            results = get_scanned_compose_files_with_states(root)
            error = None
        except Exception as e:
            results = []
            error = f"{type(e).__name__}: {e}"

        with compose_scan_lock:
            compose_scan_state["running"] = False
            compose_scan_state["results"] = results
            compose_scan_state["error"] = error

        update_tray_menu(icon)
        GLib.idle_add(show_compose_scan_results, icon, results, error)

    threading.Thread(target=_scan, daemon=True).start()


def read_autostart_enabled():
    files = (AUTOSTART_DESKTOP_FILE, SYSTEM_AUTOSTART_DESKTOP_FILE)
    for desktop_file in files:
        if not desktop_file.exists():
            continue

        try:
            lines = desktop_file.read_text().splitlines()
        except Exception:
            continue

        for line in lines:
            if line.startswith(AUTOSTART_HIDDEN_PREFIX):
                return line.split("=", 1)[1].strip().lower() != "true"
            if line.startswith(AUTOSTART_ENABLED_PREFIX):
                return line.split("=", 1)[1].strip().lower() == "true"

        return desktop_file == SYSTEM_AUTOSTART_DESKTOP_FILE

    return False


def get_autostart_exec():
    script_path = Path(__file__).resolve()
    if script_path in (
        Path("/usr/bin/docker-tray"),
        Path("/usr/lib/docker-tray/docker_tray.py"),
    ):
        return "docker-tray"
    return f'python3 "{script_path}"'


def build_autostart_desktop(enabled):
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=Docker Tray",
        f"Exec={get_autostart_exec()}",
        "Icon=docker",
        "Comment=Docker container monitor in the system tray",
        f"{AUTOSTART_HIDDEN_PREFIX}{str(not enabled).lower()}",
        f"{AUTOSTART_ENABLED_PREFIX}{str(enabled).lower()}",
        "",
    ])


def write_autostart_enabled(enabled):
    AUTOSTART_DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)

    try:
        lines = AUTOSTART_DESKTOP_FILE.read_text().splitlines()
    except FileNotFoundError:
        lines = build_autostart_desktop(enabled).splitlines()
    except Exception:
        return
    else:
        gnome_updated = False
        hidden_updated = False
        for index, line in enumerate(lines):
            if line.startswith(AUTOSTART_ENABLED_PREFIX):
                lines[index] = f"{AUTOSTART_ENABLED_PREFIX}{str(enabled).lower()}"
                gnome_updated = True
            elif line.startswith(AUTOSTART_HIDDEN_PREFIX):
                lines[index] = f"{AUTOSTART_HIDDEN_PREFIX}{str(not enabled).lower()}"
                hidden_updated = True
        if not hidden_updated:
            lines.append(f"{AUTOSTART_HIDDEN_PREFIX}{str(not enabled).lower()}")
        if not gnome_updated:
            lines.append(f"{AUTOSTART_ENABLED_PREFIX}{str(enabled).lower()}")

    AUTOSTART_DESKTOP_FILE.write_text("\n".join(lines).rstrip() + "\n")


def toggle_start_at_boot(icon, item):
    write_autostart_enabled(not read_autostart_enabled())
    update_tray_menu(icon)


def get_start_at_boot_label(item):
    return "Start at boot ✓" if read_autostart_enabled() else "Start at boot"


def open_uri(uri):
    def _open():
        bridge = Gtk.Window()
        bridge.set_default_size(1, 1)

        def on_map(w):
            Gtk.show_uri_on_window(w, uri, 0)
            GLib.timeout_add(500, lambda: w.destroy() or False)

        bridge.connect("map", on_map)
        bridge.show()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(_open)


def open_url(port):
    open_uri(f"http://localhost:{port}")


def open_docker_install():
    open_uri(docker_tray_platform.get_docker_install_url(PLATFORM_INFO))


def make_open_cb(port):
    return lambda icon, item: open_url(port)


def make_install_docker_cb():
    return lambda icon, item: open_docker_install()


def make_compose_search_cb():
    return lambda icon, item: GLib.idle_add(open_compose_scan_dialog, icon)


def make_cleanup_cb():
    return lambda icon, item: GLib.idle_add(open_cleanup_dialog, icon)


def make_start_cb(name):
    return lambda icon, item: run_docker_action("start", name, icon)


def make_restart_cb(name):
    return lambda icon, item: run_docker_action("restart", name, icon)


def make_stop_cb(name):
    return lambda icon, item: run_docker_action("stop", name, icon)


def parse_cpu_pct(s):
    try:
        return float(s.strip().rstrip("%"))
    except Exception:
        return 0.0


def parse_mem_bytes(s):
    match = re.match(r"([\d.]+)\s*(B|kB|KiB|MB|MiB|GB|GiB)", s.strip())
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2)
    multipliers = {"B": 1, "kB": 1000, "KiB": 1024, "MB": 1_000_000,
                   "MiB": 1024**2, "GB": 1_000_000_000, "GiB": 1024**3}
    return int(value * multipliers.get(unit, 1))


def format_bytes(n):
    for unit, threshold in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= threshold:
            return f"{n / threshold:.1f} {unit}"
    return f"{n} B"


def format_uptime(seconds):
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_ts(ts):
    import datetime
    dt = datetime.datetime.fromtimestamp(ts)
    if dt.date() == datetime.date.today():
        return dt.strftime("%H:%M")
    return dt.strftime("%d %b %H:%M")


def collect_stats_sample():
    result = run_docker_capture(
        ["docker", "stats", "--no-stream", "--format",
         "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"],
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "docker stats failed"
        raise RuntimeError(format_docker_error(message))
    ts = time.time()
    samples = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, cpu_str, mem_str = parts
        mem_used = mem_str.split("/")[0].strip() if "/" in mem_str else mem_str.strip()
        samples.append({
            "t": ts,
            "name": name.strip(),
            "cpu": parse_cpu_pct(cpu_str),
            "mem": parse_mem_bytes(mem_used),
            "mem_str": mem_str.strip(),
        })
    return samples


def append_stats_to_file(samples):
    with stats_history_lock:
        STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with STATS_FILE.open("a") as f:
            for sample in samples:
                f.write(json.dumps(sample) + "\n")
                if stats_history_cache["initialized"]:
                    add_stats_entry_to_cache(sample)


def add_stats_entry_to_cache(entry, recent_cutoff=None):
    try:
        name = entry["name"]
        timestamp = float(entry["t"])
        cpu = float(entry["cpu"])
        memory = int(entry["mem"])
    except (KeyError, TypeError, ValueError):
        return

    peak = stats_history_cache["peaks"].setdefault(
        name,
        {"cpu": 0.0, "cpu_ts": 0, "mem": 0, "mem_ts": 0},
    )
    if cpu > peak["cpu"]:
        peak["cpu"] = cpu
        peak["cpu_ts"] = timestamp
    if memory > peak["mem"]:
        peak["mem"] = memory
        peak["mem_ts"] = timestamp

    if recent_cutoff is None:
        recent_cutoff = time.time() - STATS_CPU_SUSTAINED_WINDOW_SECONDS
    if timestamp >= recent_cutoff:
        stats_history_cache["recent"].append(entry)


def prune_recent_stats_cache(now=None):
    cutoff = (now or time.time()) - STATS_CPU_SUSTAINED_WINDOW_SECONDS
    recent = stats_history_cache["recent"]
    while recent:
        try:
            timestamp = float(recent[0].get("t", 0))
        except (AttributeError, TypeError, ValueError):
            recent.popleft()
            continue
        if timestamp >= cutoff:
            break
        recent.popleft()


def ensure_stats_history_cache():
    with stats_history_lock:
        if stats_history_cache["initialized"]:
            prune_recent_stats_cache()
            return

        stats_history_cache["peaks"] = {}
        stats_history_cache["recent"] = deque()
        recent_cutoff = time.time() - STATS_CPU_SUSTAINED_WINDOW_SECONDS
        if STATS_FILE.exists():
            with STATS_FILE.open() as history_file:
                for line in history_file:
                    try:
                        entry = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    add_stats_entry_to_cache(entry, recent_cutoff)
        stats_history_cache["initialized"] = True


def get_stats_history_snapshot():
    ensure_stats_history_cache()
    with stats_history_lock:
        prune_recent_stats_cache()
        return (
            {name: dict(peak) for name, peak in stats_history_cache["peaks"].items()},
            list(stats_history_cache["recent"]),
        )


def reset_stats_history_cache():
    stats_history_cache["initialized"] = False
    stats_history_cache["peaks"] = {}
    stats_history_cache["recent"] = deque()


def trim_stats_file():
    with stats_history_lock:
        if not STATS_FILE.exists():
            return

        cutoff = time.time() - STATS_TRIM_DAYS * 86400
        temporary_file = STATS_FILE.with_name(f".{STATS_FILE.name}.tmp")
        try:
            with STATS_FILE.open() as source, temporary_file.open("w") as destination:
                for line in source:
                    try:
                        entry = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("t", 0) >= cutoff:
                        destination.write(json.dumps(entry) + "\n")
            temporary_file.replace(STATS_FILE)
        finally:
            temporary_file.unlink(missing_ok=True)

        reset_stats_history_cache()


def trim_stats_file_if_needed():
    if get_stats_file_size_mb() >= STATS_MAX_SIZE_MB:
        trim_stats_file()


def get_stats_file_size_mb():
    try:
        return STATS_FILE.stat().st_size / 1_000_000
    except FileNotFoundError:
        return 0.0


def get_container_restart_counts():
    ids_result = run_docker_capture(["docker", "ps", "-a", "-q"], check=False)
    container_ids = sorted(output_line_set(ids_result.stdout))
    if not container_ids:
        return {}

    result = run_docker_capture([
        "docker",
        "inspect",
        "--format",
        "{{.Name}}\t{{.RestartCount}}",
        *container_ids,
    ], check=False)
    counts = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        name, restart_count = parts
        try:
            counts[name.strip().lstrip("/")] = int(restart_count)
        except ValueError:
            counts[name.strip().lstrip("/")] = 0
    return counts


def get_container_uptimes():
    result = run_docker_capture(
        ["docker", "ps", "--format", "{{.Names}}\t{{.RunningFor}}"],
        check=False,
    )
    uptimes = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            uptimes[parts[0].strip()] = parts[1].strip()
    return uptimes


def get_recent_cpu_streak_counts(history, current_samples):
    samples_by_name = {}
    for sample in (*history, *current_samples):
        try:
            name = sample["name"]
            timestamp = float(sample["t"])
            cpu = float(sample["cpu"])
        except (KeyError, TypeError, ValueError):
            continue
        samples_by_name.setdefault(name, {})[timestamp] = cpu

    warning_counts = {}
    critical_counts = {}
    for name, samples_by_time in samples_by_name.items():
        warning_count = 0
        critical_count = 0
        warning_streak_active = True
        critical_streak_active = True
        for _timestamp, cpu in sorted(samples_by_time.items(), reverse=True):
            if warning_streak_active and cpu >= STATS_CPU_WARNING_PCT:
                warning_count += 1
            else:
                warning_streak_active = False
            if critical_streak_active and cpu >= STATS_CPU_CRITICAL_PCT:
                critical_count += 1
            else:
                critical_streak_active = False
            if not warning_streak_active and not critical_streak_active:
                break
        warning_counts[name] = warning_count
        critical_counts[name] = critical_count
    return warning_counts, critical_counts


def build_stats_summary(current_samples=None):
    peaks, recent_history = get_stats_history_snapshot()
    if current_samples is None:
        current_samples = collect_stats_sample()
    restarts = get_container_restart_counts()
    uptimes = get_container_uptimes()
    recent_warning_counts, recent_critical_counts = get_recent_cpu_streak_counts(
        recent_history,
        current_samples,
    )

    summary = []
    for s in current_samples:
        name = s["name"]
        p = peaks.get(name, {})
        recent_warning_count = recent_warning_counts.get(name, 0)
        recent_critical_count = recent_critical_counts.get(name, 0)
        peak_cpu = max(p.get("cpu", 0.0), s["cpu"])
        peak_cpu_ts = s["t"] if peak_cpu == s["cpu"] else p.get("cpu_ts", 0)
        peak_mem = max(p.get("mem", 0), s["mem"])
        peak_mem_ts = s["t"] if peak_mem == s["mem"] else p.get("mem_ts", 0)
        summary.append({
            "name": name,
            "cpu": s["cpu"],
            "mem": s["mem"],
            "mem_str": s["mem_str"],
            "uptime": uptimes.get(name, ""),
            "restarts": restarts.get(name, 0),
            "peak_cpu": peak_cpu,
            "peak_cpu_ts": peak_cpu_ts,
            "peak_mem": peak_mem,
            "peak_mem_ts": peak_mem_ts,
            "recent_cpu_warning_count": recent_warning_count,
            "recent_cpu_critical_count": recent_critical_count,
        })

    system_mem_total = 0
    for s in current_samples:
        if "/" in s["mem_str"]:
            total_part = s["mem_str"].split("/")[1].strip()
            parsed = parse_mem_bytes(total_part)
            if parsed > system_mem_total:
                system_mem_total = parsed

    summary.sort(key=lambda x: x["name"].lower())
    return summary, system_mem_total


def compute_health(summary, system_mem_total):
    issues = []
    level = "ok"

    total_cpu = sum(s["cpu"] for s in summary)
    total_mem = sum(s["mem"] for s in summary)
    mem_pct = (total_mem / system_mem_total * 100) if system_mem_total else 0

    for s in summary:
        if s.get("recent_cpu_critical_count", 0) >= STATS_CPU_SUSTAINED_SAMPLES:
            level = "critical"
            issues.append(f"⚠ {s['name']}: sustained CPU {s['cpu']:.1f}%")
        elif s.get("recent_cpu_warning_count", 0) >= STATS_CPU_SUSTAINED_SAMPLES and level == "ok":
            level = "warning"
            issues.append(f"↑ {s['name']}: sustained CPU {s['cpu']:.1f}%")

    if mem_pct >= 85:
        if level != "critical":
            level = "critical"
        issues.append(f"⚠ RAM: {format_bytes(total_mem)} / {format_bytes(system_mem_total)} ({mem_pct:.0f}%)")
    elif mem_pct >= 70 and level == "ok":
        level = "warning"
        issues.append(f"↑ RAM: {format_bytes(total_mem)} / {format_bytes(system_mem_total)} ({mem_pct:.0f}%)")

    return level, total_cpu, total_mem, system_mem_total, mem_pct, issues


def set_container_health_level(icon, level):
    if level != container_health_state["level"]:
        container_health_state["level"] = level
        update_tray_menu(icon)


def poll_container_stats_once(icon):
    try:
        samples = collect_stats_sample()
        if not samples:
            set_container_health_level(icon, "idle")
            return

        summary, system_mem_total = build_stats_summary(samples)
        level, *_ = compute_health(summary, system_mem_total)
        append_stats_to_file(samples)
        trim_stats_file_if_needed()
        set_container_health_level(icon, level)
    except Exception:
        set_container_health_level(icon, "unknown")


def poll_container_stats(icon):
    while True:
        poll_container_stats_once(icon)
        time.sleep(STATS_POLL_INTERVAL_SECONDS)


def clear_container_stats_dialog():
    container_stats_dialog["window"] = None
    container_stats_dialog["content"] = None
    return GLib.SOURCE_REMOVE


def destroy_container_stats_dialog():
    window = container_stats_dialog["window"]
    if window is not None:
        window.destroy()
    clear_container_stats_dialog()
    return GLib.SOURCE_REMOVE


def ensure_container_stats_dialog():
    if container_stats_dialog["window"] is not None:
        container_stats_dialog["window"].present()
        return container_stats_dialog["window"]
    window = Gtk.Window(title="Container Stats")
    window.set_default_size(780, 600)
    window.set_resizable(True)
    window.set_keep_above(True)
    window.set_skip_taskbar_hint(True)
    window.set_position(Gtk.WindowPosition.CENTER)
    window.connect("destroy", lambda w: clear_container_stats_dialog())
    container_stats_dialog["window"] = window
    return window


def set_container_stats_content(content):
    window = container_stats_dialog["window"]
    old = container_stats_dialog["content"]
    if window is None:
        return
    if old is not None:
        window.remove(old)
    container_stats_dialog["content"] = content
    window.add(content)
    window.show_all()
    window.present()


def show_container_stats_loading():
    ensure_container_stats_dialog()
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.set_border_width(16)
    spinner = Gtk.Spinner()
    spinner.start()
    label = Gtk.Label(label="Loading container stats...")
    label.set_xalign(0)
    box.pack_start(spinner, False, False, 0)
    box.pack_start(label, True, True, 0)
    set_container_stats_content(box)
    return GLib.SOURCE_REMOVE


def make_stats_header_label(text):
    label = Gtk.Label()
    label.set_markup(f"<b>{text}</b>")
    label.set_xalign(1)
    label.set_margin_start(12)
    return label


def make_stats_cell(text, xalign=1, warning=False, dim=False):
    label = Gtk.Label()
    if warning:
        label.set_markup(f"<span foreground='orange'>{text}</span>")
    else:
        label.set_text(text)
    label.set_xalign(xalign)
    label.set_margin_start(12)
    if dim:
        label.get_style_context().add_class("dim-label")
    return label


def show_container_stats(summary, system_mem_total, error):
    ensure_container_stats_dialog()
    box = make_dialog_box()

    if error:
        label = Gtk.Label(label=f"Failed to load stats: {error}")
        label.set_xalign(0)
        label.set_line_wrap(True)
        box.pack_start(label, False, False, 0)
    elif not summary:
        label = Gtk.Label(label="No running containers found.")
        label.set_xalign(0)
        box.pack_start(label, False, False, 0)
    else:
        level, total_cpu, total_mem, sys_mem, mem_pct, issues = compute_health(
            summary,
            system_mem_total,
        )

        badge = {"ok": "🟢 Healthy", "warning": "🟡 Watch", "critical": "🔴 Action needed"}[level]
        color = {"ok": "green", "warning": "orange", "critical": "red"}[level]

        health_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        badge_label = Gtk.Label()
        badge_label.set_markup(f"<b><span foreground='{color}'>{badge}</span></b>")
        badge_label.set_xalign(0)
        health_row.pack_start(badge_label, False, False, 0)

        totals_label = Gtk.Label(label=(
            f"{len(summary)} containers  ·  "
            f"CPU total: {total_cpu:.1f}%  ·  "
            f"RAM: {format_bytes(total_mem)} / {format_bytes(sys_mem)} ({mem_pct:.0f}%)"
        ))
        totals_label.set_xalign(0)
        totals_label.get_style_context().add_class("dim-label")
        health_row.pack_start(totals_label, True, True, 0)
        box.pack_start(health_row, False, False, 0)

        if issues:
            for issue in issues:
                issue_label = Gtk.Label(label=issue)
                issue_label.set_xalign(0)
                box.pack_start(issue_label, False, False, 0)

        size_mb = get_stats_file_size_mb()
        if size_mb >= STATS_MAX_SIZE_MB:
            size_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            size_label = Gtk.Label(label=f"History log is {size_mb:.0f} MB — trim to last {STATS_TRIM_DAYS} days?")
            size_label.set_xalign(0)
            size_label.set_hexpand(True)
            trim_button = Gtk.Button(label="Trim")
            trim_button.connect("clicked", lambda b: (trim_stats_file(), start_container_stats_load()))
            size_row.pack_start(size_label, True, True, 0)
            size_row.pack_start(trim_button, False, False, 0)
            box.pack_start(size_row, False, False, 0)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        box.pack_start(sep, False, False, 4)


        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(450)

        grid = Gtk.Grid()
        grid.set_row_spacing(6)
        grid.set_border_width(4)

        headers = ["Container", "CPU", "Peak CPU", "RAM", "Peak RAM", "Restarts", "Uptime"]
        for col, text in enumerate(headers):
            lbl = make_stats_header_label(text)
            if col == 0:
                lbl.set_xalign(0)
            grid.attach(lbl, col, 0, 1, 1)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        grid.attach(sep, 0, 1, len(headers), 1)

        for row_idx, s in enumerate(summary):
            row = row_idx + 2
            has_spike = s["peak_cpu"] >= STATS_CPU_CRITICAL_PCT
            has_restarts = s["restarts"] > 0

            name_text = f"⚠ {s['name']}" if (has_spike or has_restarts) else s["name"]
            name_lbl = make_stats_cell(name_text, xalign=0, warning=(has_spike or has_restarts))
            name_lbl.set_hexpand(True)
            grid.attach(name_lbl, 0, row, 1, 1)

            grid.attach(make_stats_cell(f"{s['cpu']:.1f}%"), 1, row, 1, 1)

            peak_cpu_ts = f" ({format_ts(s['peak_cpu_ts'])})" if s["peak_cpu_ts"] else ""
            peak_cpu_text = f"{s['peak_cpu']:.1f}%{peak_cpu_ts}"
            grid.attach(make_stats_cell(peak_cpu_text, warning=has_spike, dim=bool(peak_cpu_ts)), 2, row, 1, 1)

            mem_used = s["mem_str"].split("/")[0].strip() if "/" in s["mem_str"] else s["mem_str"]
            grid.attach(make_stats_cell(mem_used), 3, row, 1, 1)

            peak_mem_ts = f" ({format_ts(s['peak_mem_ts'])})" if s["peak_mem_ts"] else ""
            peak_mem_text = f"{format_bytes(s['peak_mem'])}{peak_mem_ts}"
            grid.attach(make_stats_cell(peak_mem_text, dim=bool(peak_mem_ts)), 4, row, 1, 1)

            restarts_text = str(s["restarts"]) if s["restarts"] > 0 else "-"
            grid.attach(make_stats_cell(restarts_text, warning=has_restarts), 5, row, 1, 1)

            grid.attach(make_stats_cell(s["uptime"], dim=True), 6, row, 1, 1)

        scroller.add(grid)
        box.pack_start(scroller, True, True, 0)

    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    buttons.set_halign(Gtk.Align.END)

    refresh_button = Gtk.Button(label="Refresh")
    refresh_button.connect("clicked", lambda b: start_container_stats_load())
    buttons.pack_start(refresh_button, False, False, 0)

    close_button = Gtk.Button(label="Close")
    close_button.connect("clicked", lambda b: destroy_container_stats_dialog())
    buttons.pack_start(close_button, False, False, 0)

    box.pack_start(buttons, False, False, 0)
    set_container_stats_content(box)
    return GLib.SOURCE_REMOVE


def start_container_stats_load():
    GLib.idle_add(show_container_stats_loading)

    def _load():
        try:
            summary, system_mem_total = build_stats_summary()
            error = None
        except Exception as e:
            summary, system_mem_total = [], 0
            error = f"{type(e).__name__}: {e}"
        GLib.idle_add(show_container_stats, summary, system_mem_total, error)

    threading.Thread(target=_load, daemon=True).start()


def open_container_stats_dialog(icon, item):
    ensure_container_stats_dialog()
    start_container_stats_load()


def start_menu_polling(icon):
    icon.visible = True
    watch_theme(icon)
    threading.Thread(target=watch_container_status, args=(icon,), daemon=True).start()
    threading.Thread(target=poll_updates, args=(icon,), daemon=True).start()
    threading.Thread(target=poll_container_stats, args=(icon,), daemon=True).start()


def watch_container_status(icon):
    helper = "/usr/lib/docker-tray/docker_tray_privileged.py"
    while getattr(icon, "_running", True):
        try:
            process = subprocess.Popen(
                ["pkexec", helper, "watch"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            for line in process.stdout:
                try:
                    containers, error = parse_container_watch_message(line)
                except (TypeError, ValueError, json.JSONDecodeError) as parse_error:
                    containers, error = (), f"Invalid status from Docker Tray helper: {parse_error}"
                if set_container_watch_state(containers, error):
                    update_tray_menu(icon)
                if not getattr(icon, "_running", True):
                    process.terminate()
                    return
            detail = process.stderr.read().strip()
            if process.wait() != 0 and not detail:
                detail = "Docker Tray status helper stopped"
            if detail and set_container_watch_state(error=detail[:300]):
                update_tray_menu(icon)
        except Exception as error:
            if set_container_watch_state(error=f"Docker Tray status helper failed: {error}"):
                update_tray_menu(icon)
        time.sleep(MENU_REFRESH_SECONDS)


def check_engine_update():
    return docker_tray_platform.check_engine_update(DOCKER_CMD_TIMEOUT_SECONDS, PLATFORM_INFO)


def get_local_image_id(image):
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True, text=True,
        timeout=DOCKER_CMD_TIMEOUT_SECONDS,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def get_container_image_refs():
    ids_result = run_docker_capture(["docker", "ps", "-a", "-q"], check=False)
    container_ids = sorted(output_line_set(ids_result.stdout))
    if not container_ids:
        return []

    result = run_docker_capture([
        "docker",
        "inspect",
        "--format",
        "{{.Config.Image}}\t{{.Image}}",
        *container_ids,
    ], check=False)
    refs = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        image, container_image_id = parts
        image = image.strip()
        container_image_id = container_image_id.strip()
        if image:
            refs.append((image, container_image_id))
    return refs


def get_remote_config_digest(image):
    result = subprocess.run(
        ["docker", "manifest", "inspect", "--verbose", image],
        capture_output=True, text=True,
        timeout=DOCKER_MANIFEST_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except Exception:
        return None
    arch = docker_tray_platform.linux_package_arch()
    if isinstance(data, list):
        for entry in data:
            p = entry.get("Descriptor", {}).get("platform", {})
            if p.get("architecture") == arch and p.get("os") == "linux":
                return get_manifest_config_digest(entry)
        return None
    return get_manifest_config_digest(data)


def get_cached_remote_config_digest(image, now=None):
    now = time.monotonic() if now is None else now
    with remote_digest_cache_lock:
        cached = remote_digest_cache.get(image)
        if cached is not None and cached[0] > now:
            return cached[1]

    digest = get_remote_config_digest(image)
    ttl = REMOTE_DIGEST_CACHE_SECONDS if digest else REMOTE_DIGEST_FAILURE_CACHE_SECONDS
    with remote_digest_cache_lock:
        remote_digest_cache[image] = (now + ttl, digest)
    return digest


def get_manifest_config_digest(manifest):
    for key in ("SchemaV2Manifest", "OCIManifest"):
        digest = manifest.get(key, {}).get("config", {}).get("digest")
        if digest:
            return digest
    return manifest.get("config", {}).get("digest")


def is_checkable_image(image):
    if "@" in image or is_image_id_reference(image):
        return False
    return True


def is_image_id_reference(image):
    image = image.strip()
    if image.startswith("sha256:"):
        image = image.removeprefix("sha256:")
    return bool(re.fullmatch(r"[0-9a-fA-F]{12,64}", image))


def parse_app_version(version):
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", version.strip())
    if not match:
        raise ValueError(f"Unsupported version: {version}")
    return tuple(int(part or 0) for part in match.groups())


def check_app_update():
    request = Request(
        APP_LATEST_RELEASE_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"docker-tray/{APP_VERSION}",
        },
    )
    with urlopen(request, timeout=APP_UPDATE_TIMEOUT_SECONDS) as response:
        release = json.loads(response.read())

    tag_name = release.get("tag_name", "")
    latest_version = tag_name.removeprefix("v")
    if parse_app_version(latest_version) <= parse_app_version(APP_VERSION):
        return AppUpdate(False)

    release_url = release.get("html_url", "")
    if not release_url.startswith(f"{APP_RELEASES_URL}/"):
        release_url = f"{APP_RELEASES_URL}/tag/{tag_name}"

    package_url = ""
    package_digest = ""
    expected_package_name = f"docker-tray_{latest_version}_all.deb"
    for asset in release.get("assets") or []:
        if asset.get("name") != expected_package_name:
            continue
        candidate_url = asset.get("browser_download_url", "")
        if not candidate_url.startswith(APP_RELEASE_DOWNLOAD_URL_PREFIX):
            continue
        candidate_digest = asset.get("digest") or ""
        if candidate_digest and not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", candidate_digest):
            continue
        package_url = candidate_url
        package_digest = candidate_digest.lower()
        break
    return AppUpdate(
        True,
        latest_version=latest_version,
        release_url=release_url,
        package_url=package_url,
        package_digest=package_digest,
    )


def download_app_update(update, destination_dir):
    if not update.package_url.startswith(APP_RELEASE_DOWNLOAD_URL_PREFIX):
        raise RuntimeError("The release package URL is not trusted")

    package_name = f"docker-tray_{update.latest_version}_all.deb"
    package_path = Path(destination_dir) / package_name
    request = Request(
        update.package_url,
        headers={"User-Agent": f"docker-tray/{APP_VERSION}"},
    )
    digest = hashlib.sha256()
    with urlopen(request, timeout=APP_UPGRADE_TIMEOUT_SECONDS) as response:
        with package_path.open("wb") as package_file:
            while chunk := response.read(64 * 1024):
                package_file.write(chunk)
                digest.update(chunk)
    package_path.chmod(0o644)

    actual_digest = f"sha256:{digest.hexdigest()}"
    if update.package_digest and actual_digest != update.package_digest:
        package_path.unlink(missing_ok=True)
        raise RuntimeError("The downloaded package checksum does not match the release")
    validate_app_update_package(package_path, update.latest_version)
    return package_path


def validate_app_update_package(package_path, expected_version):
    result = subprocess.run(
        ["dpkg-deb", "--field", str(package_path), "Package", "Version", "Architecture"],
        capture_output=True,
        text=True,
        timeout=APP_UPDATE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "invalid Debian package"
        raise RuntimeError(detail)
    fields = {}
    for line in result.stdout.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    if fields != {
        "Package": "docker-tray",
        "Version": expected_version,
        "Architecture": "all",
    }:
        raise RuntimeError("The downloaded package metadata does not match the release")


def run_app_upgrade(update):
    if not update.can_install:
        raise RuntimeError("Automatic Docker Tray upgrades are not available on this platform")
    with tempfile.TemporaryDirectory(prefix="docker-tray-update-") as temp_dir:
        Path(temp_dir).chmod(0o755)
        package_path = download_app_update(update, temp_dir)
        return subprocess.run(
            [
                "pkexec",
                PRIVILEGED_HELPER,
                "install-update",
                str(package_path),
                update.latest_version,
                update.package_digest,
            ],
            capture_output=True,
            text=True,
            timeout=APP_UPGRADE_TIMEOUT_SECONDS,
        )


def check_image_updates():
    container_refs = get_container_image_refs()
    images = sorted({
        image for image, _container_image_id in container_refs
        if is_checkable_image(image)
    })
    local_ids = {image: get_local_image_id(image) for image in images}
    stale_running_images = {
        image for image, container_image_id in container_refs
        if is_checkable_image(image)
        and container_image_id
        and (local := local_ids.get(image))
        and local != container_image_id
    }
    remote_ids = {}
    if images:
        worker_count = min(REMOTE_DIGEST_WORKERS, len(images))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            remote_ids = dict(zip(images, executor.map(get_cached_remote_config_digest, images)))
    remote_updates = {
        image for image in images
        if (local := local_ids.get(image))
        and (remote := remote_ids.get(image))
        and local != remote
    }
    return sorted(remote_updates | stale_running_images)


def get_update_state_snapshot():
    with update_check_lock:
        return (
            update_check_state["engine_update"],
            list(update_check_state["image_updates"]),
        )


def get_app_update_snapshot():
    with update_check_lock:
        return update_check_state["app_update"]


def set_update_state(icon, engine_update, image_updates, app_update=None):
    with update_check_lock:
        if app_update is None:
            app_update = update_check_state["app_update"]
        app_update_became_available = (
            app_update.available
            and app_update != update_check_state["app_update"]
        )
        changed = (
            app_update != update_check_state["app_update"]
            or engine_update != update_check_state["engine_update"]
            or image_updates != update_check_state["image_updates"]
        )
        update_check_state["app_update"] = app_update
        update_check_state["engine_update"] = engine_update
        update_check_state["image_updates"] = image_updates
    if changed:
        update_tray_menu(icon)
        if updates_dialog["window"] is not None:
            show_updates_dialog(icon)
    if app_update_became_available:
        notify_user(icon, f"Docker Tray {app_update.latest_version} is available.")
    return GLib.SOURCE_REMOVE


def run_update_check(icon):
    if not update_check_run_lock.acquire(blocking=False):
        return
    previous_engine_update, previous_image_updates = get_update_state_snapshot()
    previous_app_update = get_app_update_snapshot()
    try:
        try:
            app_update = check_app_update()
        except Exception:
            app_update = previous_app_update
        try:
            engine_update = check_engine_update()
        except Exception:
            engine_update = previous_engine_update
        try:
            image_updates = check_image_updates()
        except Exception:
            image_updates = previous_image_updates
        GLib.idle_add(set_update_state, icon, engine_update, image_updates, app_update)
    finally:
        update_check_run_lock.release()


def poll_updates(icon):
    run_update_check(icon)
    while getattr(icon, "_running", True):
        time.sleep(UPDATE_CHECK_INTERVAL_SECONDS)
        run_update_check(icon)


def clear_updates_dialog():
    updates_dialog["window"] = None
    updates_dialog["content"] = None
    if (
        not updates_dialog["app_upgrading"]
        and not updates_dialog["engine_upgrading"]
        and not updates_dialog["pulling_images"]
    ):
        updates_dialog["status"] = ""
    return GLib.SOURCE_REMOVE


def destroy_updates_dialog():
    window = updates_dialog["window"]
    if window is not None:
        window.destroy()
    clear_updates_dialog()
    return GLib.SOURCE_REMOVE


def ensure_updates_dialog():
    if updates_dialog["window"] is not None:
        updates_dialog["window"].present()
        return updates_dialog["window"]
    window = Gtk.Window(title="Docker Updates")
    window.set_default_size(400, 200)
    window.set_resizable(True)
    window.set_keep_above(True)
    window.set_skip_taskbar_hint(True)
    window.set_position(Gtk.WindowPosition.CENTER)
    window.connect("destroy", lambda w: clear_updates_dialog())
    updates_dialog["window"] = window
    return window


def set_updates_dialog_content(content):
    window = updates_dialog["window"]
    old = updates_dialog["content"]
    if window is None:
        return
    if old is not None:
        window.remove(old)
    updates_dialog["content"] = content
    window.add(content)
    window.show_all()
    window.present()


def start_app_upgrade(icon):
    if (
        updates_dialog["app_upgrading"]
        or updates_dialog["engine_upgrading"]
        or updates_dialog["pulling_images"]
    ):
        return
    app_update = get_app_update_snapshot()
    if not app_update.can_install:
        return
    updates_dialog["app_upgrading"] = True
    updates_dialog["status"] = f"Downloading Docker Tray {app_update.latest_version}..."
    show_updates_dialog(icon)

    def _upgrade():
        try:
            result = run_app_upgrade(app_update)
        except Exception as error:
            GLib.idle_add(finish_app_upgrade, icon, None, error)
            return
        GLib.idle_add(finish_app_upgrade, icon, result, None)

    threading.Thread(target=_upgrade, daemon=True).start()


def finish_app_upgrade(icon, result, error):
    updates_dialog["app_upgrading"] = False
    if error is not None:
        if isinstance(error, subprocess.TimeoutExpired):
            detail = "timed out"
        else:
            detail = f"{type(error).__name__}: {error}"
        updates_dialog["status"] = f"Docker Tray upgrade failed: {detail}"
        if updates_dialog["window"] is not None:
            show_updates_dialog(icon)
        return GLib.SOURCE_REMOVE

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "upgrade command failed"
        updates_dialog["status"] = f"Docker Tray upgrade failed: {detail}"
        if updates_dialog["window"] is not None:
            show_updates_dialog(icon)
        return GLib.SOURCE_REMOVE

    updates_dialog["status"] = "Docker Tray upgraded. Restarting..."
    icon._restart_after_upgrade = True
    icon.stop()
    return GLib.SOURCE_REMOVE


def start_docker_engine_upgrade(icon):
    if (
        updates_dialog["app_upgrading"]
        or updates_dialog["engine_upgrading"]
        or updates_dialog["pulling_images"]
    ):
        return
    updates_dialog["engine_upgrading"] = True
    updates_dialog["status"] = "Upgrading Docker Engine..."
    show_updates_dialog(icon)

    def _upgrade():
        engine_update, _image_updates = get_update_state_snapshot()
        try:
            result = docker_tray_platform.run_engine_upgrade(
                engine_update,
                timeout=DOCKER_ENGINE_UPGRADE_TIMEOUT_SECONDS,
            )
        except Exception as error:
            GLib.idle_add(finish_docker_engine_upgrade, icon, None, error)
            return
        GLib.idle_add(finish_docker_engine_upgrade, icon, result, None)

    threading.Thread(target=_upgrade, daemon=True).start()


def finish_docker_engine_upgrade(icon, result, error):
    updates_dialog["engine_upgrading"] = False
    if error is not None:
        if isinstance(error, subprocess.TimeoutExpired):
            detail = "timed out"
        else:
            detail = f"{type(error).__name__}: {error}"
        updates_dialog["status"] = f"Docker Engine upgrade failed: {detail}"
        if updates_dialog["window"] is not None:
            show_updates_dialog(icon)
        return GLib.SOURCE_REMOVE

    if result.returncode == 0:
        updates_dialog["status"] = ""
        _current_engine_update, image_updates = get_update_state_snapshot()
        set_update_state(icon, docker_tray_platform.EngineUpdate(False), image_updates)
        destroy_updates_dialog()
        threading.Thread(target=run_update_check, args=(icon,), daemon=True).start()
    else:
        detail = result.stderr.strip() or result.stdout.strip() or "upgrade command failed"
        updates_dialog["status"] = f"Docker Engine upgrade failed: {detail}"
        if updates_dialog["window"] is not None:
            show_updates_dialog(icon)
    return GLib.SOURCE_REMOVE


def finish_image_pull(
    icon,
    image,
    service_count,
    removed_image_count=0,
    cleanup_error="",
    start_recheck=True,
):
    updates_dialog["pulling_images"].discard(image)
    engine_update, image_updates = get_update_state_snapshot()
    image_updates = [update_image for update_image in image_updates if update_image != image]
    set_update_state(icon, engine_update, image_updates)
    status = f"Finished pulling and restarting {image} for {service_count} compose service(s)."
    if removed_image_count:
        noun = "image" if removed_image_count == 1 else "images"
        status += f" Removed {removed_image_count} replaced {noun}."
    if cleanup_error:
        status += f" Replaced image cleanup failed: {cleanup_error}"
    updates_dialog["status"] = status
    if updates_dialog["window"] is not None:
        show_updates_dialog(icon)
    if start_recheck:
        threading.Thread(target=run_update_check, args=(icon,), daemon=True).start()
    return GLib.SOURCE_REMOVE


def set_image_pull_status(icon, status):
    updates_dialog["status"] = status
    if updates_dialog["window"] is not None:
        show_updates_dialog(icon)
    return GLib.SOURCE_REMOVE


def fail_image_pull(icon, image, status):
    updates_dialog["pulling_images"].discard(image)
    updates_dialog["status"] = status
    if updates_dialog["window"] is not None:
        show_updates_dialog(icon)
    return GLib.SOURCE_REMOVE


def schedule_image_pull_failure(icon, image, status, schedule_completion=True):
    if schedule_completion:
        GLib.idle_add(fail_image_pull, icon, image, status)
    return False, status, 0, ""


def run_privileged_image_updates(images):
    images = list(images)
    result = subprocess.run(
        ["pkexec", PRIVILEGED_HELPER, "image-update", *images],
        capture_output=True,
        text=True,
        timeout=DOCKER_IMAGE_UPDATE_TIMEOUT_SECONDS * max(1, len(images)),
    )
    outcomes = {}
    for line in result.stdout.splitlines():
        try:
            message = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        image = message.get("image") if isinstance(message, dict) else None
        if image not in images or not isinstance(message.get("success"), bool):
            continue
        if message["success"]:
            outcomes[image] = {
                "success": True,
                "error": "",
                "service_count": int(message.get("service_count", 0)),
                "removed_image_count": int(message.get("removed_image_count", 0)),
                "cleanup_error": str(message.get("cleanup_error", "")),
            }
        else:
            outcomes[image] = {
                "success": False,
                "error": str(message.get("error", "Image update failed")),
                "service_count": 0,
                "removed_image_count": 0,
                "cleanup_error": "",
            }

    missing_detail = (
        result.stderr.strip()
        or (result.stdout.strip() if not outcomes else "")
        or "The privileged image update helper did not return a result"
    )
    if len(missing_detail) > COMMAND_ERROR_DETAIL_MAX_CHARS:
        missing_detail = missing_detail[:COMMAND_ERROR_DETAIL_MAX_CHARS].rstrip() + "…"
    for image in images:
        outcomes.setdefault(image, {
            "success": False,
            "error": missing_detail,
            "service_count": 0,
            "removed_image_count": 0,
            "cleanup_error": "",
        })
    return outcomes


def run_image_compose_pull_safely(
    icon,
    image,
    start_recheck=True,
    schedule_completion=True,
):
    try:
        return run_image_compose_pull(
            icon,
            image,
            start_recheck=start_recheck,
            schedule_completion=schedule_completion,
        )
    except Exception as error:
        status = f"Unexpected failure while updating {image}: {type(error).__name__}: {error}"
        return schedule_image_pull_failure(
            icon,
            image,
            status,
            schedule_completion=schedule_completion,
        )


def start_image_compose_pull(button, icon, image):
    if (
        updates_dialog["app_upgrading"]
        or updates_dialog["engine_upgrading"]
        or updates_dialog["pulling_images"]
    ):
        return
    updates_dialog["status"] = f"Pulling {image}..."
    updates_dialog["pulling_images"].add(image)
    show_updates_dialog(icon)

    def _pull():
        run_image_compose_pull_safely(icon, image)

    threading.Thread(target=_pull, daemon=True).start()


def finish_all_image_pulls(
    icon,
    images,
    successful_images,
    removed_image_count,
    errors,
    cleanup_errors,
):
    total_count = len(images)
    success_count = len(successful_images)
    updates_dialog["pulling_images"].clear()
    engine_update, image_updates = get_update_state_snapshot()
    successful_images = set(successful_images)
    remaining_updates = [image for image in image_updates if image not in successful_images]
    set_update_state(icon, engine_update, remaining_updates)
    if errors:
        detail = "; ".join(errors)
        if len(detail) > COMMAND_ERROR_DETAIL_MAX_CHARS:
            detail = detail[:COMMAND_ERROR_DETAIL_MAX_CHARS].rstrip() + "…"
        updates_dialog["status"] = (
            f"Batch finished: {success_count} of {total_count} images updated. "
            f"Failures: {detail}"
        )
    else:
        image_noun = "image" if total_count == 1 else "images"
        updates_dialog["status"] = f"Updated and cleaned up all {total_count} {image_noun}."
        if removed_image_count:
            removed_noun = "image" if removed_image_count == 1 else "images"
            updates_dialog["status"] += f" Removed {removed_image_count} replaced {removed_noun}."
    if cleanup_errors:
        cleanup_detail = "; ".join(cleanup_errors)
        if len(cleanup_detail) > COMMAND_ERROR_DETAIL_MAX_CHARS:
            cleanup_detail = cleanup_detail[:COMMAND_ERROR_DETAIL_MAX_CHARS].rstrip() + "…"
        updates_dialog["status"] += f" Cleanup warnings: {cleanup_detail}"
    if updates_dialog["window"] is not None:
        show_updates_dialog(icon)
    threading.Thread(target=run_update_check, args=(icon,), daemon=True).start()
    return GLib.SOURCE_REMOVE


def start_all_image_compose_pulls(icon):
    if (
        updates_dialog["app_upgrading"]
        or updates_dialog["engine_upgrading"]
        or updates_dialog["pulling_images"]
    ):
        return
    _engine_update, image_updates = get_update_state_snapshot()
    if not image_updates:
        return

    images = list(image_updates)
    updates_dialog["pulling_images"].update(images)
    updates_dialog["status"] = f"Updating 1 of {len(images)} images..."
    show_updates_dialog(icon)

    def _pull_all():
        removed_image_count = 0
        errors = []
        cleanup_errors = []
        successful_images = []
        try:
            outcomes = run_privileged_image_updates(images)
        except Exception as error:
            detail = get_command_failure_detail(error=error)
            outcomes = {
                image: {
                    "success": False,
                    "error": detail,
                    "removed_image_count": 0,
                    "cleanup_error": "",
                }
                for image in images
            }
        for index, image in enumerate(images, start=1):
            GLib.idle_add(
                set_image_pull_status,
                icon,
                f"Updating {index} of {len(images)}: {image}...",
            )
            outcome = outcomes[image]
            if outcome["success"]:
                successful_images.append(image)
                removed_image_count += outcome["removed_image_count"]
                if outcome["cleanup_error"]:
                    cleanup_errors.append(f"{image}: {outcome['cleanup_error']}")
            else:
                errors.append(f"{image}: {outcome['error']}")

        GLib.idle_add(
            finish_all_image_pulls,
            icon,
            images,
            successful_images,
            removed_image_count,
            errors,
            cleanup_errors,
        )

    threading.Thread(target=_pull_all, daemon=True).start()


def run_image_compose_pull(
    icon,
    image,
    start_recheck=True,
    schedule_completion=True,
):
    outcome = run_privileged_image_updates([image])[image]
    if not outcome["success"]:
        return schedule_image_pull_failure(
            icon,
            image,
            f"Update failed for {image}: {outcome['error']}",
            schedule_completion=schedule_completion,
        )

    if schedule_completion:
        GLib.idle_add(
            finish_image_pull,
            icon,
            image,
            outcome["service_count"],
            outcome["removed_image_count"],
            outcome["cleanup_error"],
            start_recheck,
        )
    return (
        True,
        "",
        outcome["removed_image_count"],
        outcome["cleanup_error"],
    )


def show_updates_dialog(icon):
    ensure_updates_dialog()
    box = make_dialog_box()

    app_update = get_app_update_snapshot()
    engine_update, image_updates = get_update_state_snapshot()
    update_action_running = (
        updates_dialog["app_upgrading"]
        or updates_dialog["engine_upgrading"]
        or bool(updates_dialog["pulling_images"])
    )
    if app_update.available:
        app_label = Gtk.Label(label="Docker Tray update available")
        app_label.set_xalign(0)
        box.pack_start(app_label, False, False, 0)

        version_label = Gtk.Label(label=f"{APP_VERSION} → {app_update.latest_version}")
        version_label.set_xalign(0)
        box.pack_start(version_label, False, False, 0)

        if app_update.can_install:
            app_upgrading = updates_dialog["app_upgrading"]
            install_button = Gtk.Button(
                label="Installing..." if app_upgrading else "Install update",
            )
            install_button.set_sensitive(not update_action_running)
            if not update_action_running:
                install_button.connect("clicked", lambda button: start_app_upgrade(icon))
            box.pack_start(install_button, False, False, 0)
        else:
            release_button = Gtk.Button(label="Open release")
            release_button.connect(
                "clicked",
                lambda button, url=app_update.release_url: open_uri(url),
            )
            box.pack_start(release_button, False, False, 0)

    if engine_update.available:
        engine_label = Gtk.Label(label=f"{engine_update.package_name} update available")
        engine_label.set_xalign(0)
        box.pack_start(engine_label, False, False, 0)
        if engine_update.detail:
            detail = Gtk.Label(label=engine_update.detail)
            detail.set_xalign(0)
            box.pack_start(detail, False, False, 0)
        if engine_update.can_upgrade:
            engine_upgrading = updates_dialog["engine_upgrading"]
            upgrade_button = Gtk.Button(
                label="Upgrading..." if engine_upgrading else engine_update.upgrade_label,
            )
            upgrade_button.set_sensitive(not update_action_running)
            if not update_action_running:
                upgrade_button.connect("clicked", lambda b: start_docker_engine_upgrade(icon))
            box.pack_start(upgrade_button, False, False, 0)
        else:
            detail = Gtk.Label(label="Use your system package manager to upgrade Docker.")
            detail.set_xalign(0)
            box.pack_start(detail, False, False, 0)

    if image_updates:
        images_label = Gtk.Label(label="Image updates available:")
        images_label.set_xalign(0)
        box.pack_start(images_label, False, False, 0)
        pull_in_progress = bool(updates_dialog["pulling_images"])
        update_all_button = Gtk.Button(
            label="Updating all..." if pull_in_progress else "Update + cleanup all",
        )
        update_all_button.set_sensitive(not update_action_running)
        if not update_action_running:
            update_all_button.connect("clicked", lambda button: start_all_image_compose_pulls(icon))
        box.pack_start(update_all_button, False, False, 0)
        for image in image_updates:
            is_pulling = image in updates_dialog["pulling_images"]
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            image_label = Gtk.Label(label=image)
            image_label.set_xalign(0)
            image_label.set_hexpand(True)
            image_label.set_line_wrap(True)
            pull_button = Gtk.Button(label="Pulling" if is_pulling else "Update + cleanup")
            pull_button.set_sensitive(not update_action_running)
            if not update_action_running:
                pull_button.connect(
                    "clicked",
                    lambda button, pull_image=image: start_image_compose_pull(button, icon, pull_image),
                )
            row.pack_start(image_label, True, True, 0)
            row.pack_start(pull_button, False, False, 0)
            box.pack_start(row, False, False, 0)

    if updates_dialog["status"]:
        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if updates_dialog["status"].startswith(
            (
                "Downloading ",
                "Installing ",
                "Pulling ",
                "Restarting ",
                "Waiting ",
                "Upgrading ",
                "Removing ",
            ),
        ):
            spinner = Gtk.Spinner()
            spinner.start()
            status_row.pack_start(spinner, False, False, 0)
        status_label = Gtk.Label(label=updates_dialog["status"])
        status_label.set_xalign(0)
        status_label.set_line_wrap(True)
        status_row.pack_start(status_label, True, True, 0)
        box.pack_start(status_row, False, False, 0)

    if not app_update.available and not engine_update.available and not image_updates:
        done_label = Gtk.Label(label="No updates pending.")
        done_label.set_xalign(0)
        box.pack_start(done_label, False, False, 0)

    close_button = Gtk.Button(label="Close")
    close_button.connect("clicked", lambda b: destroy_updates_dialog())
    close_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    close_row.set_halign(Gtk.Align.END)
    close_row.pack_start(close_button, False, False, 0)
    add_bottom_button_row(box, close_row)

    set_updates_dialog_content(box)
    return GLib.SOURCE_REMOVE


def open_updates_dialog(icon, item):
    GLib.idle_add(show_updates_dialog, icon)


def get_settings_items(pystray):
    return [
        pystray.MenuItem("Container stats", open_container_stats_dialog),
        pystray.MenuItem("Compose search", make_compose_search_cb()),
        pystray.MenuItem("Cleanup", make_cleanup_cb()),
        pystray.MenuItem(
            get_start_at_boot_label,
            toggle_start_at_boot,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"Version {APP_VERSION}", None, enabled=False),
    ]


def get_menu_items(pystray):
    items = []
    app_update = get_app_update_snapshot()
    engine_update, image_updates = get_update_state_snapshot()
    if container_health_state["level"] == "critical":
        items.append(pystray.MenuItem("🔴 Container issue detected", open_container_stats_dialog))
    elif container_health_state["level"] == "warning":
        items.append(pystray.MenuItem("🟡 Container warning", open_container_stats_dialog))
    elif container_health_state["level"] == "unknown" and is_docker_installed():
        items.append(pystray.MenuItem("⚪ Container stats unavailable", open_container_stats_dialog))
    if app_update.available or engine_update.available or image_updates:
        items.append(pystray.MenuItem("⬆️ Updates available", open_updates_dialog))
    if items:
        items.append(pystray.Menu.SEPARATOR)
    if not is_docker_installed():
        items += [
            pystray.MenuItem("Docker is not installed", None, enabled=False),
            pystray.MenuItem("Download Docker ↗", make_install_docker_cb()),
        ]
    else:
        try:
            for name, running, port in sorted(get_containers(), key=container_sort_key):
                sub = []
                if running and port:
                    sub.append(pystray.MenuItem(
                        "🔗 Open",
                        make_open_cb(port)
                    ))
                if running:
                    sub += [
                        pystray.MenuItem("🔄 Restart", make_restart_cb(name)),
                        pystray.MenuItem("⏹️ Stop", make_stop_cb(name)),
                    ]
                else:
                    sub.append(pystray.MenuItem("▶️ Start", make_start_cb(name)))

                status_marker = "• " if running else "◦ "
                label = (
                    f"{status_marker}{name} :{port}"
                    if port else
                    f"{status_marker}{name}"
                )
                items.append(pystray.MenuItem(label, pystray.Menu(*sub)))
        except Exception as e:
            items.append(pystray.MenuItem(f"Error: {e}", None))

    items += [
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Settings",
            pystray.Menu(lambda: get_settings_items(pystray)),
        ),
        pystray.MenuItem("Quit", lambda icon, item: icon.stop()),
    ]
    return items


def main():
    import pystray

    instance_lock = acquire_instance_lock()
    if instance_lock is None:
        return

    icon = pystray.Icon(
        "docker-tray",
        make_icon(),
        "Docker Monitor",
        menu=pystray.Menu(lambda: get_menu_items(pystray)),
    )
    try:
        icon.run(setup=start_menu_polling)
    finally:
        instance_lock.close()
    if getattr(icon, "_restart_after_upgrade", False):
        os.execvp("docker-tray", ["docker-tray"])


if __name__ == "__main__":
    main()

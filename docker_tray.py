#!/usr/bin/env python3
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gio

from PIL import Image


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
AUTOSTART_DESKTOP_FILE = Path.home() / ".config" / "autostart" / "docker-tray.desktop"
AUTOSTART_ENABLED_PREFIX = "X-GNOME-Autostart-enabled="
DOCKER_INSTALL_URL = "https://docs.docker.com/engine/install/ubuntu/"
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


ICON_DIR = Path(__file__).parent


def is_dark_mode():
    try:
        settings = Gio.Settings.new("org.gnome.desktop.interface")
        return settings.get_string("color-scheme") == "prefer-dark"
    except Exception:
        return True


def make_icon():
    name = "icon-dark.png" if is_dark_mode() else "icon-light.png"
    return Image.open(ICON_DIR / name).convert("RGBA")


def watch_theme(icon):
    settings = Gio.Settings.new("org.gnome.desktop.interface")
    def on_changed(settings, key):
        if key == "color-scheme":
            icon.icon = make_icon()
    settings.connect("changed", on_changed)


def get_containers():
    result = subprocess.run(
        ["docker", "ps", "-a", "--format", DOCKER_PS_FORMAT],
        capture_output=True,
        text=True,
        check=True,
    )
    return parse_containers(result.stdout)


def is_docker_installed():
    return shutil.which("docker") is not None


def get_container_snapshot():
    try:
        return tuple(sorted(get_containers(), key=container_sort_key))
    except Exception as e:
        return ((type(e).__name__, str(e)),)


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


def run_docker_action(action, name):
    threading.Thread(
        target=subprocess.run,
        args=(["docker", action, name],),
        daemon=True,
    ).start()


def run_compose_up(compose_file):
    threading.Thread(
        target=subprocess.run,
        args=(["docker", "compose", "-f", str(compose_file), "up", "-d"],),
        daemon=True,
    ).start()


def run_docker_capture(args, check=True):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=check,
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


def update_tray_menu(icon):
    try:
        icon.update_menu()
    except Exception:
        pass


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
    commands = [
        ["docker", "container", "prune", "-f"],
        ["docker", "image", "prune", "-f"],
        ["docker", "network", "prune", "-f"],
        ["docker", "builder", "prune", "-f"],
    ]
    output = []
    for command in commands:
        result = run_docker_capture(command)
        command_text = " ".join(command)
        combined_output = "\n".join(
            text.strip()
            for text in (result.stdout, result.stderr)
            if text.strip()
        )
        if combined_output:
            output.append(f"$ {command_text}\n{combined_output}")
        else:
            output.append(f"$ {command_text}\nDone")
    return "\n\n".join(output)


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
        ("srv", Path("/srv")),
        ("opt", Path("/opt")),
        ("etc", Path("/etc")),
        ("Whole system", Path("/")),
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

    GLib.idle_add(set_compose_dialog_action_failed, action)


def run_compose_file_from_dialog(compose_file, icon, button):
    run_compose_up(compose_file)
    button.set_label("Starting")
    button.set_sensitive(False)
    update_tray_menu(icon)
    row = button.get_parent()
    threading.Thread(
        target=poll_compose_start_state,
        args=(compose_file, icon, row, button),
        daemon=True,
    ).start()


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
    try:
        for line in AUTOSTART_DESKTOP_FILE.read_text().splitlines():
            if line.startswith(AUTOSTART_ENABLED_PREFIX):
                return line.split("=", 1)[1].strip().lower() == "true"
    except Exception:
        return False
    return False


def build_autostart_desktop(enabled):
    script_path = Path(__file__).resolve()
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=Docker Tray",
        f'Exec=python3 "{script_path}"',
        "Icon=docker",
        "Comment=Docker container monitor in the system tray",
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
        updated = False
        for index, line in enumerate(lines):
            if line.startswith(AUTOSTART_ENABLED_PREFIX):
                lines[index] = f"{AUTOSTART_ENABLED_PREFIX}{str(enabled).lower()}"
                updated = True
                break
        if not updated:
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
    open_uri(DOCKER_INSTALL_URL)


def make_open_cb(port):
    return lambda icon, item: open_url(port)


def make_install_docker_cb():
    return lambda icon, item: open_docker_install()


def make_compose_search_cb():
    return lambda icon, item: GLib.idle_add(open_compose_scan_dialog, icon)


def make_cleanup_cb():
    return lambda icon, item: GLib.idle_add(open_cleanup_dialog, icon)


def make_start_cb(name):
    return lambda icon, item: run_docker_action("start", name)


def make_restart_cb(name):
    return lambda icon, item: run_docker_action("restart", name)


def make_stop_cb(name):
    return lambda icon, item: run_docker_action("stop", name)


def start_menu_polling(icon):
    icon.visible = True
    watch_theme(icon)
    threading.Thread(target=poll_menu, args=(icon,), daemon=True).start()


def poll_menu(icon):
    last_snapshot = get_container_snapshot()
    while getattr(icon, "_running", True):
        time.sleep(MENU_REFRESH_SECONDS)
        current_snapshot = get_container_snapshot()
        if current_snapshot == last_snapshot:
            continue
        last_snapshot = current_snapshot

        update_tray_menu(icon)


def get_settings_items(pystray):
    return [
        pystray.MenuItem("Compose search", make_compose_search_cb()),
        pystray.MenuItem("Cleanup", make_cleanup_cb()),
        pystray.MenuItem(
            get_start_at_boot_label,
            toggle_start_at_boot,
        ),
    ]


def get_menu_items(pystray):
    items = []
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
            items.append(pystray.MenuItem(f"Error: {type(e).__name__}: {e}", None))

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

    icon = pystray.Icon(
        "docker-tray",
        make_icon(),
        "Docker Monitor",
        menu=pystray.Menu(lambda: get_menu_items(pystray)),
    )
    icon.run(setup=start_menu_polling)


if __name__ == "__main__":
    main()

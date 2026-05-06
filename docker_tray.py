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
from gi.repository import Gtk, GLib

from PIL import Image, ImageDraw


DOCKER_PS_FORMAT = "{{.Names}}\t{{.Status}}\t{{.Ports}}"
HOST_PORT_RE = re.compile(r"(?:0\.0\.0\.0|127\.0\.0\.1):(\d+)->\d+/tcp")
MENU_REFRESH_SECONDS = 5
APP_CONFIG_DIR = Path.home() / ".config" / "docker-tray"
AUTOSTART_DESKTOP_FILE = Path.home() / ".config" / "autostart" / "docker-tray.desktop"
AUTOSTART_ENABLED_PREFIX = "X-GNOME-Autostart-enabled="
DOCKER_INSTALL_URL = "https://docs.docker.com/installation/ubuntulinux/"
COMPOSE_SELECTION_FILE = APP_CONFIG_DIR / "compose-files.txt"
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
    "node_modules",
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
}


def make_icon():
    img = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    badge = (255, 255, 255, 255)
    spout = (0, 0, 0, 255)

    d.rounded_rectangle([6, 6, 58, 58], radius=14, fill=badge)

    d.arc([17, 17, 32, 36], 190, 345, fill=spout, width=4)
    d.arc([32, 17, 47, 36], 195, 350, fill=spout, width=4)
    d.line([32, 34, 32, 47], fill=spout, width=4)
    d.ellipse([29, 44, 35, 50], fill=spout)
    return img


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


def should_skip_compose_scan_dir(root, dirname):
    path = Path(root, dirname)
    try:
        rel = path.relative_to(Path.home())
    except ValueError:
        rel = path
    return (
        dirname in COMPOSE_SCAN_SKIP_DIRS
        or str(rel) in COMPOSE_SCAN_SKIP_DIRS
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

    if compose_file.name in {"compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml"}:
        return str(rel.parent) if str(rel.parent) != "." else compose_file.name
    return str(rel)


def read_selected_compose_files():
    try:
        paths = [
            Path(line.strip())
            for line in COMPOSE_SELECTION_FILE.read_text().splitlines()
            if line.strip()
        ]
    except FileNotFoundError:
        return []
    except Exception:
        return []

    return sorted(paths, key=lambda path: str(path).lower())


def write_selected_compose_files(compose_files):
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    unique_paths = sorted({str(path) for path in compose_files})
    text = "\n".join(unique_paths)
    COMPOSE_SELECTION_FILE.write_text(f"{text}\n" if text else "")


def add_selected_compose_file(compose_file):
    selected = read_selected_compose_files()
    if compose_file not in selected:
        selected.append(compose_file)
        write_selected_compose_files(selected)


def remove_selected_compose_file(compose_file):
    selected = [
        selected_file
        for selected_file in read_selected_compose_files()
        if selected_file != compose_file
    ]
    write_selected_compose_files(selected)


def get_compose_scan_state():
    with compose_scan_lock:
        return dict(compose_scan_state)


def update_tray_menu(icon):
    try:
        icon.update_menu()
    except Exception:
        pass


def show_compose_scan_loader():
    if compose_scan_loader["window"] is not None:
        compose_scan_loader["window"].present()
        return GLib.SOURCE_REMOVE

    window = Gtk.Window(title="Docker Tray")
    window.set_default_size(260, 88)
    window.set_resizable(False)
    window.set_keep_above(True)
    window.set_skip_taskbar_hint(True)
    window.set_position(Gtk.WindowPosition.CENTER)

    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.set_border_width(16)

    spinner = Gtk.Spinner()
    spinner.start()

    label = Gtk.Label(label="Searching compose files...")
    label.set_xalign(0)

    box.pack_start(spinner, False, False, 0)
    box.pack_start(label, True, True, 0)
    window.add(box)
    window.show_all()

    compose_scan_loader["window"] = window
    compose_scan_loader["spinner"] = spinner
    compose_scan_loader["label"] = label
    return GLib.SOURCE_REMOVE


def close_compose_scan_loader(message):
    window = compose_scan_loader["window"]
    if window is None:
        return GLib.SOURCE_REMOVE

    spinner = compose_scan_loader["spinner"]
    label = compose_scan_loader["label"]
    if spinner is not None:
        spinner.stop()
    if label is not None:
        label.set_text(message)

    def _destroy():
        current_window = compose_scan_loader["window"]
        if current_window is not None:
            current_window.destroy()
        compose_scan_loader["window"] = None
        compose_scan_loader["spinner"] = None
        compose_scan_loader["label"] = None
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(1200, _destroy)
    return GLib.SOURCE_REMOVE


def start_compose_scan(icon):
    with compose_scan_lock:
        if compose_scan_state["running"]:
            GLib.idle_add(show_compose_scan_loader)
            return
        compose_scan_state["running"] = True
        compose_scan_state["results"] = None
        compose_scan_state["error"] = None

    GLib.idle_add(show_compose_scan_loader)
    update_tray_menu(icon)

    def _scan():
        try:
            results = scan_compose_files()
            error = None
        except Exception as e:
            results = []
            error = f"{type(e).__name__}: {e}"

        with compose_scan_lock:
            compose_scan_state["running"] = False
            compose_scan_state["results"] = results
            compose_scan_state["error"] = error

        update_tray_menu(icon)
        if error:
            GLib.idle_add(close_compose_scan_loader, "Compose search failed")
        else:
            GLib.idle_add(close_compose_scan_loader, f"Found {len(results)} compose file(s)")

    threading.Thread(target=_scan, daemon=True).start()


def read_autostart_enabled():
    try:
        for line in AUTOSTART_DESKTOP_FILE.read_text().splitlines():
            if line.startswith(AUTOSTART_ENABLED_PREFIX):
                return line.split("=", 1)[1].strip().lower() == "true"
    except FileNotFoundError:
        return False
    except Exception:
        return False
    return False


def build_autostart_desktop(enabled):
    script_path = Path(__file__).resolve()
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=Docker Tray",
        f"Exec=python3 {script_path}",
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


def make_compose_start_cb(compose_file):
    return lambda icon, item: run_compose_up(compose_file)


def make_compose_add_cb(compose_file):
    def _add(icon, item):
        add_selected_compose_file(compose_file)
        update_tray_menu(icon)

    return _add


def make_compose_remove_cb(compose_file):
    def _remove(icon, item):
        remove_selected_compose_file(compose_file)
        update_tray_menu(icon)

    return _remove


def make_compose_search_cb():
    return lambda icon, item: start_compose_scan(icon)


def make_start_cb(name):
    return lambda icon, item: run_docker_action("start", name)


def make_restart_cb(name):
    return lambda icon, item: run_docker_action("restart", name)


def make_stop_cb(name):
    return lambda icon, item: run_docker_action("stop", name)


def start_menu_polling(icon):
    icon.visible = True
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


def get_compose_file_items(pystray):
    try:
        selected_files = read_selected_compose_files()
        scan_state = get_compose_scan_state()
    except Exception as e:
        return [pystray.MenuItem(f"Error: {type(e).__name__}: {e}", None)]

    items = []
    if selected_files:
        for compose_file in selected_files:
            items.append(pystray.MenuItem(
                compose_file_label(compose_file),
                pystray.Menu(
                    pystray.MenuItem("Start", make_compose_start_cb(compose_file)),
                    pystray.MenuItem("Remove", make_compose_remove_cb(compose_file)),
                ),
            ))
    else:
        items.append(pystray.MenuItem("No compose files added", None, enabled=False))

    items += [
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Search compose files", make_compose_search_cb()),
    ]

    if scan_state["running"]:
        items.append(pystray.MenuItem("Searching...", None, enabled=False))
        return items

    if scan_state["error"]:
        items.append(pystray.MenuItem(f"Error: {scan_state['error']}", None, enabled=False))
        return items

    discovered_files = scan_state["results"]
    if discovered_files is None:
        return items

    selected_set = set(selected_files)
    available_files = [
        compose_file
        for compose_file in discovered_files
        if compose_file not in selected_set
    ]
    if available_files:
        add_items = [
            pystray.MenuItem(
                compose_file_label(compose_file),
                make_compose_add_cb(compose_file),
            )
            for compose_file in available_files
        ]
    else:
        add_items = [pystray.MenuItem("No new compose files found", None, enabled=False)]

    items.append(pystray.MenuItem(
        "Add found compose file",
        pystray.Menu(*add_items),
    ))
    return items


def get_settings_items(pystray):
    return [
        pystray.MenuItem(
            "Compose files",
            pystray.Menu(lambda: get_compose_file_items(pystray)),
        ),
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
                        "  Open in browser ↗",
                        make_open_cb(port)
                    ))
                if running:
                    sub += [
                        pystray.MenuItem("  Restart ↻", make_restart_cb(name)),
                        pystray.MenuItem("  Stop ✕", make_stop_cb(name)),
                    ]
                else:
                    sub.append(pystray.MenuItem("  Start ▸", make_start_cb(name)))

                status_marker = "• " if running else "◦ "
                label = (
                    f"{status_marker}{name}  :{port}"
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

#!/usr/bin/env python3
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
AUTOSTART_DESKTOP_FILE = Path.home() / ".config" / "autostart" / "docker-tray.desktop"
AUTOSTART_ENABLED_PREFIX = "X-GNOME-Autostart-enabled="
DOCKER_INSTALL_URL = "https://docs.docker.com/installation/ubuntulinux/"


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
    try:
        icon.update_menu()
    except Exception:
        pass


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

        try:
            icon.update_menu()
        except Exception:
            pass


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
            pystray.Menu(
                pystray.MenuItem(
                    get_start_at_boot_label,
                    toggle_start_at_boot,
                ),
            ),
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

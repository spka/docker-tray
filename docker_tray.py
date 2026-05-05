#!/usr/bin/env python3
import threading
import subprocess
import re
import os

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib

import pystray
from PIL import Image, ImageDraw


def make_icon():
    img = Image.new("RGB", (64, 64), color=(30, 144, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, 56, 56], radius=12, fill=(20, 100, 200))
    d.rectangle([18, 24, 46, 40], fill="white")
    d.ellipse([22, 28, 30, 36], fill=(30, 144, 255))
    d.ellipse([34, 28, 42, 36], fill=(30, 144, 255))
    return img


def get_containers():
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
        capture_output=True, text=True
    )
    containers = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        name = parts[0]
        status = parts[1] if len(parts) > 1 else ""
        ports = parts[2] if len(parts) > 2 else ""
        port = extract_web_port(ports)
        containers.append((name, status, port))
    return containers


def extract_web_port(ports_str):
    match = re.search(r'0\.0\.0\.0:(\d+)->\d+/tcp', ports_str)
    return match.group(1) if match else None


def restart_container(name):
    def _do():
        subprocess.run(["docker", "restart", name])
    threading.Thread(target=_do, daemon=True).start()


def open_url(port, name=""):
    url = f"http://localhost:{port}"

    def _open():
        bridge = Gtk.Window()
        bridge.set_default_size(1, 1)

        def on_map(w):
            Gtk.show_uri_on_window(w, url, 0)
            GLib.timeout_add(500, lambda: w.destroy() or False)

        bridge.connect("map", on_map)
        bridge.show()
        return GLib.SOURCE_REMOVE

    GLib.idle_add(_open)


def make_open_cb(p, n):
    return lambda icon, item: open_url(p, n)

def make_restart_cb(n):
    return lambda icon, item: restart_container(n)

def get_menu_items():
    items = []
    try:
        for name, status, port in get_containers():
            sub = []
            if port:
                sub.append(pystray.MenuItem(
                    f"Open in browser  :{port}",
                    make_open_cb(port, name)
                ))
            sub.append(pystray.MenuItem(
                "Restart",
                make_restart_cb(name)
            ))
            items.append(pystray.MenuItem(f"  {name}", pystray.Menu(*sub)))
    except Exception as e:
        import traceback
        traceback.print_exc()
        items.append(pystray.MenuItem(f"Error: {type(e).__name__}: {e}", None))

    items += [
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda icon, item: icon.stop()),
    ]
    return items


def main():
    icon = pystray.Icon(
        "docker-tray",
        make_icon(),
        "Docker Monitor",
        menu=pystray.Menu(get_menu_items),
    )
    icon.run()


if __name__ == "__main__":
    main()

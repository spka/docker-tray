#!/usr/bin/env python3
import re
import subprocess
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

from PIL import Image, ImageDraw


DOCKER_PS_FORMAT = "{{.Names}}\t{{.Ports}}"
HOST_PORT_RE = re.compile(r"(?:0\.0\.0\.0|127\.0\.0\.1):(\d+)->\d+/tcp")


def make_icon():
    img = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    white = (255, 255, 255, 255)

    d.rectangle([13, 30, 43, 38], fill=white)
    d.rectangle([17, 22, 24, 28], fill=white)
    d.rectangle([26, 22, 33, 28], fill=white)
    d.rectangle([35, 22, 42, 28], fill=white)
    d.rectangle([26, 14, 33, 20], fill=white)

    d.polygon([(45, 29), (55, 25), (52, 34)], fill=white)
    d.pieslice([9, 24, 56, 52], 0, 180, fill=white)
    d.rectangle([9, 38, 50, 41], fill=white)
    d.ellipse([46, 28, 49, 31], fill=(0, 0, 0, 255))
    return img


def get_containers():
    result = subprocess.run(
        ["docker", "ps", "--format", DOCKER_PS_FORMAT],
        capture_output=True,
        text=True,
        check=True,
    )

    containers = []
    for line in result.stdout.strip().splitlines():
        name, _, ports = line.partition("\t")
        containers.append((name, extract_web_port(ports)))
    return containers


def extract_web_port(ports_str):
    match = HOST_PORT_RE.search(ports_str)
    return match.group(1) if match else None


def restart_container(name):
    threading.Thread(
        target=subprocess.run,
        args=(["docker", "restart", name],),
        daemon=True,
    ).start()


def open_url(port):
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


def make_open_cb(port):
    return lambda icon, item: open_url(port)


def make_restart_cb(name):
    return lambda icon, item: restart_container(name)


def get_menu_items(pystray):
    items = []
    try:
        for name, port in get_containers():
            sub = []
            if port:
                sub.append(pystray.MenuItem(
                    f"Open in browser :{port}",
                    make_open_cb(port)
                ))
            sub.append(pystray.MenuItem(
                "Restart",
                make_restart_cb(name)
            ))
            items.append(pystray.MenuItem(name, pystray.Menu(*sub)))
    except Exception as e:
        items.append(pystray.MenuItem(f"Error: {type(e).__name__}: {e}", None))

    items += [
        pystray.Menu.SEPARATOR,
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
    icon.run()


if __name__ == "__main__":
    main()

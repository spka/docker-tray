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

    deep_blue = (12, 68, 120, 255)
    mid_blue = (20, 124, 190, 255)
    light_blue = (116, 204, 232, 255)
    foam = (232, 250, 255, 255)

    d.ellipse([7, 33, 57, 58], fill=deep_blue)
    d.pieslice([6, 21, 58, 59], 14, 166, fill=mid_blue)
    d.arc([12, 30, 52, 62], 190, 350, fill=light_blue, width=3)

    d.polygon([(32, 30), (20, 8), (29, 10), (36, 25)], fill=deep_blue)
    d.polygon([(32, 30), (44, 8), (35, 10), (28, 25)], fill=deep_blue)
    d.polygon([(28, 18), (15, 12), (21, 7), (34, 23)], fill=mid_blue)
    d.polygon([(36, 18), (49, 12), (43, 7), (30, 23)], fill=mid_blue)
    d.line([(32, 27), (32, 38)], fill=foam, width=3)

    d.ellipse([20, 35, 44, 47], fill=foam)
    d.ellipse([22, 32, 42, 43], fill=mid_blue)
    d.arc([17, 38, 47, 54], 200, 340, fill=foam, width=2)
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
                    f"Open in browser  :{port}",
                    make_open_cb(port)
                ))
            sub.append(pystray.MenuItem(
                "Restart",
                make_restart_cb(name)
            ))
            items.append(pystray.MenuItem(f"  {name}", pystray.Menu(*sub)))
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

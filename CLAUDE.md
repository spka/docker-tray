# docker-tray

System tray app for Ubuntu GNOME on Wayland. It lists running Docker
containers, shows a submenu for each container, and lets the user open exposed
web ports in the browser or restart the container.

This file is the handoff note for future agent sessions. The important part is
the Wayland URL-opening workaround below; do not simplify it away without
testing the tray menu against an already-running Flatpak Firefox instance.

## Files

- `docker_tray.py` - main app.
- `requirements.txt` - Python package notes.
- `~/.config/autostart/docker-tray.desktop` - starts the tray app on login.

## Stack

- `pystray` - system tray icon and dynamic menu.
- `ayatana-appindicator3` backend - pystray backend used on Ubuntu/GNOME.
- `Pillow` - draws the tray icon.
- `GTK3` via `gi.repository.Gtk` and `GLib` - required for Wayland-aware URL
  opening.
- Python stdlib `subprocess` - Docker interaction through `docker ps` and
  `docker restart`.

## Install

System packages:

```bash
sudo apt install python3-pystray python3-pil python3-gi gir1.2-gtk-3.0
```

Docker must be installed and the current user must be allowed to run `docker ps`
without a password.

Autostart entry:

```text
~/.config/autostart/docker-tray.desktop
```

Current autostart command:

```text
python3 /home/stephan/development/docker-tray/docker_tray.py
```

## Run

Start manually:

```bash
python3 /home/stephan/development/docker-tray/docker_tray.py &
```

Restart after edits:

```bash
pkill -f docker_tray.py
python3 /home/stephan/development/docker-tray/docker_tray.py &
```

Check whether it is running:

```bash
pgrep -af docker_tray.py
```

## Current Behavior

The app polls Docker state every 5 seconds and calls `icon.update_menu()` only
when the container snapshot changes, because AppIndicator menus are not
guaranteed to rebuild every time they open. For every container returned by
`docker ps -a`, the app displays the container name and a submenu.

Running containers, detected from Docker status strings beginning with `Up`, get
a leading `•` marker. Stopped containers get a leading `◦` marker.

If the container exposes a host port matching either of these forms:

```text
0.0.0.0:<host-port>-><container-port>/tcp
127.0.0.1:<host-port>-><container-port>/tcp
```

the submenu includes:

- `Open in browser :<host-port>`
- `Restart`

If no matching web port is found, only `Restart` is shown.

## Key Design Decisions

### No Docker SDK

Do not switch this app to the Docker Python SDK casually. On this system the SDK
failed with a `chunked` keyword error. The current app intentionally uses:

```bash
docker ps -a --format "{{.Names}}\t{{.Status}}\t{{.Ports}}"
```

This includes names, status, and ports so stopped containers can stay visible
while running state changes.

### URL Opening on Wayland

This was the hard bug.

Firefox is installed as a Flatpak. Calling Firefox directly, for example with:

```python
subprocess.run(["flatpak", "run", "org.mozilla.firefox", url])
```

can silently ignore the URL when Firefox is already running. The tray app is a
background process, and Wayland's xdg-activation protocol prevents it from
activating or handing a URL to an already-running app unless the request carries
a valid activation token.

The working solution is to create a real 1x1 GTK window, wait for it to be
mapped, and call `Gtk.show_uri_on_window()` from that mapped window. GTK can then
derive the activation token from the Wayland surface and pass it to Firefox.

The 1x1 window is not decoration. It is the activation bridge. Removing it can
bring back the bug where menu clicks do nothing while Firefox is open.

Current implementation:

```python
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
```

Important details:

- `Gtk.show_uri_on_window()` must run after the bridge window is mapped.
- Use `GLib.idle_add()` so GTK work runs on the GTK/main loop side.
- The bridge window is destroyed shortly after the URL is handed off.
- Test with Firefox already running, not only from a closed browser state.

### pystray Callback Signature

`pystray` rejects actions with more than two parameters. Use closure factories
for menu item callbacks:

```python
def make_open_cb(port):
    return lambda icon, item: open_url(port)
```

Do not write callbacks that require `port`, `name`, `icon`, and `item` as direct
parameters.

### Dynamic Menu Refresh

Pass a callable into `pystray.Menu`:

```python
menu=pystray.Menu(lambda: get_menu_items(pystray))
```

Do not call `get_menu_items(pystray)` during icon construction. The current
approach, plus the 5-second Docker snapshot poller, lets the menu reflect Docker
status changes that happen outside the tray app without redrawing the menu on
every poll.

Because the app passes a custom `setup` callback to `icon.run()`, that callback
must set `icon.visible = True`. Without that line the process runs but the tray
icon does not appear.

## Troubleshooting

### Tray Icon Does Not Appear

Verify the app is running:

```bash
pgrep -af docker_tray.py
```

Verify required packages are installed:

```bash
python3 -c "import pystray, gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk"
```

Ubuntu GNOME may also require AppIndicator support packages. The known-good
dependency path is the apt install command in the install section.

### Menu Has No Containers

Run:

```bash
docker ps -a --format "{{.Names}}\t{{.Status}}\t{{.Ports}}"
```

If that command fails or returns nothing, the tray app will not have containers
to display. Fix Docker permissions or start the containers first.

### Container Has No Open Button

The parser currently recognizes ports exposed as:

```text
0.0.0.0:<host-port>-><container-port>/tcp
127.0.0.1:<host-port>-><container-port>/tcp
```

Containers bound to IPv6, UDP-only ports, or unusual Docker output may need
`extract_web_port()` to be expanded.

### Clicking Open Does Nothing

Test this exact scenario:

1. Start Firefox first.
2. Start `docker_tray.py`.
3. Click a container's `Open in browser` menu item.

If it fails, inspect the `open_url()` GTK bridge before trying shell helpers,
`gio open`, `xdg-open`, or direct Flatpak commands. Those approaches were the
source of the original long debugging session.

## Maintenance Notes

- Keep edits small; this is a simple tray utility.
- Prefer subprocess calls to Docker CLI over adding SDK dependencies.
- Keep GTK URL opening in the main thread/main loop.
- Preserve the 1x1 mapped-window activation bridge unless a replacement is
  tested on GNOME Wayland with already-running Flatpak Firefox.
- Keep `pystray` imported inside `main()`. Importing it at module load can try
  to connect to the display, which makes simple parser tests fail outside a
  normal desktop session.
- If adding support for more port formats, update `extract_web_port()` and test
  against real `docker ps -a --format` output.

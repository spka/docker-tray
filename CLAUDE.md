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
  `docker restart`, and `docker compose -f <file> up -d`.
- Python stdlib `shutil` - checks whether the `docker` CLI exists before trying
  to list containers.
- `~/.config/autostart/docker-tray.desktop` - managed by the tray menu's
  `Settings -> Start at boot` toggle.
- `~/.config/docker-tray/compose-files.txt` - selected Docker Compose files
  shown under `Settings -> Compose files`.

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

Containers are sorted alphabetically by name before the menu is built, so the
order is stable.

Running containers, detected from Docker status strings beginning with `Up`, get
a leading `•` marker. Stopped containers get a leading `◦` marker.

If the container exposes a host port matching either of these forms:

```text
0.0.0.0:<host-port>-><container-port>/tcp
127.0.0.1:<host-port>-><container-port>/tcp
```

running container submenus include:

- `Restart ↻`
- `Stop ✕`

If a running container has a matching web port, it also includes
`Open in browser ↗`.

Stopped container submenus include:

- `Start ▸`

## Key Design Decisions

### No Docker SDK

Do not switch this app to the Docker Python SDK casually. On this system the SDK
failed with a `chunked` keyword error. The current app intentionally uses:

```bash
docker ps -a --format "{{.Names}}\t{{.Status}}\t{{.Ports}}"
```

This includes names, status, and ports so stopped containers can stay visible
while running state changes.

### Docker Install Check

Before listing containers, the tray menu checks whether the `docker` executable
is on `PATH`. If it is missing, the menu shows:

- `Docker is not installed`
- `Download Docker ↗`

The download item opens Docker's official Ubuntu Engine install page:

```text
https://docs.docker.com/installation/ubuntulinux/
```

Keep this pointed at official Docker documentation. The app still handles Docker
daemon or permission errors separately through the normal error menu item.

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

### Start At Boot Toggle

The tray menu has a `Settings` submenu with a `Start at boot` toggle. That
toggle reads and writes the `X-GNOME-Autostart-enabled` value inside
`~/.config/autostart/docker-tray.desktop`.

When enabled, the menu label renders as `Start at boot ✓`. This keeps the state
marker after the text instead of relying on the tray backend's native checkmark,
which appears before the item text.

If the desktop file is missing, the toggle recreates it with the current script
path in the `Exec=` line and enables autostart.

### Compose File Launcher

The tray menu also has `Settings -> Compose files`. It scans the current user's
home directory for:

- `compose.yml`
- `compose.yaml`
- `docker-compose.yml`
- `docker-compose.yaml`

The app does not scan for these files at startup or while opening the regular
tray menu. The scan starts only when the user clicks `Search compose files`.
Clicking a tray menu action closes the AppIndicator menu, so the app also opens
a small GTK loader window while the background scan runs. If the compose submenu
is opened during the scan, it shows `Searching...`.

Found files are not added automatically. After the scan completes, the user has
to choose one from `Add found compose file`, which saves the path in:

```text
~/.config/docker-tray/compose-files.txt
```

Each saved compose file gets a submenu with:

- `Start` - runs `docker compose -f <file> up -d`
- `Remove` - removes the saved path from the tray app list

The scan skips hidden directories plus `.cache`, `.git`, `.local/share/Trash`,
`__pycache__`, and `node_modules`.

## Troubleshooting

### Tray Icon Does Not Appear

Verify the app is running:

```bash
pgrep -af docker_tray.py
```

When working from a sandboxed agent session, regular `pgrep` can miss host
desktop processes or show only sandbox wrapper commands. Check the host process
table explicitly:

```bash
ps -eo pid=,ppid=,user=,cmd= | rg '[d]ocker_tray\.py'
```

If duplicate tray icons were started from the sandbox, kill the specific host
PIDs and start one clean copy in the desktop session:

```bash
kill <pid> <pid> ...
setsid env DISPLAY=:0 WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
  python3 /home/stephan/development/docker-tray/docker_tray.py \
  >/tmp/docker-tray.log 2>&1 < /dev/null &
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

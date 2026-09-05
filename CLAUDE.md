# docker-tray

System tray app for Linux desktops with AppIndicator/StatusNotifier support. It lists running Docker
containers, shows a submenu for each container, and lets the user open exposed
web ports in the browser or restart the container.

This file is the handoff note for future agent sessions. The important part is
the Wayland URL-opening workaround below; do not simplify it away without
testing the tray menu against an already-running Flatpak Firefox instance.

## Files

- `docker_tray.py` - tray startup, menu wiring, and non-update features.
- `docker_tray_update_service.py` - GTK-independent update state, cache, and workers.
- `docker_tray_update_backend.py` - update-related Docker commands, HTTP requests,
  downloads, and privileged transactions; command and HTTP implementations are injectable.
- `docker_tray_updates_dialog.py` - GTK rendering and update action callbacks.
- `docker_tray_commands.py` - shared command and authorization error messages.
- `docker_tray_runtime.py` - explicit Docker and PolicyKit command paths.
- `docker_tray_state.py` - typed mutable process state.
- `docker_tray_ui.py` - shared GTK dialog lifecycle.
- `docker_tray_compose.py` - filesystem-only Compose discovery helpers.
- `docker_tray_stats.py` - pure stats parsing, formatting, and health logic.
- `docker_tray_updates.py` - pure release and image-update helpers.
- `docker_tray_autostart.py` - XDG autostart file handling.
- `docker_tray_platform.py` - distro/desktop detection, install URLs, Docker
  Engine update providers, and theme detection.
- `icon-dark.png` - tray icon for dark panels (white badge, 256×256).
- `icon-light.png` - tray icon for light panels (black badge, 256×256).
- `requirements.txt` - Python package notes.
- `~/.config/autostart/docker-tray.desktop` - starts the tray app on login.
- `~/.local/share/docker-tray/stats.jsonl` - container stats history (JSONL, one sample per container per poll, fields: `t`, `name`, `cpu`, `mem`, `mem_str`). Polled every ~5 minutes. Read this file to inspect historical CPU/memory data instead of relying on live `docker stats`.

## Stack

- `pystray` - system tray icon and dynamic menu.
- `ayatana-appindicator3` backend - pystray backend used on GNOME and KDE
  Plasma sessions with AppIndicator/StatusNotifier tray support.
- `Pillow` - loads the tray icon PNG.
- `GTK3` via `gi.repository.Gtk`, `GLib`, and `Gio` - required for Wayland-aware
  URL opening and light/dark theme detection.
- Python stdlib `subprocess` - Docker interaction through the explicit gateway
  in `docker_tray_runtime.py`, backed by the packaged root-owned PolicyKit
  broker and its narrow command allowlist.
- `~/.config/autostart/docker-tray.desktop` - managed by the tray menu's
  `Settings -> Start at boot` toggle.

## Install

System packages:

```bash
sudo apt install python3-pystray python3-pil python3-gi gir1.2-gtk-3.0
```

Docker must be installed. The packaged PolicyKit broker provides read access
without putting the current user in the root-equivalent `docker` group, and
prompts for administrator authentication for state-changing operations.

Autostart entry:

```text
~/.config/autostart/docker-tray.desktop
```

Current autostart command:

```text
python3 /home/stephan/Development/docker-tray/docker_tray.py
```

## Run

Start manually:

```bash
python3 /home/stephan/Development/docker-tray/docker_tray.py &
```

Restart after edits:

```bash
pkill -f docker_tray.py
python3 /home/stephan/Development/docker-tray/docker_tray.py &
```

Check whether it is running:

```bash
pgrep -af docker_tray.py
```

## Current Behavior

The app keeps one read-only privileged watcher open. It reads the Docker socket
every 5 seconds and streams sanitized snapshots to the unprivileged tray. The
tray calls `icon.update_menu()` only when the snapshot changes, because
AppIndicator menus are not guaranteed to rebuild every time they open.

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

- `🔄 Restart`
- `⏹️ Stop`

If a running container has a matching web port, it also includes
`🔗 Open`.

Stopped container submenus include:

- `▶️ Start`

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

The download item opens a platform-specific Docker install page:

```text
Ubuntu: https://docs.docker.com/engine/install/ubuntu/
Debian: https://docs.docker.com/engine/install/debian/
Arch: https://wiki.archlinux.org/title/Docker
```

Keep this in `docker_tray_platform.py`. The app still handles Docker daemon or
permission errors separately through the normal error menu item.

### Light/Dark Tray Icon

The app ships two icon files — `icon-dark.png` (white badge, for dark panels) and
`icon-light.png` (black badge, for light panels). On startup, `is_dark_mode()`
delegates to `docker_tray_platform.py`. GNOME reads
`org.gnome.desktop.interface color-scheme` via `Gio.Settings`; KDE Plasma uses
`~/.config/kdeglobals` as a best-effort fallback. GNOME registers a GSettings
listener in `watch_theme()` to swap `icon.icon` live when the system theme
changes.

To replace the icons: put new 256×256 PNGs in the same directory and restart the
app. Keep both files present; the app will crash on startup if either is missing.

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
toggle reads and writes the XDG `Hidden` value and preserves the GNOME
`X-GNOME-Autostart-enabled` value inside
`~/.config/autostart/docker-tray.desktop`.

When enabled, the menu label renders as `Start at boot ✓`. This keeps the state
marker after the text instead of relying on the tray backend's native checkmark,
which appears before the item text.

If the desktop file is missing, the toggle recreates it with the current script
path in the `Exec=` line and enables autostart.

### Compose File Launcher

The tray menu has `Settings -> Compose search`. It opens a small GTK
dialog before scanning. The dialog has a search-directory dropdown with common
locations inside the user's home, such as Home, development, Documents,
Downloads, Desktop, and Projects when those directories exist. This boundary
matches the PolicyKit helper's Compose path validation. The user can cancel or
confirm the scan.

When confirmed, it scans the chosen directory for:

- `compose.yml`
- `compose.yaml`
- `docker-compose.yml`
- `docker-compose.yaml`

The app does not scan for these files at startup or while opening the regular
tray menu. After confirmation, the dialog shows a spinner while the background
scan runs. If no compose files are found, it shows a short message and closes
after a visible countdown.

After the scan completes, the dialog lists every found compose file. It asks
Docker for Compose labels on existing containers using `docker ps -a`, then
marks compose files with at least one running container as `Running`. Compose
files that are not running get a `Run` button, which starts them with:

```bash
docker compose -f <file> up -d
```

After `Run` is clicked, the dialog changes the button to `Starting` and polls
Docker for up to about a minute. Once Docker reports a running container for
that compose file, the row changes to `Running`; if it never appears running,
the `Run` button is enabled again.

The scan skips hidden directories plus `.cache`, `.git`, `.local/share/Trash`,
`__pycache__`, `node_modules`, and heavy/pseudo-system paths such as `/proc`,
`/sys`, `/dev`, `/run`, `/tmp`, `/var/lib/docker`, and `/var/lib/containerd`.

If Docker is not available or the user cannot read Docker state, the scan still
lists the compose files and treats their running state as unknown, which means
they get a `Run` button.

### Docker Cleanup Check

The tray menu has `Settings -> Cleanup`. It opens a GTK popup and checks
for conservative cleanup candidates:

- stopped or created containers
- dangling images that are not referenced by any container
- unused networks
- reclaimable build cache

If nothing is found, the popup says `Everything is fine`. If cleanup candidates
are found, it lists them and shows a `Cleanup` button.

The cleanup action authenticates once, then the root-owned broker runs these
fixed conservative prunes as one transaction:

```bash
docker container prune -f
docker image prune -f
docker network prune -f
docker builder prune -f
```

The popup shows command output after cleanup, which is important when Docker
refuses to remove something. Docker can show images as dangling even while a
container still uses their image ID; those images are intentionally excluded
from the popup because `docker image prune` cannot remove them yet. Do not add
`--volumes` casually. Volumes can hold persistent app data and are intentionally
not removed by this cleanup button.

### Image Update Transactions

Image updates do not send individual write commands through the generic Docker
wrapper. One `image-update` PolicyKit action starts the root-owned broker for a
single image or the entire `Update all` batch. The broker discovers Compose
labels directly, accepts only Compose files and working directories inside the
invoking user's home, pulls and recreates each service, waits for running and
healthy containers on the new image ID, and removes replaced image IDs only
when no container still uses them.

The main app wires one `UpdateService` to an `UpdateBackend` and an
`UpdatesDialog`. Each service owns its check state, operation state, and digest
cache. The service does not import GTK or the main app. `GLib.idle_add` is
injected as its dispatcher, keeping dialog refreshes and notifications on the
desktop side. Tests can instantiate independent services with a fake backend
and queued dispatcher. The service's `close()` wakes its periodic checker on
shutdown; already-authorized operations are not cancelled by closing a dialog.

Remote manifest checks use at most four workers. Successful registry digests
are cached for 15 minutes and failures for two minutes. Individual registry
failures retain previous notices for those images while successful checks still
refresh other images. `UpdateService.run_update_check()` holds a non-blocking
run lock until its results have been applied through the dispatcher, so timer
and post-update refreshes cannot overlap or apply out-of-order snapshots.

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
  python3 /home/stephan/Development/docker-tray/docker_tray.py \
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

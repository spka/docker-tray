# docker-tray

A small Linux tray utility for people running Docker Compose services on a home
server or workstation.

It keeps common Docker chores out of the terminal: check which containers are
running, start or stop them, restart a service, and open exposed web ports in
your browser from the tray menu. It can also scan for compose files, bring a
compose stack up, show Docker cleanup options, and flag Docker Engine or image
updates. On Debian and Ubuntu, Docker Tray can download, verify, and install its
own release updates from the updates dialog.

## Why

If you run a bunch of self-hosted services, jumping into a terminal just to see
status or remember which localhost port opens which web UI gets old quickly.
Docker Desktop is not a great fit for that either: frequent updates, manual
package downloads, and more bundled extras than a small home-server workflow
needs. This keeps the heavy lifting in the Docker CLI and adds only a small tray
menu for quick checks and actions.

## Supported Desktops And Distros

Docker Tray is tested first on Ubuntu/GNOME and includes platform support for
Arch-family distributions on KDE Plasma.

The core tray actions use the Docker CLI and should be portable across Linux
desktops with AppIndicator/StatusNotifier tray support. Distro-specific behavior
is isolated to install help, Docker Engine update checks, theme detection, and
packaging.

## Install On Debian/Ubuntu

Download the release package and its checksum:

```bash
curl -fLO https://github.com/spka/docker-tray/releases/download/v0.2.7/docker-tray_0.2.7_all.deb
curl -fLO https://github.com/spka/docker-tray/releases/download/v0.2.7/docker-tray_0.2.7_all.deb.sha256
sha256sum --check docker-tray_0.2.7_all.deb.sha256
sudo apt install ./docker-tray_0.2.7_all.deb
```

Docker Tray starts automatically on login after installing the package.
The desktop autostart entry launches a per-user systemd service, which restarts
the tray after an unexpected crash and stops it with the graphical session.

## Install On Arch Linux

Use the Arch package recipe in `packaging/arch/PKGBUILD`.

```bash
cd packaging/arch
makepkg -si
```

On KDE Plasma, make sure the system tray is enabled and configured to show
application status items.

Docker must already be installed. On Debian and Ubuntu, the packaged PolicyKit
helper provides narrowly scoped status access and asks for desktop
authentication before state-changing Docker actions. Direct membership in the
`docker` group is not required. A persistent read-only helper streams sanitized
container snapshots to the tray without repeatedly launching Docker CLI
processes. Compose discovery and launches are limited to files inside the
current user's home directory. Cleanup is authorized once per cleanup run.
Single-image and batch image updates likewise use one authenticated helper
transaction for discovery, pull, recreation, readiness checks, and safe removal
of replaced images.

Remote registry checks run through a four-worker pool, and successful digests
are cached for 15 minutes. Local image IDs are inspected in one batched
PolicyKit transaction instead of launching one helper per image. The updates
view reports checks in progress, the last completed check, and offline, Docker
Engine, or registry failures instead of presenting failed checks as “no
updates.” Cancelled, denied, and failed privilege requests are reported
separately and do not imply that an operation failed midway.

Docker Tray sends health, update, and action-failure notices directly to the
desktop's standard notification service, so they appear as Ubuntu notification
popups and remain available in the notification list. Use **Settings → Test
notification** to verify delivery. Delivery runs outside the GTK event loop
and times out cleanly if the desktop notification service is unavailable.

Automatic package installation is offered only for release assets carrying a
GitHub SHA-256 digest. The root-owned helper copies the download into a
root-owned directory, verifies its checksum and Debian metadata again, and only
then invokes APT.

Future release tags matching `vX.Y.Z` are built and published by GitHub Actions
after the tag, application, and Debian versions are confirmed to match. The AUR
recipe independently tracks the latest published release with a verified source
checksum and generated `.SRCINFO`. Each release includes a SHA-256 checksum and
GitHub build-provenance attestation.

## Uninstall

```bash
sudo apt remove docker-tray
rm -f ~/.config/autostart/docker-tray.desktop
```

## License

MIT

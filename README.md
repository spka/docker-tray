# docker-tray

A small Linux tray utility for people running Docker Compose services on a home
server or workstation.

It keeps common Docker chores out of the terminal: check which containers are
running, start or stop them, restart a service, and open exposed web ports in
your browser from the tray menu. It can also scan for compose files, bring a
compose stack up, show Docker cleanup options, and flag Docker Engine or image
updates.

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

Install the release package and run the packaged launcher:

```bash
# Download the release package to /tmp.
curl -L -o /tmp/docker-tray_0.2.1_all.deb https://github.com/spka/docker-tray/releases/download/v0.2.1/docker-tray_0.2.1_all.deb

# Make the file readable by apt's sandbox user.
chmod 0644 /tmp/docker-tray_0.2.1_all.deb

# Install or upgrade Docker Tray.
sudo apt install /tmp/docker-tray_0.2.1_all.deb

# Remove the temporary package file.
rm /tmp/docker-tray_0.2.1_all.deb
```

Docker Tray starts automatically on login after installing the package.

## Install On Arch Linux

Use the Arch package recipe in `packaging/arch/PKGBUILD`.

```bash
cd packaging/arch
makepkg -si
```

On KDE Plasma, make sure the system tray is enabled and configured to show
application status items.

Docker must already be installed, and your user must be able to run `docker`
without a password.

## Uninstall

```bash
sudo apt remove docker-tray
rm -f ~/.config/autostart/docker-tray.desktop
```

## License

MIT

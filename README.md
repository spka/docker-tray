# docker-tray

A small Ubuntu/GNOME tray utility for people running Docker Compose services on
a home server or workstation.

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

## Install

For normal use, install the release package and run the packaged launcher:

```bash
# Download the release package to /tmp.
curl -L -o /tmp/docker-tray_0.1.3_all.deb https://github.com/spka/docker-tray/releases/download/v0.1.3/docker-tray_0.1.3_all.deb

# Make the file readable by apt's sandbox user.
chmod 0644 /tmp/docker-tray_0.1.3_all.deb

# Install or upgrade Docker Tray.
sudo apt install /tmp/docker-tray_0.1.3_all.deb

# Remove the temporary package file.
rm /tmp/docker-tray_0.1.3_all.deb
```

Docker Tray starts automatically on login after installing the package.

Docker must already be installed, and your user must be able to run `docker`
without a password.

## Uninstall

```bash
sudo apt remove docker-tray
rm -f ~/.config/autostart/docker-tray.desktop
```

## License

MIT

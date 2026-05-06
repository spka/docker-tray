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

```bash
sudo apt install python3-pystray python3-pil python3-gi gir1.2-gtk-3.0
python3 docker_tray.py
```

Docker must already be installed, and your user must be able to run `docker`
without a password.

Release packages can be installed with:

```bash
sudo apt install ./docker-tray_0.1.0_all.deb
docker-tray
```

## Uninstall

```bash
sudo apt remove docker-tray
rm -f ~/.config/autostart/docker-tray.desktop
```

## License

MIT

#!/usr/bin/env bash
set -euo pipefail

version="${1:-0.1.0}"
package="docker-tray"
root="dist/${package}_${version}_all"

rm -rf "$root"
mkdir -p \
  "$root/DEBIAN" \
  "$root/usr/bin" \
  "$root/usr/share/docker-tray" \
  "$root/usr/share/doc/docker-tray"

cat > "$root/DEBIAN/control" <<CONTROL
Package: docker-tray
Version: ${version}
Section: utils
Priority: optional
Architecture: all
Maintainer: Stephan Karsten <stephan.karsten@outlook.com>
Depends: python3, python3-pystray, python3-pil, python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1
Description: Small Docker tray utility for Ubuntu GNOME
 Docker Tray shows Docker containers in the system tray and provides quick
 actions for opening exposed web ports, starting, stopping, and restarting
 services. Docker itself is not installed or managed by this package.
CONTROL

install -m 0755 docker_tray.py "$root/usr/bin/docker-tray"
install -m 0644 icon-dark.png icon-light.png "$root/usr/share/docker-tray/"
install -m 0644 README.md "$root/usr/share/doc/docker-tray/README.md"
install -m 0644 LICENSE "$root/usr/share/doc/docker-tray/copyright"

find "$root" -type d -exec chmod 0755 {} +
dpkg-deb --root-owner-group --build "$root" "dist/${package}_${version}_all.deb"

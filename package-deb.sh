#!/usr/bin/env bash
set -euo pipefail

version="${1:-0.2.12}"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
  echo "Version must contain two or three numeric components" >&2
  exit 2
fi
package="docker-tray"
root="dist/${package}_${version}_all"

rm -rf "$root"
mkdir -p \
  "$root/DEBIAN" \
  "$root/etc/xdg/autostart" \
  "$root/usr/share/polkit-1/actions" \
  "$root/usr/bin" \
  "$root/usr/lib/docker-tray" \
  "$root/usr/lib/systemd/user" \
  "$root/usr/share/applications" \
  "$root/usr/share/docker-tray" \
  "$root/usr/share/doc/docker-tray" \
  "$root/usr/share/icons/hicolor/256x256/apps"

cat > "$root/DEBIAN/control" <<CONTROL
Package: docker-tray
Version: ${version}
Section: utils
Priority: optional
Architecture: all
Maintainer: Stephan Karsten <stephan.karsten@outlook.com>
Depends: python3, python3-pystray, python3-pil, python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, pkexec
Description: Small Docker tray utility
 Docker Tray shows Docker containers in the system tray and provides quick
 actions for opening exposed web ports, starting, stopping, and restarting
 services. Docker itself is not installed or managed by this package.
CONTROL

install -m 0755 docker_tray.py "$root/usr/lib/docker-tray/docker_tray.py"
for module in docker_tray_*.py; do
  mode=0644
  if [[ "$module" == "docker_tray_privileged.py" ]]; then
    mode=0755
  fi
  install -m "$mode" "$module" "$root/usr/lib/docker-tray/$module"
done
install -m 0755 docker-tray-docker "$root/usr/lib/docker-tray/docker"
install -m 0644 com.github.spka.docker-tray.policy \
  "$root/usr/share/polkit-1/actions/com.github.spka.docker-tray.policy"
install -m 0755 docker-tray-launcher "$root/usr/bin/docker-tray"
install -m 0644 icon-dark.png icon-light.png "$root/usr/share/docker-tray/"
install -m 0644 icon-light.png "$root/usr/share/icons/hicolor/256x256/apps/docker-tray.png"
install -m 0644 README.md "$root/usr/share/doc/docker-tray/README.md"
install -m 0644 LICENSE "$root/usr/share/doc/docker-tray/copyright"
install -m 0644 docker-tray.service "$root/usr/lib/systemd/user/docker-tray.service"

install -m 0644 docker-tray.desktop "$root/usr/share/applications/docker-tray.desktop"
install -m 0644 docker-tray-autostart.desktop "$root/etc/xdg/autostart/docker-tray.desktop"

find "$root" -type d -exec chmod 0755 {} +
dpkg-deb --root-owner-group --build "$root" "dist/${package}_${version}_all.deb"

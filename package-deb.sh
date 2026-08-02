#!/usr/bin/env bash
set -euo pipefail

version="${1:-0.2.0}"
package="docker-tray"
root="dist/${package}_${version}_all"

rm -rf "$root"
mkdir -p \
  "$root/DEBIAN" \
  "$root/etc/xdg/autostart" \
  "$root/usr/bin" \
  "$root/usr/lib/docker-tray" \
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
Depends: python3, python3-pystray, python3-pil, python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1
Description: Small Docker tray utility
 Docker Tray shows Docker containers in the system tray and provides quick
 actions for opening exposed web ports, starting, stopping, and restarting
 services. Docker itself is not installed or managed by this package.
CONTROL

install -m 0755 docker_tray.py "$root/usr/lib/docker-tray/docker_tray.py"
install -m 0644 docker_tray_platform.py "$root/usr/lib/docker-tray/docker_tray_platform.py"
cat > "$root/usr/bin/docker-tray" <<'WRAPPER'
#!/usr/bin/env sh
exec python3 /usr/lib/docker-tray/docker_tray.py "$@"
WRAPPER
chmod 0755 "$root/usr/bin/docker-tray"
install -m 0644 icon-dark.png icon-light.png "$root/usr/share/docker-tray/"
install -m 0644 icon-light.png "$root/usr/share/icons/hicolor/256x256/apps/docker-tray.png"
install -m 0644 README.md "$root/usr/share/doc/docker-tray/README.md"
install -m 0644 LICENSE "$root/usr/share/doc/docker-tray/copyright"

cat > "$root/usr/share/applications/docker-tray.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Docker Tray
Exec=docker-tray
Icon=/usr/share/docker-tray/icon-light.png
Comment=Docker container monitor in the system tray
Categories=Utility;
Terminal=false
DESKTOP
chmod 0644 "$root/usr/share/applications/docker-tray.desktop"

cat > "$root/etc/xdg/autostart/docker-tray.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Docker Tray
Exec=docker-tray
Icon=/usr/share/docker-tray/icon-light.png
Comment=Docker container monitor in the system tray
Categories=Utility;
Terminal=false
Hidden=false
X-GNOME-Autostart-enabled=true
DESKTOP
chmod 0644 "$root/etc/xdg/autostart/docker-tray.desktop"

find "$root" -type d -exec chmod 0755 {} +
dpkg-deb --root-owner-group --build "$root" "dist/${package}_${version}_all.deb"

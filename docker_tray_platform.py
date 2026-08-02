import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


UBUNTU_DOCKER_INSTALL_URL = "https://docs.docker.com/engine/install/ubuntu/"
DEBIAN_DOCKER_INSTALL_URL = "https://docs.docker.com/engine/install/debian/"
ARCH_DOCKER_INSTALL_URL = "https://wiki.archlinux.org/title/Docker"
DOCKER_INSTALL_URL = "https://docs.docker.com/engine/install/"


@dataclass(frozen=True)
class PlatformInfo:
    distro_id: str
    distro_like: tuple[str, ...]
    desktop: tuple[str, ...]

    @property
    def is_arch_family(self):
        return "arch" in (self.distro_id, *self.distro_like)

    @property
    def is_debian_family(self):
        return bool({"debian", "ubuntu"} & {self.distro_id, *self.distro_like})

    @property
    def is_kde(self):
        return "kde" in self.desktop or os.environ.get("KDE_FULL_SESSION") == "true"

    @property
    def is_gnome(self):
        return "gnome" in self.desktop


@dataclass(frozen=True)
class EngineUpdate:
    available: bool
    detail: str = ""
    package_name: str = "Docker"
    upgrade_label: str | None = None
    upgrade_command: tuple[str, ...] | None = None

    @property
    def can_upgrade(self):
        return self.upgrade_command is not None


def get_platform_info():
    return PlatformInfo(
        distro_id=_read_os_release_value("ID"),
        distro_like=tuple(_read_os_release_value("ID_LIKE").split()),
        desktop=_read_desktop_names(),
    )


def get_docker_install_url(platform_info=None):
    platform_info = platform_info or get_platform_info()
    if platform_info.distro_id == "ubuntu":
        return UBUNTU_DOCKER_INSTALL_URL
    if platform_info.distro_id == "debian":
        return DEBIAN_DOCKER_INSTALL_URL
    if platform_info.is_arch_family:
        return ARCH_DOCKER_INSTALL_URL
    return DOCKER_INSTALL_URL


def check_engine_update(timeout, platform_info=None):
    provider = _get_update_provider(platform_info)
    if provider is None:
        return EngineUpdate(False)
    return provider.check(timeout)


def run_engine_upgrade(update, timeout=None):
    if not update.can_upgrade:
        raise RuntimeError("Docker Engine upgrade is not available on this platform")
    return subprocess.run(
        list(update.upgrade_command),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def is_dark_mode(Gio, platform_info=None):
    platform_info = platform_info or get_platform_info()
    if platform_info.is_gnome:
        return _is_gnome_dark_mode(Gio)
    if platform_info.is_kde:
        kde_dark = _is_kde_dark_mode()
        if kde_dark is not None:
            return kde_dark
    return _is_gnome_dark_mode(Gio)


def watch_theme(Gio, platform_info, on_changed):
    if not platform_info.is_gnome:
        return None
    try:
        settings = Gio.Settings.new("org.gnome.desktop.interface")
    except Exception:
        return None

    def _on_changed(settings, key):
        if key == "color-scheme":
            on_changed()

    settings.connect("changed", _on_changed)
    return settings


class AptDockerUpdateProvider:
    package_name = "Docker CE"

    def check(self, timeout):
        if shutil.which("apt") is None:
            return EngineUpdate(False)
        result = subprocess.run(
            ["apt", "list", "--upgradable"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        arch = linux_package_arch()
        for line in result.stdout.splitlines():
            if not line.startswith("docker-ce/"):
                continue
            detail = ""
            version_match = re.search(rf"\s(\S+)\s+{arch}", line)
            current_match = re.search(r"\[upgradable from: (\S+)\]", line)
            if version_match and current_match:
                new_ver = re.search(r":(\d+\.\d+\.\d+)", version_match.group(1))
                old_ver = re.search(r":(\d+\.\d+\.\d+)", current_match.group(1))
                if new_ver and old_ver:
                    detail = f"{old_ver.group(1)} -> {new_ver.group(1)}"
            return EngineUpdate(
                True,
                detail=detail,
                package_name=self.package_name,
                upgrade_label="Upgrade Docker CE",
                upgrade_command=("pkexec", "apt-get", "install", "-y", "--only-upgrade", "docker-ce"),
            )
        return EngineUpdate(False, package_name=self.package_name)


class PacmanDockerUpdateProvider:
    package_name = "Docker"

    def check(self, timeout):
        if shutil.which("pacman") is None:
            return EngineUpdate(False)
        result = subprocess.run(
            ["pacman", "-Qu", "docker"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode not in (0, 1):
            return EngineUpdate(False, package_name=self.package_name)
        for line in result.stdout.splitlines():
            if not line.startswith("docker "):
                continue
            detail = _parse_pacman_update_detail(line)
            return EngineUpdate(True, detail=detail, package_name=self.package_name)
        return EngineUpdate(False, package_name=self.package_name)


def _get_update_provider(platform_info=None):
    platform_info = platform_info or get_platform_info()
    if platform_info.is_arch_family:
        return PacmanDockerUpdateProvider()
    if platform_info.is_debian_family:
        return AptDockerUpdateProvider()
    return None


def _read_os_release_value(key):
    try:
        lines = Path("/etc/os-release").read_text().splitlines()
    except Exception:
        return ""
    prefix = f"{key}="
    for line in lines:
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip().strip('"').lower()
    return ""


def _read_desktop_names():
    names = []
    for key in ("XDG_CURRENT_DESKTOP", "DESKTOP_SESSION"):
        value = os.environ.get(key, "")
        names.extend(part.strip().lower() for part in value.split(":") if part.strip())
    return tuple(dict.fromkeys(names))


def linux_package_arch():
    machine = os.uname().machine
    if machine == "x86_64":
        return "amd64"
    if machine == "aarch64":
        return "arm64"
    return machine


def _parse_pacman_update_detail(line):
    match = re.search(r"\s(\S+)\s+->\s+(\S+)", line)
    if not match:
        return ""
    return f"{match.group(1)} -> {match.group(2)}"


def _is_gnome_dark_mode(Gio):
    try:
        settings = Gio.Settings.new("org.gnome.desktop.interface")
        return settings.get_string("color-scheme") == "prefer-dark"
    except Exception:
        return True


def _is_kde_dark_mode():
    path = Path.home() / ".config" / "kdeglobals"
    try:
        text = path.read_text()
    except Exception:
        return None

    color_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            color_section = line == "[Colors:Window]"
            continue
        if not color_section or not line.startswith("BackgroundNormal="):
            continue
        try:
            rgb = [int(part) for part in line.split("=", 1)[1].split(",")[:3]]
        except ValueError:
            return None
        return sum(rgb) / len(rgb) < 128
    return None

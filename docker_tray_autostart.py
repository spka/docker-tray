"""Read and write Docker Tray's XDG autostart preference."""


AUTOSTART_ENABLED_PREFIX = "X-GNOME-Autostart-enabled="
AUTOSTART_HIDDEN_PREFIX = "Hidden="


def read_enabled(user_file, system_file):
    for desktop_file in (user_file, system_file):
        if not desktop_file.exists():
            continue
        try:
            lines = desktop_file.read_text().splitlines()
        except Exception:
            continue
        for line in lines:
            if line.startswith(AUTOSTART_HIDDEN_PREFIX):
                return line.split("=", 1)[1].strip().lower() != "true"
            if line.startswith(AUTOSTART_ENABLED_PREFIX):
                return line.split("=", 1)[1].strip().lower() == "true"
        return desktop_file == system_file
    return False


def build_desktop(enabled, command):
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=Docker Tray",
        f"Exec={command}",
        "Icon=docker",
        "Comment=Docker container monitor in the system tray",
        f"{AUTOSTART_HIDDEN_PREFIX}{str(not enabled).lower()}",
        f"{AUTOSTART_ENABLED_PREFIX}{str(enabled).lower()}",
        "",
    ])


def write_enabled(user_file, enabled, command):
    user_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = user_file.read_text().splitlines()
    except FileNotFoundError:
        lines = build_desktop(enabled, command).splitlines()
    except Exception:
        return False
    else:
        replacements = {
            AUTOSTART_ENABLED_PREFIX: f"{AUTOSTART_ENABLED_PREFIX}{str(enabled).lower()}",
            AUTOSTART_HIDDEN_PREFIX: f"{AUTOSTART_HIDDEN_PREFIX}{str(not enabled).lower()}",
            "Exec=": f"Exec={command}",
        }
        found = set()
        for index, line in enumerate(lines):
            for prefix, replacement in replacements.items():
                if line.startswith(prefix):
                    lines[index] = replacement
                    found.add(prefix)
                    break
        for prefix, replacement in replacements.items():
            if prefix not in found:
                lines.append(replacement)
    user_file.write_text("\n".join(lines).rstrip() + "\n")
    return True

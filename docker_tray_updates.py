"""Pure helpers for Docker Tray application and image update checks."""

import re
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class LocalImageMetadata:
    image_id: str
    registry_backed: bool


class ImageUpdateCheckError(RuntimeError):
    """An incomplete registry scan with usable results for other images."""

    def __init__(self, updates, failures):
        self.updates = sorted(updates)
        self.failures = dict(failures)
        super().__init__("; ".join(f"{image}: {error}" for image, error in failures.items()))


def get_manifest_config_digest(manifest):
    for key in ("SchemaV2Manifest", "OCIManifest"):
        digest = manifest.get(key, {}).get("config", {}).get("digest")
        if digest:
            return digest
    return manifest.get("config", {}).get("digest")


def is_image_id_reference(image):
    image = image.strip()
    if image.startswith("sha256:"):
        image = image.removeprefix("sha256:")
    return bool(re.fullmatch(r"[0-9a-fA-F]{12,64}", image))


def is_checkable_image(image):
    return "@" not in image and not is_image_id_reference(image)


def parse_app_version(version):
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", version.strip())
    if not match:
        raise ValueError(f"Unsupported version: {version}")
    return tuple(int(part or 0) for part in match.groups())


def format_relative_time(timestamp, now=None):
    if timestamp is None:
        return "not checked yet"
    elapsed = max(0, int((time.time() if now is None else now) - timestamp))
    if elapsed < 60:
        return "just now"
    if elapsed < 3600:
        minutes = elapsed // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = elapsed // 3600
    return f"{hours} hour{'s' if hours != 1 else ''} ago"

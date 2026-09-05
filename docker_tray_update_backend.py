"""Docker commands, registry requests and release installation for updates.

This boundary performs I/O but owns no UI or update-check state. Tests can
replace it entirely, or inject its subprocess and HTTP implementations.
"""

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

import docker_tray_platform
import docker_tray_runtime
from docker_tray_commands import (
    COMMAND_ERROR_DETAIL_MAX_CHARS,
    get_command_failure_detail,
    get_authorization_failure_detail,
)
from docker_tray_updates import (
    AppUpdate,
    LocalImageMetadata,
    get_manifest_config_digest,
    parse_app_version,
)

APP_LATEST_RELEASE_API_URL = "https://api.github.com/repos/spka/docker-tray/releases/latest"
APP_RELEASES_URL = "https://github.com/spka/docker-tray/releases"
APP_RELEASE_DOWNLOAD_URL_PREFIX = f"{APP_RELEASES_URL}/download/"
APP_UPDATE_TIMEOUT_SECONDS = 10
APP_UPGRADE_TIMEOUT_SECONDS = 10 * 60
DOCKER_IMAGE_METADATA_FORMAT = "{{.Id}}\t{{json .RepoDigests}}"
DOCKER_CMD_TIMEOUT_SECONDS = 15
DOCKER_MANIFEST_TIMEOUT_SECONDS = 30
DOCKER_ENGINE_UPGRADE_TIMEOUT_SECONDS = 30 * 60
DOCKER_IMAGE_UPDATE_TIMEOUT_SECONDS = 15 * 60


class UpdateBackend:
    def __init__(
        self,
        version,
        platform_info,
        *,
        run=None,
        open_url=None,
        docker_command=None,
        privileged_command=None,
    ):
        self.version = version
        self.platform_info = platform_info
        self.run = run or subprocess.run
        self.urlopen = open_url or urlopen
        self.docker_command = docker_command or docker_tray_runtime.docker_command
        self.privileged_command = privileged_command or docker_tray_runtime.privileged_command

    def run_docker_capture(self, args, check=True):
        return self.run(
            args, capture_output=True, text=True, check=check, timeout=DOCKER_CMD_TIMEOUT_SECONDS
        )

    def output_line_set(self, output):
        return {line.strip() for line in output.splitlines() if line.strip()}

    def check_engine_update(self):
        return docker_tray_platform.check_engine_update(
            DOCKER_CMD_TIMEOUT_SECONDS, self.platform_info
        )

    def get_local_image_metadata(self, images):
        images = list(images)
        if not images:
            return {}
        result = self.run(
            self.docker_command(
                "image", "inspect", "--format", DOCKER_IMAGE_METADATA_FORMAT, *images
            ),
            capture_output=True,
            text=True,
            timeout=DOCKER_CMD_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "image inspection failed"
            raise RuntimeError(detail)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != len(images):
            raise RuntimeError("Docker returned an incomplete image inspection result")
        metadata = {}
        for image, line in zip(images, lines):
            parts = line.split("\t", 1)
            if len(parts) != 2:
                raise RuntimeError("Docker returned invalid image metadata")
            image_id, raw_repo_digests = parts
            try:
                repo_digests = json.loads(raw_repo_digests)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError("Docker returned invalid repository digests") from error
            metadata[image] = LocalImageMetadata(
                image_id=image_id,
                registry_backed=isinstance(repo_digests, list) and bool(repo_digests),
            )
        return metadata

    def get_container_image_refs(self):
        ids_result = self.run_docker_capture(self.docker_command("ps", "-a", "-q"), check=False)
        if ids_result.returncode != 0:
            raise RuntimeError(get_command_failure_detail(result=ids_result))
        container_ids = sorted(self.output_line_set(ids_result.stdout))
        if not container_ids:
            return []
        result = self.run_docker_capture(
            self.docker_command(
                "inspect", "--format", "{{.Config.Image}}\t{{.Image}}", *container_ids
            ),
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(get_command_failure_detail(result=result))
        refs = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            image, container_image_id = parts
            image = image.strip()
            container_image_id = container_image_id.strip()
            if image:
                refs.append((image, container_image_id))
        return refs

    def get_remote_config_digest(self, image):
        result = self.run(
            self.docker_command("manifest", "inspect", "--verbose", image),
            capture_output=True,
            text=True,
            timeout=DOCKER_MANIFEST_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "registry request failed"
            raise RuntimeError(f"{image}: {detail}")
        try:
            data = json.loads(result.stdout)
        except Exception as error:
            raise RuntimeError(f"{image}: invalid registry response") from error
        arch = docker_tray_platform.linux_package_arch()
        if isinstance(data, list):
            for entry in data:
                p = entry.get("Descriptor", {}).get("platform", {})
                if p.get("architecture") == arch and p.get("os") == "linux":
                    return get_manifest_config_digest(entry)
            return None
        return get_manifest_config_digest(data)

    def check_app_update(self):
        request = Request(
            APP_LATEST_RELEASE_API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"docker-tray/{self.version}",
            },
        )
        with self.urlopen(request, timeout=APP_UPDATE_TIMEOUT_SECONDS) as response:
            release = json.loads(response.read())
        tag_name = release.get("tag_name", "")
        latest_version = tag_name.removeprefix("v")
        if parse_app_version(latest_version) <= parse_app_version(self.version):
            return AppUpdate(False)
        release_url = release.get("html_url", "")
        if not release_url.startswith(f"{APP_RELEASES_URL}/"):
            release_url = f"{APP_RELEASES_URL}/tag/{tag_name}"
        package_url = ""
        package_digest = ""
        expected_package_name = f"docker-tray_{latest_version}_all.deb"
        for asset in release.get("assets") or []:
            if asset.get("name") != expected_package_name:
                continue
            candidate_url = asset.get("browser_download_url", "")
            if not candidate_url.startswith(APP_RELEASE_DOWNLOAD_URL_PREFIX):
                continue
            candidate_digest = asset.get("digest") or ""
            if candidate_digest and (not re.fullmatch("sha256:[0-9a-fA-F]{64}", candidate_digest)):
                continue
            package_url = candidate_url
            package_digest = candidate_digest.lower()
            break
        return AppUpdate(
            True,
            latest_version=latest_version,
            release_url=release_url,
            package_url=package_url,
            package_digest=package_digest,
            install_supported=self.platform_info.is_debian_family,
        )

    def download_app_update(self, update, destination_dir):
        if not update.package_url.startswith(APP_RELEASE_DOWNLOAD_URL_PREFIX):
            raise RuntimeError("The release package URL is not trusted")
        package_name = f"docker-tray_{update.latest_version}_all.deb"
        package_path = Path(destination_dir) / package_name
        request = Request(update.package_url, headers={"User-Agent": f"docker-tray/{self.version}"})
        digest = hashlib.sha256()
        with self.urlopen(request, timeout=APP_UPGRADE_TIMEOUT_SECONDS) as response:
            with package_path.open("wb") as package_file:
                while chunk := response.read(64 * 1024):
                    package_file.write(chunk)
                    digest.update(chunk)
        package_path.chmod(420)
        actual_digest = f"sha256:{digest.hexdigest()}"
        if update.package_digest and actual_digest != update.package_digest:
            package_path.unlink(missing_ok=True)
            raise RuntimeError("The downloaded package checksum does not match the release")
        self.validate_app_update_package(package_path, update.latest_version)
        return package_path

    def validate_app_update_package(self, package_path, expected_version):
        result = self.run(
            ["dpkg-deb", "--field", str(package_path), "Package", "Version", "Architecture"],
            capture_output=True,
            text=True,
            timeout=APP_UPDATE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "invalid Debian package"
            raise RuntimeError(detail)
        fields = {}
        for line in result.stdout.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()
        if fields != {"Package": "docker-tray", "Version": expected_version, "Architecture": "all"}:
            raise RuntimeError("The downloaded package metadata does not match the release")

    def run_app_upgrade(self, update):
        if not update.can_install or not self.platform_info.is_debian_family:
            raise RuntimeError("Automatic Docker Tray upgrades are not available on this platform")
        with tempfile.TemporaryDirectory(prefix="docker-tray-update-") as temp_dir:
            Path(temp_dir).chmod(493)
            package_path = self.download_app_update(update, temp_dir)
            return self.run(
                self.privileged_command(
                    "install-update",
                    str(package_path),
                    update.latest_version,
                    update.package_digest,
                ),
                capture_output=True,
                text=True,
                timeout=APP_UPGRADE_TIMEOUT_SECONDS,
            )

    def run_privileged_image_updates(self, images):
        images = list(images)
        result = self.run(
            self.privileged_command("image-update", *images),
            capture_output=True,
            text=True,
            timeout=DOCKER_IMAGE_UPDATE_TIMEOUT_SECONDS * max(1, len(images)),
        )
        outcomes = {}
        for line in result.stdout.splitlines():
            try:
                message = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            image = message.get("image") if isinstance(message, dict) else None
            if image not in images or not isinstance(message.get("success"), bool):
                continue
            if message["success"]:
                outcomes[image] = {
                    "success": True,
                    "error": "",
                    "service_count": int(message.get("service_count", 0)),
                    "removed_image_count": int(message.get("removed_image_count", 0)),
                    "cleanup_error": str(message.get("cleanup_error", "")),
                }
            else:
                outcomes[image] = {
                    "success": False,
                    "error": str(message.get("error", "Image update failed")),
                    "service_count": 0,
                    "removed_image_count": 0,
                    "cleanup_error": "",
                }
        if result.returncode != 0 and (not outcomes):
            missing_detail = get_authorization_failure_detail(
                result, "The privileged image update helper did not return a result"
            )
        else:
            missing_detail = (
                result.stderr.strip()
                or (result.stdout.strip() if not outcomes else "")
                or "The privileged image update helper did not return a result"
            )
        if len(missing_detail) > COMMAND_ERROR_DETAIL_MAX_CHARS:
            missing_detail = missing_detail[:COMMAND_ERROR_DETAIL_MAX_CHARS].rstrip() + "…"
        for image in images:
            outcomes.setdefault(
                image,
                {
                    "success": False,
                    "error": missing_detail,
                    "service_count": 0,
                    "removed_image_count": 0,
                    "cleanup_error": "",
                },
            )
        return outcomes

    def run_engine_upgrade(self, update):
        return docker_tray_platform.run_engine_upgrade(
            update, timeout=DOCKER_ENGINE_UPGRADE_TIMEOUT_SECONDS
        )

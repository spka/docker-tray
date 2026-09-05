#!/usr/bin/python3
"""Narrow PolicyKit broker for Docker Tray.

This file is installed root-owned. It accepts only the Docker CLI shapes used
by Docker Tray, separating unattended read-only queries from authenticated
state-changing operations.
"""

import ctypes
import http.client
import hashlib
import json
import os
import pwd
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


DOCKER = "/usr/bin/docker"
DOCKER_SOCKET = "/var/run/docker.sock"
APT_GET = "/usr/bin/apt-get"
DPKG_DEB = "/usr/bin/dpkg-deb"
MAX_UPDATE_PACKAGE_BYTES = 100 * 1024 * 1024
UPDATE_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?\Z")
UPDATE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
WATCH_INTERVAL_SECONDS = 5
PR_SET_PDEATHSIG = 1
COMPOSE_PULL_TIMEOUT_SECONDS = 10 * 60
COMPOSE_UP_TIMEOUT_SECONDS = 3 * 60
COMPOSE_READY_TIMEOUT_SECONDS = 60
COMPOSE_READY_POLL_SECONDS = 2
MAX_IMAGE_UPDATE_IMAGES = 32
COMPOSE_FILE_NAMES = {
    "compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml",
}
CONTAINER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
OBJECT_ID_RE = re.compile(r"(?:sha256:)?[0-9a-f]{12,64}\Z")
IMAGE_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,511}\Z")
FULL_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
IMAGE_METADATA_FORMAT = "{{.Id}}\t{{json .RepoDigests}}"

PS_CONTAINERS_FORMAT = "{{.Names}}\t{{.Status}}\t{{.Ports}}"
PS_COMPOSE_FORMAT = (
    "{{.Names}}\t{{.Status}}\t"
    "{{.Label \"com.docker.compose.project.config_files\"}}\t"
    "{{.Label \"com.docker.compose.project.working_dir\"}}"
)
INSPECT_COMPOSE_LABEL_FORMAT = (
    "{{.Config.Image}}\t{{.Image}}\t"
    "{{index .Config.Labels \"com.docker.compose.project.config_files\"}}\t"
    "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}\t"
    "{{index .Config.Labels \"com.docker.compose.service\"}}"
)
INSPECT_COMPOSE_STATE_FORMAT = (
    "{{.Config.Image}}\t{{.Image}}\t{{.State.Running}}\t"
    "{{if .State.Health}}{{.State.Health.Status}}{{end}}\t"
    "{{index .Config.Labels \"com.docker.compose.project.config_files\"}}\t"
    "{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}\t"
    "{{index .Config.Labels \"com.docker.compose.service\"}}"
)

READ_EXACT = {
    ("ps", "-a", "--format", PS_CONTAINERS_FORMAT),
    ("ps", "-a", "--format", PS_COMPOSE_FORMAT),
    ("ps", "-a", "-q"),
    ("ps", "--format", "{{.Names}}\t{{.RunningFor}}"),
    ("ps", "-a", "--filter", "status=exited", "--filter", "status=created", "-q"),
    ("images", "--filter", "dangling=true", "--no-trunc", "-q"),
    ("network", "ls", "--filter", "dangling=true", "-q"),
    ("system", "df", "--format", "{{.Type}}\t{{.Reclaimable}}"),
    ("stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"),
}
INSPECT_FORMATS = {
    "{{.Image}}",
    "{{.Name}}\t{{.RestartCount}}",
    "{{.Config.Image}}\t{{.Image}}",
    INSPECT_COMPOSE_LABEL_FORMAT,
    INSPECT_COMPOSE_STATE_FORMAT,
}
PRUNE_COMMAND_ORDER = (
    ("container", "prune", "-f"),
    ("image", "prune", "-f"),
    ("network", "prune", "-f"),
    ("builder", "prune", "-f"),
)


def fail(message):
    print(f"docker-tray: {message}", file=sys.stderr)
    raise SystemExit(2)


def invoking_user():
    value = os.environ.get("PKEXEC_UID")
    if value is None or not value.isdigit():
        fail("must be launched through pkexec")
    return pwd.getpwuid(int(value))


def path_within(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_context(raw_cwd, user):
    home = Path(user.pw_dir).resolve()
    cwd = Path(raw_cwd).resolve()
    if not cwd.is_dir() or not path_within(cwd, home):
        fail("working directory must be inside the invoking user's home")
    return cwd, home


def valid_name(value):
    return bool(CONTAINER_NAME_RE.fullmatch(value))


def valid_id(value):
    return bool(OBJECT_ID_RE.fullmatch(value))


def valid_image(value):
    return bool(IMAGE_REF_RE.fullmatch(value))


def validate_read(args):
    command = tuple(args)
    if command in READ_EXACT:
        return
    if len(args) >= 4 and args[:2] == ["inspect", "--format"]:
        if args[2] in INSPECT_FORMATS and all(valid_id(value) for value in args[3:]):
            return
    if len(args) >= 5 and args[:3] == ["image", "inspect", "--format"]:
        if args[3] in {"{{.Id}}", IMAGE_METADATA_FORMAT} and all(
            valid_image(image) for image in args[4:]
        ):
            return
    fail("read command is not permitted")


def validate_compose(args, home):
    index = 1
    files = []
    while index + 1 < len(args) and args[index] == "-f":
        candidate = Path(args[index + 1]).resolve()
        if (
            not candidate.is_file()
            or not path_within(candidate, home)
            or candidate.name not in COMPOSE_FILE_NAMES
        ):
            fail("Compose files must be regular files inside the invoking user's home")
        files.append(candidate)
        index += 2
    if not files or index >= len(args):
        fail("a Compose file and action are required")
    remainder = args[index:]
    if remainder == ["up", "-d"]:
        return
    if len(remainder) == 3 and remainder[:2] == ["up", "-d"] and valid_name(remainder[2]):
        return
    fail("Compose action is not permitted")


def normalize_template_value(value):
    value = value.strip()
    return "" if value == "<no value>" else value


def output_lines(output):
    return [line.strip() for line in output.splitlines() if line.strip()]


def run_docker(args, cwd=None, timeout=60):
    return subprocess.run(
        [DOCKER, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def command_error(result, fallback):
    detail = result.stderr.strip() or result.stdout.strip() or fallback
    return detail[:500]


def resolve_compose_files(raw_files, raw_working_dir, home):
    raw_working_path = Path(raw_working_dir)
    if not raw_working_dir or not raw_working_path.is_absolute():
        return None
    working_dir = raw_working_path.resolve()
    if not working_dir.is_dir() or not path_within(working_dir, home):
        return None
    files = []
    for raw_file in raw_files.split(","):
        raw_file = raw_file.strip()
        if not raw_file:
            continue
        candidate = Path(raw_file)
        if not candidate.is_absolute():
            candidate = working_dir / candidate
        candidate = candidate.resolve()
        if (
            not candidate.is_file()
            or not path_within(candidate, home)
            or candidate.name not in COMPOSE_FILE_NAMES
        ):
            return None
        files.append(candidate)
    return (tuple(files), working_dir) if files else None


def discover_image_update(image, home):
    ids_result = run_docker(["ps", "-a", "-q"])
    if ids_result.returncode != 0:
        raise RuntimeError(command_error(ids_result, "docker ps failed"))
    container_ids = [value for value in output_lines(ids_result.stdout) if valid_id(value)]
    if not container_ids:
        return [], set()

    inspect_result = run_docker([
        "inspect", "--format", INSPECT_COMPOSE_LABEL_FORMAT, *container_ids,
    ])
    if inspect_result.returncode != 0:
        raise RuntimeError(command_error(inspect_result, "docker inspect failed"))

    targets = []
    replaced_image_ids = set()
    seen = set()
    for line in inspect_result.stdout.splitlines():
        parts = line.split("\t", 4)
        if len(parts) != 5:
            continue
        config_image, container_image_id, raw_files, raw_working_dir, service = (
            normalize_template_value(part) for part in parts
        )
        if config_image == image and FULL_IMAGE_ID_RE.fullmatch(container_image_id):
            replaced_image_ids.add(container_image_id)
        if image not in {config_image, container_image_id} or not valid_name(service):
            continue
        resolved = resolve_compose_files(raw_files, raw_working_dir, home)
        if resolved is None:
            continue
        files, working_dir = resolved
        key = (files, working_dir, service)
        if key not in seen:
            seen.add(key)
            targets.append(key)
    return targets, replaced_image_ids


def compose_args(files):
    args = ["compose"]
    for compose_file in files:
        args.extend(("-f", str(compose_file)))
    return args


def compose_service_ready(files, working_dir, service, image):
    prefix = compose_args(files)
    ids_result = run_docker(
        [*prefix, "ps", "--all", "-q", service], cwd=working_dir
    )
    if ids_result.returncode != 0:
        return False
    container_ids = [value for value in output_lines(ids_result.stdout) if valid_id(value)]
    if not container_ids:
        return False

    image_result = run_docker([
        "image", "inspect", "--format", "{{.Id}}", image,
    ])
    if image_result.returncode != 0:
        return False
    expected_image_id = image_result.stdout.strip()
    state_format = (
        "{{.Image}}\t{{.State.Running}}\t"
        "{{if .State.Health}}{{.State.Health.Status}}{{end}}"
    )
    state_result = run_docker([
        "inspect", "--format", state_format, *container_ids,
    ])
    if state_result.returncode != 0:
        return False
    # An empty health field is valid; stripping whitespace would remove its tab.
    states = [line for line in state_result.stdout.splitlines() if line.strip()]
    if len(states) != len(container_ids):
        return False
    for line in states:
        parts = line.split("\t", 2)
        if len(parts) != 3:
            return False
        image_id, running, health = parts
        if image_id != expected_image_id or running != "true":
            return False
        if health and health != "healthy":
            return False
    return True


def wait_for_compose_service(files, working_dir, service, image):
    deadline = time.monotonic() + COMPOSE_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if compose_service_ready(files, working_dir, service, image):
            return True
        time.sleep(COMPOSE_READY_POLL_SECONDS)
    return False


def get_used_image_ids():
    ids_result = run_docker(["ps", "-a", "-q"])
    if ids_result.returncode != 0:
        raise RuntimeError(command_error(ids_result, "docker ps failed"))
    container_ids = [value for value in output_lines(ids_result.stdout) if valid_id(value)]
    if not container_ids:
        return set()
    inspect_result = run_docker([
        "inspect", "--format", "{{.Image}}", *container_ids,
    ])
    if inspect_result.returncode != 0:
        raise RuntimeError(command_error(inspect_result, "docker inspect failed"))
    return {
        value for value in output_lines(inspect_result.stdout)
        if FULL_IMAGE_ID_RE.fullmatch(value)
    }


def write_operation_message(message):
    print(json.dumps(message, separators=(",", ":")), flush=True)


def update_compose_image(image, home):
    targets, replaced_image_ids = discover_image_update(image, home)
    if not targets:
        raise RuntimeError(f"No authorized Compose service was found for {image}")

    for files, working_dir, service in targets:
        prefix = compose_args(files)
        write_operation_message({"image": image, "status": f"Pulling {image} for {service}..."})
        pull_result = run_docker(
            [*prefix, "pull", service],
            cwd=working_dir,
            timeout=COMPOSE_PULL_TIMEOUT_SECONDS,
        )
        if pull_result.returncode != 0:
            raise RuntimeError(command_error(pull_result, f"Pull failed for {image}"))

        write_operation_message({"image": image, "status": f"Restarting {service}..."})
        up_result = run_docker(
            [*prefix, "up", "-d", service],
            cwd=working_dir,
            timeout=COMPOSE_UP_TIMEOUT_SECONDS,
        )
        if up_result.returncode != 0:
            raise RuntimeError(command_error(up_result, f"Restart failed for {service}"))

        write_operation_message({
            "image": image,
            "status": f"Waiting for {service} to finish restarting...",
        })
        if not wait_for_compose_service(files, working_dir, service, image):
            raise RuntimeError(
                f"{service} was not ready after {COMPOSE_READY_TIMEOUT_SECONDS} seconds"
            )

    removed_count = 0
    cleanup_errors = []
    try:
        removable_ids = sorted(replaced_image_ids - get_used_image_ids())
        for image_id in removable_ids:
            remove_result = run_docker(["image", "rm", image_id])
            if remove_result.returncode == 0:
                removed_count += 1
            else:
                cleanup_errors.append(command_error(remove_result, f"Could not remove {image_id}"))
    except Exception as error:
        cleanup_errors.append(str(error)[:500])
    return len(targets), removed_count, "; ".join(cleanup_errors)


def run_image_updates(images, home):
    failed = False
    for image in images:
        try:
            service_count, removed_count, cleanup_error = update_compose_image(image, home)
            write_operation_message({
                "image": image,
                "success": True,
                "service_count": service_count,
                "removed_image_count": removed_count,
                "cleanup_error": cleanup_error,
            })
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            failed = True
            write_operation_message({
                "image": image,
                "success": False,
                "error": str(error)[:500],
            })
    raise SystemExit(1 if failed else 0)


def validate_write(args, home):
    if len(args) == 2 and args[0] in {"start", "stop", "restart"} and valid_name(args[1]):
        return
    if args and args[0] == "compose":
        validate_compose(args, home)
        return
    fail("write command is not permitted")


def parse_deb_fields(output):
    fields = {}
    for line in output.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    return fields


def validate_update_metadata(package_path, expected_version):
    result = subprocess.run(
        [DPKG_DEB, "--field", str(package_path), "Package", "Version", "Architecture"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        fail(result.stderr.strip() or result.stdout.strip() or "invalid Debian package")
    expected = {
        "Package": "docker-tray",
        "Version": expected_version,
        "Architecture": "all",
    }
    if parse_deb_fields(result.stdout) != expected:
        fail("update package metadata does not match the release")


def stage_update_package(raw_path, expected_version, expected_digest, user, destination):
    if not UPDATE_VERSION_RE.fullmatch(expected_version):
        fail("invalid update version")
    if not UPDATE_DIGEST_RE.fullmatch(expected_digest):
        fail("invalid update digest")

    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(raw_path, flags)
    except OSError as error:
        fail(f"cannot open update package: {error}")

    digest = hashlib.sha256()
    try:
        metadata = os.fstat(source_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != user.pw_uid
            or metadata.st_size <= 0
            or metadata.st_size > MAX_UPDATE_PACKAGE_BYTES
        ):
            fail("update package ownership, type, or size is invalid")
        with os.fdopen(source_fd, "rb", closefd=False) as source:
            with destination.open("xb") as target:
                while chunk := source.read(64 * 1024):
                    target.write(chunk)
                    digest.update(chunk)
    finally:
        os.close(source_fd)

    destination.chmod(0o644)
    if f"sha256:{digest.hexdigest()}" != expected_digest:
        fail("update package checksum does not match the release")
    validate_update_metadata(destination, expected_version)


def install_update(raw_path, expected_version, expected_digest, user):
    with tempfile.TemporaryDirectory(prefix="docker-tray-install-", dir="/var/tmp") as directory:
        directory_path = Path(directory)
        directory_path.chmod(0o755)
        package_path = directory_path / f"docker-tray_{expected_version}_all.deb"
        stage_update_package(raw_path, expected_version, expected_digest, user, package_path)
        result = subprocess.run([APT_GET, "install", "-y", str(package_path)], timeout=600)
        raise SystemExit(result.returncode)


def run_cleanup():
    output = []
    for args in PRUNE_COMMAND_ORDER:
        result = subprocess.run(
            [DOCKER, *args], capture_output=True, text=True, timeout=60
        )
        command_text = "docker " + " ".join(args)
        detail = "\n".join(
            value.strip() for value in (result.stdout, result.stderr) if value.strip()
        ) or "Done"
        output.append(f"$ {command_text}\n{detail}")
        if result.returncode != 0:
            print("\n\n".join(output))
            raise SystemExit(result.returncode)
    print("\n\n".join(output))


class DockerUnixConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=10)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(DOCKER_SOCKET)


def parse_container_api_data(data):
    containers = []
    for item in data:
        names = item.get("Names") or []
        if not names:
            continue
        name = str(names[0]).lstrip("/")
        if not valid_name(name):
            continue
        port = None
        for binding in item.get("Ports") or []:
            if (
                binding.get("Type") == "tcp"
                and binding.get("IP") in {"0.0.0.0", "127.0.0.1"}
                and isinstance(binding.get("PublicPort"), int)
            ):
                port = str(binding["PublicPort"])
                break
        containers.append((name, item.get("State") == "running", port))
    return sorted(containers, key=lambda container: container[0].lower())


def get_container_api_snapshot():
    connection = DockerUnixConnection()
    try:
        connection.request("GET", "/containers/json?all=1")
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise RuntimeError(f"Docker API returned HTTP {response.status}")
        data = json.loads(body)
        if not isinstance(data, list):
            raise RuntimeError("Docker API returned an invalid container list")
        return parse_container_api_data(data)
    finally:
        connection.close()


def write_watch_message(message):
    try:
        print(json.dumps(message, separators=(",", ":")), flush=True)
    except BrokenPipeError:
        raise SystemExit(0)


def terminate_when_parent_exits():
    parent_pid = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if os.getppid() != parent_pid:
        raise SystemExit(0)


def watch_container_snapshots():
    while True:
        try:
            containers = get_container_api_snapshot()
            write_watch_message({"containers": containers})
        except (OSError, ValueError, RuntimeError) as error:
            detail = str(error).strip() or type(error).__name__
            write_watch_message({"error": detail[:300]})
        time.sleep(WATCH_INTERVAL_SECONDS)


def parse_request(argv):
    if len(argv) < 6 or argv[2] != "--cwd" or argv[4] != "--" or not argv[5:]:
        fail("invalid request")
    return argv[1], argv[3], argv[5:]


def main(argv=None):
    argv = argv or sys.argv
    if os.geteuid() != 0:
        fail("broker must run as root")
    mode = argv[1] if len(argv) > 1 else ""
    user = invoking_user()
    if mode == "watch":
        if len(argv) != 2:
            fail("invalid watch request")
        terminate_when_parent_exits()
        watch_container_snapshots()
        return
    if mode == "cleanup":
        if len(argv) != 2:
            fail("invalid cleanup request")
        run_cleanup()
        return
    if mode == "install-update":
        if len(argv) != 5:
            fail("invalid update request")
        install_update(argv[2], argv[3], argv[4], user)
        return
    if mode == "image-update":
        images = argv[2:]
        if (
            not images
            or len(images) > MAX_IMAGE_UPDATE_IMAGES
            or len(set(images)) != len(images)
            or any(not valid_image(image) or "@" in image for image in images)
        ):
            fail("invalid image update request")
        run_image_updates(images, Path(user.pw_dir).resolve())
        return
    mode, raw_cwd, args = parse_request(argv)
    cwd, home = validate_context(raw_cwd, user)
    if mode == "read":
        validate_read(args)
    elif mode == "write":
        validate_write(args, home)
    else:
        fail("invalid authorization mode")
    result = subprocess.run([DOCKER, *args], cwd=cwd)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()

#!/usr/bin/python3
"""Narrow PolicyKit broker for Docker Tray.

This file is installed root-owned. It accepts only the Docker CLI shapes used
by Docker Tray, separating unattended read-only queries from authenticated
state-changing operations.
"""

import http.client
import json
import os
import pwd
import re
import socket
import subprocess
import sys
import time
from pathlib import Path


DOCKER = "/usr/bin/docker"
DOCKER_SOCKET = "/var/run/docker.sock"
WATCH_INTERVAL_SECONDS = 5
COMPOSE_FILE_NAMES = {
    "compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml",
}
CONTAINER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
OBJECT_ID_RE = re.compile(r"(?:sha256:)?[0-9a-f]{12,64}\Z")
IMAGE_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,511}\Z")

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
PRUNE_COMMANDS = {
    ("container", "prune", "-f"),
    ("image", "prune", "-f"),
    ("network", "prune", "-f"),
    ("builder", "prune", "-f"),
}


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
    if len(args) == 5 and args[:3] == ["image", "inspect", "--format"]:
        if args[3] == "{{.Id}}" and valid_image(args[4]):
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
    if len(remainder) == 2 and remainder[0] == "pull" and valid_name(remainder[1]):
        return
    if remainder == ["up", "-d"]:
        return
    if len(remainder) == 3 and remainder[:2] == ["up", "-d"] and valid_name(remainder[2]):
        return
    fail("Compose action is not permitted")


def validate_write(args, home):
    command = tuple(args)
    if len(args) == 2 and args[0] in {"start", "stop", "restart"} and valid_name(args[1]):
        return
    if command in PRUNE_COMMANDS:
        return
    if len(args) == 3 and args[:2] == ["image", "rm"] and valid_id(args[2]):
        return
    if args and args[0] == "compose":
        validate_compose(args, home)
        return
    fail("write command is not permitted")


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
        watch_container_snapshots()
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

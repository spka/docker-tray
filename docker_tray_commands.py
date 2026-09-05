"""Shared command failure messages for Docker Tray."""

import subprocess

COMMAND_ERROR_DETAIL_MAX_CHARS = 500


def format_docker_error(message):
    lower = message.lower()
    if "permission denied" in lower and "docker.sock" in lower:
        return "Docker socket permission denied. Reinstall Docker Tray's PolicyKit helper."
    if "no such file or directory" in lower and "docker.sock" in lower:
        return "Docker daemon is not running. Start docker.service."
    if "cannot connect to the docker daemon" in lower:
        return "Docker daemon is not running."
    if any(
        marker in lower
        for marker in (
            "request dismissed",
            "authentication dialog was dismissed",
        )
    ):
        return "Authorization was cancelled. Docker Tray will retry automatically."
    if "not authorized" in lower:
        return "Authorization was denied. Docker Tray will retry automatically."
    return message


def get_command_failure_detail(result=None, error=None):
    if isinstance(error, subprocess.TimeoutExpired):
        detail = f"timed out after {error.timeout} seconds"
    elif error is not None:
        detail = f"{type(error).__name__}: {error}"
    elif result is not None:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
    else:
        detail = "unknown failure"
    detail = format_docker_error(detail)
    if len(detail) > COMMAND_ERROR_DETAIL_MAX_CHARS:
        return detail[:COMMAND_ERROR_DETAIL_MAX_CHARS].rstrip() + "…"
    return detail


def get_authorization_failure_detail(result, fallback="privileged operation failed"):
    detail = result.stderr.strip() or result.stdout.strip() or fallback
    lower = detail.lower()
    if result.returncode == 126 or any(
        marker in lower
        for marker in (
            "request dismissed",
            "authentication dialog was dismissed",
        )
    ):
        return "Authorization was cancelled. No changes were made."
    if "not authorized" in lower:
        return "Authorization was denied. No changes were made."
    if result.returncode == 127:
        return "Authorization failed. No changes were made."
    return get_command_failure_detail(result=result)

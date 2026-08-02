import subprocess
import unittest
from unittest import mock

import docker_tray


class ImmediateThread:
    def __init__(self, target, args=(), **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


def run_idle_callback(callback, *args):
    return callback(*args)


class DockerActionTests(unittest.TestCase):
    @mock.patch.object(docker_tray, "update_tray_menu")
    @mock.patch.object(docker_tray.threading, "Thread", ImmediateThread)
    @mock.patch.object(docker_tray.subprocess, "run")
    def test_failed_container_action_notifies_user(self, run, update_menu):
        run.return_value = subprocess.CompletedProcess(
            args=["docker", "stop", "web"],
            returncode=1,
            stdout="",
            stderr="permission denied",
        )
        icon = mock.Mock()

        docker_tray.run_docker_action("stop", "web", icon)

        icon.notify.assert_called_once_with(
            "Could not stop web: permission denied",
            "Docker Tray",
        )
        update_menu.assert_called_once_with(icon)

    @mock.patch.object(docker_tray, "update_tray_menu")
    @mock.patch.object(docker_tray.threading, "Thread", ImmediateThread)
    @mock.patch.object(
        docker_tray.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(["docker", "restart", "web"], 15),
    )
    def test_timed_out_container_action_notifies_user(self, _run, update_menu):
        icon = mock.Mock()

        docker_tray.run_docker_action("restart", "web", icon)

        icon.notify.assert_called_once_with(
            "Could not restart web: timed out after 15 seconds",
            "Docker Tray",
        )
        update_menu.assert_called_once_with(icon)

    @mock.patch.object(docker_tray.GLib, "idle_add", side_effect=run_idle_callback)
    @mock.patch.object(docker_tray, "update_tray_menu")
    @mock.patch.object(docker_tray.threading, "Thread", ImmediateThread)
    @mock.patch.object(docker_tray.subprocess, "run")
    def test_failed_compose_up_notifies_and_reports_completion(
        self,
        run,
        update_menu,
        _idle_add,
    ):
        run.return_value = subprocess.CompletedProcess(
            args=["docker", "compose"],
            returncode=1,
            stdout="",
            stderr="invalid compose file",
        )
        icon = mock.Mock()
        on_finished = mock.Mock()

        docker_tray.run_compose_up("compose.yml", icon, on_finished)

        icon.notify.assert_called_once_with(
            "Could not start compose.yml: invalid compose file",
            "Docker Tray",
        )
        on_finished.assert_called_once_with(False)
        update_menu.assert_called_once_with(icon)

    @mock.patch.object(docker_tray.GLib, "idle_add", side_effect=run_idle_callback)
    @mock.patch.object(docker_tray, "update_tray_menu")
    @mock.patch.object(docker_tray.threading, "Thread", ImmediateThread)
    @mock.patch.object(docker_tray.subprocess, "run")
    def test_successful_compose_up_reports_success_without_notification(
        self,
        run,
        update_menu,
        _idle_add,
    ):
        run.return_value = subprocess.CompletedProcess(
            args=["docker", "compose"],
            returncode=0,
            stdout="",
            stderr="",
        )
        icon = mock.Mock()
        on_finished = mock.Mock()

        docker_tray.run_compose_up("compose.yml", icon, on_finished)

        icon.notify.assert_not_called()
        on_finished.assert_called_once_with(True)
        update_menu.assert_called_once_with(icon)

    @mock.patch.object(docker_tray.GLib, "idle_add", side_effect=run_idle_callback)
    @mock.patch.object(docker_tray.time, "sleep")
    @mock.patch.object(docker_tray, "get_compose_file_running_state", return_value=False)
    def test_compose_start_state_timeout_notifies_user(self, _state, _sleep, _idle_add):
        icon = mock.Mock()
        row = mock.Mock()
        button = mock.Mock()
        button.get_parent.return_value = row

        docker_tray.poll_compose_start_state("compose.yml", icon, row, button)

        icon.notify.assert_called_once_with(
            "Could not start compose.yml: no running container appeared within 60 seconds",
            "Docker Tray",
        )
        button.set_label.assert_called_once_with("Run")
        button.set_sensitive.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()

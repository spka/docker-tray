import subprocess
import unittest
from unittest import mock

import docker_tray
import docker_tray_platform


class ImmediateThread:
    def __init__(self, target, args=(), **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


def run_idle_callback(callback, *args):
    return callback(*args)


class UpdateWorkerTests(unittest.TestCase):
    def setUp(self):
        self.original_update_state = docker_tray.get_update_state_snapshot()
        docker_tray.updates_dialog.update({
            "window": None,
            "content": None,
            "status": "",
            "engine_upgrading": False,
            "pulling_images": set(),
        })
        self.engine_update = docker_tray_platform.EngineUpdate(
            True,
            package_name="Docker CE",
            upgrade_command=("test-upgrade",),
        )
        docker_tray.update_check_state.update({
            "engine_update": self.engine_update,
            "image_updates": ["example:latest"],
        })

    def tearDown(self):
        engine_update, image_updates = self.original_update_state
        docker_tray.update_check_state.update({
            "engine_update": engine_update,
            "image_updates": image_updates,
        })

    @mock.patch.object(docker_tray, "show_updates_dialog")
    @mock.patch.object(docker_tray.GLib, "idle_add", side_effect=run_idle_callback)
    @mock.patch.object(docker_tray.threading, "Thread", ImmediateThread)
    @mock.patch.object(
        docker_tray.docker_tray_platform,
        "run_engine_upgrade",
        side_effect=FileNotFoundError("pkexec was not found"),
    )
    def test_engine_upgrade_exception_clears_busy_state(
        self,
        _run_upgrade,
        _idle_add,
        _show_dialog,
    ):
        docker_tray.start_docker_engine_upgrade(mock.Mock())

        self.assertFalse(docker_tray.updates_dialog["engine_upgrading"])
        self.assertIn("pkexec was not found", docker_tray.updates_dialog["status"])

    @mock.patch.object(docker_tray, "show_updates_dialog")
    @mock.patch.object(docker_tray.GLib, "idle_add", side_effect=run_idle_callback)
    @mock.patch.object(docker_tray.threading, "Thread", ImmediateThread)
    @mock.patch.object(
        docker_tray,
        "run_image_compose_pull",
        side_effect=RuntimeError("Docker inspect failed"),
    )
    def test_unexpected_image_update_failure_clears_busy_state(
        self,
        _run_pull,
        _idle_add,
        _show_dialog,
    ):
        image = "example:latest"
        docker_tray.start_image_compose_pull(mock.Mock(), mock.Mock(), image)

        self.assertNotIn(image, docker_tray.updates_dialog["pulling_images"])
        self.assertIn("Docker inspect failed", docker_tray.updates_dialog["status"])

    @mock.patch.object(docker_tray_platform.subprocess, "run")
    def test_engine_upgrade_has_a_timeout(self, run):
        docker_tray_platform.run_engine_upgrade(self.engine_update, timeout=123)

        run.assert_called_once_with(
            ["test-upgrade"],
            capture_output=True,
            text=True,
            timeout=123,
        )

    @mock.patch.object(docker_tray, "run_docker_capture")
    @mock.patch.object(docker_tray, "get_container_image_ids")
    def test_replaced_image_cleanup_skips_images_still_used_by_containers(
        self,
        get_container_image_ids,
        run_docker_capture,
    ):
        old_unused = "sha256:" + "1" * 64
        old_still_used = "sha256:" + "2" * 64
        get_container_image_ids.return_value = {old_still_used}
        run_docker_capture.return_value = subprocess.CompletedProcess(
            args=["docker", "image", "rm"],
            returncode=0,
            stdout="",
            stderr="",
        )

        removed, error = docker_tray.remove_unused_replaced_images({old_unused, old_still_used})

        self.assertEqual(1, removed)
        self.assertEqual("", error)
        run_docker_capture.assert_called_once_with(
            ["docker", "image", "rm", old_unused],
            check=False,
        )

    @mock.patch.object(docker_tray, "run_docker_capture")
    @mock.patch.object(docker_tray, "get_container_image_ids", return_value=set())
    def test_replaced_image_cleanup_reports_removal_failure(
        self,
        _get_container_image_ids,
        run_docker_capture,
    ):
        old_image = "sha256:" + "1" * 64
        run_docker_capture.return_value = subprocess.CompletedProcess(
            args=["docker", "image", "rm"],
            returncode=1,
            stdout="",
            stderr="image is referenced by another tag",
        )

        removed, error = docker_tray.remove_unused_replaced_images({old_image})

        self.assertEqual(0, removed)
        self.assertEqual("image is referenced by another tag", error)

    @mock.patch.object(docker_tray, "finish_image_pull")
    @mock.patch.object(docker_tray, "set_image_pull_status")
    @mock.patch.object(docker_tray.GLib, "idle_add", side_effect=run_idle_callback)
    @mock.patch.object(docker_tray, "remove_unused_replaced_images", return_value=(1, ""))
    @mock.patch.object(docker_tray, "wait_for_compose_service_ready", return_value=True)
    @mock.patch.object(docker_tray, "run_compose_service_up")
    @mock.patch.object(docker_tray, "run_compose_pull")
    @mock.patch.object(docker_tray, "get_compose_pull_targets_for_image")
    @mock.patch.object(docker_tray, "get_container_image_ids_for_reference")
    def test_successful_image_update_removes_replaced_image_after_restart(
        self,
        get_old_images,
        get_targets,
        run_pull,
        run_up,
        _wait_ready,
        remove_old_images,
        _idle_add,
        _set_status,
        finish_pull,
    ):
        old_image = "sha256:" + "1" * 64
        get_old_images.return_value = {old_image}
        get_targets.return_value = [(('compose.yml',), "web", "/srv/web")]
        run_pull.return_value = subprocess.CompletedProcess([], 0, "", "")
        run_up.return_value = subprocess.CompletedProcess([], 0, "", "")
        icon = mock.Mock()

        docker_tray.run_image_compose_pull(icon, "example:latest")

        remove_old_images.assert_called_once_with({old_image})
        finish_pull.assert_called_once_with(icon, "example:latest", 1, 1, "")


if __name__ == "__main__":
    unittest.main()

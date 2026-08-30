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

    def test_image_update_check_accepts_all_registry_tags(self):
        for image in (
            "example",
            "example:latest",
            "example:15",
            "example:v3",
            "example:15.2.2",
            "registry.example:5000/team/example:stable",
        ):
            with self.subTest(image=image):
                self.assertTrue(docker_tray.is_checkable_image(image))

    def test_image_update_check_rejects_digest_pins_and_image_ids(self):
        for image in (
            "example@sha256:" + "1" * 64,
            "sha256:" + "2" * 64,
            "3" * 12,
        ):
            with self.subTest(image=image):
                self.assertFalse(docker_tray.is_checkable_image(image))

    @mock.patch.object(docker_tray, "get_remote_config_digest")
    @mock.patch.object(docker_tray, "get_local_image_id")
    @mock.patch.object(docker_tray, "get_container_image_refs")
    def test_image_update_check_includes_moving_major_tags(
        self,
        get_container_refs,
        get_local_image_id,
        get_remote_config_digest,
    ):
        digest_pin = "pinned@sha256:" + "4" * 64
        get_container_refs.return_value = [
            ("wg-easy:15", "sha256:old-wg"),
            ("immich:v3", "sha256:old-immich"),
            ("fixed:15.2.2", "sha256:fixed"),
            (digest_pin, "sha256:pinned"),
        ]
        get_local_image_id.side_effect = {
            "wg-easy:15": "sha256:old-wg",
            "immich:v3": "sha256:old-immich",
            "fixed:15.2.2": "sha256:fixed",
        }.get
        get_remote_config_digest.side_effect = {
            "wg-easy:15": "sha256:new-wg",
            "immich:v3": "sha256:new-immich",
            "fixed:15.2.2": "sha256:fixed",
        }.get

        updates = docker_tray.check_image_updates()

        self.assertEqual(["immich:v3", "wg-easy:15"], updates)
        self.assertNotIn(mock.call(digest_pin), get_local_image_id.call_args_list)
        self.assertNotIn(mock.call(digest_pin), get_remote_config_digest.call_args_list)
        self.assertEqual(3, get_local_image_id.call_count)

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
        finish_pull.assert_called_once_with(icon, "example:latest", 1, 1, "", True)

    @mock.patch.object(docker_tray, "show_updates_dialog")
    @mock.patch.object(docker_tray, "run_update_check")
    @mock.patch.object(docker_tray.GLib, "idle_add", side_effect=run_idle_callback)
    @mock.patch.object(docker_tray.threading, "Thread", ImmediateThread)
    @mock.patch.object(docker_tray, "run_image_compose_pull_safely")
    def test_update_all_processes_images_sequentially_and_summarizes_success(
        self,
        run_image_update,
        _idle_add,
        run_update_check,
        _show_dialog,
    ):
        images = ["one:latest", "two:latest"]
        docker_tray.update_check_state["image_updates"] = images
        run_image_update.side_effect = [
            (True, "", 1, ""),
            (True, "", 2, ""),
        ]
        icon = mock.Mock()

        docker_tray.start_all_image_compose_pulls(icon)

        self.assertEqual(
            [
                mock.call(
                    icon,
                    "one:latest",
                    start_recheck=False,
                    schedule_completion=False,
                ),
                mock.call(
                    icon,
                    "two:latest",
                    start_recheck=False,
                    schedule_completion=False,
                ),
            ],
            run_image_update.call_args_list,
        )
        self.assertEqual(set(), docker_tray.updates_dialog["pulling_images"])
        self.assertEqual(
            "Updated and cleaned up all 2 images. Removed 3 replaced images.",
            docker_tray.updates_dialog["status"],
        )
        self.assertEqual([], docker_tray.get_update_state_snapshot()[1])
        run_update_check.assert_called_once_with(icon)

    @mock.patch.object(docker_tray, "show_updates_dialog")
    @mock.patch.object(docker_tray, "run_update_check")
    @mock.patch.object(docker_tray.GLib, "idle_add", side_effect=run_idle_callback)
    @mock.patch.object(docker_tray.threading, "Thread", ImmediateThread)
    @mock.patch.object(docker_tray, "run_image_compose_pull_safely")
    def test_update_all_continues_after_an_image_failure(
        self,
        run_image_update,
        _idle_add,
        _run_update_check,
        _show_dialog,
    ):
        docker_tray.update_check_state["image_updates"] = ["broken:latest", "working:latest"]
        run_image_update.side_effect = [
            (False, "Pull failed for broken:latest", 0, ""),
            (True, "", 1, ""),
        ]

        docker_tray.start_all_image_compose_pulls(mock.Mock())

        self.assertEqual(2, run_image_update.call_count)
        self.assertIn("1 of 2 images updated", docker_tray.updates_dialog["status"])
        self.assertIn("Pull failed for broken:latest", docker_tray.updates_dialog["status"])
        self.assertEqual(["broken:latest"], docker_tray.get_update_state_snapshot()[1])

    @mock.patch.object(docker_tray, "show_updates_dialog")
    @mock.patch.object(docker_tray.threading, "Thread")
    def test_update_all_does_not_start_while_an_update_is_running(self, thread, show_dialog):
        docker_tray.updates_dialog["pulling_images"] = {"already-running:latest"}

        docker_tray.start_all_image_compose_pulls(mock.Mock())

        thread.assert_not_called()
        show_dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()

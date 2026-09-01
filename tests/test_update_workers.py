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
        docker_tray.remote_digest_cache.values.clear()
        docker_tray.updates_dialog.clear()
        docker_tray.updates_dialog_state.status = ""
        docker_tray.updates_dialog_state.app_upgrading = False
        docker_tray.updates_dialog_state.engine_upgrading = False
        docker_tray.updates_dialog_state.pulling_images.clear()
        self.engine_update = docker_tray_platform.EngineUpdate(
            True,
            package_name="Docker CE",
            upgrade_command=("test-upgrade",),
        )
        docker_tray.update_check_state.engine_update = self.engine_update
        docker_tray.update_check_state.image_updates = ["example:latest"]

    def tearDown(self):
        engine_update, image_updates = self.original_update_state
        docker_tray.update_check_state.engine_update = engine_update
        docker_tray.update_check_state.image_updates = image_updates

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

        self.assertFalse(docker_tray.updates_dialog_state.engine_upgrading)
        self.assertIn("pkexec was not found", docker_tray.updates_dialog_state.status)

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

        self.assertNotIn(image, docker_tray.updates_dialog_state.pulling_images)
        self.assertIn("Docker inspect failed", docker_tray.updates_dialog_state.status)

    @mock.patch.object(docker_tray_platform.subprocess, "run")
    def test_engine_upgrade_has_a_timeout(self, run):
        docker_tray_platform.run_engine_upgrade(self.engine_update, timeout=123)

        run.assert_called_once_with(
            ["test-upgrade"],
            capture_output=True,
            text=True,
            timeout=123,
        )

    @mock.patch.object(docker_tray, "check_app_update")
    @mock.patch.object(docker_tray, "check_engine_update")
    @mock.patch.object(docker_tray, "check_image_updates")
    def test_overlapping_update_check_is_skipped(
        self,
        check_images,
        check_engine,
        check_app,
    ):
        docker_tray.update_check_state.run_lock.acquire()
        try:
            docker_tray.run_update_check(mock.Mock())
        finally:
            docker_tray.update_check_state.run_lock.release()

        check_app.assert_not_called()
        check_engine.assert_not_called()
        check_images.assert_not_called()

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

    @mock.patch.object(docker_tray, "get_remote_config_digest", return_value="sha256:new")
    def test_remote_digest_cache_uses_ttl(self, get_remote_digest):
        self.assertEqual(
            "sha256:new",
            docker_tray.get_cached_remote_config_digest("example:latest", now=100),
        )
        self.assertEqual(
            "sha256:new",
            docker_tray.get_cached_remote_config_digest("example:latest", now=101),
        )
        self.assertEqual(1, get_remote_digest.call_count)

        docker_tray.get_cached_remote_config_digest(
            "example:latest",
            now=100 + docker_tray.REMOTE_DIGEST_CACHE_SECONDS + 1,
        )
        self.assertEqual(2, get_remote_digest.call_count)

    @mock.patch.object(docker_tray, "ThreadPoolExecutor")
    @mock.patch.object(docker_tray, "get_local_image_metadata")
    @mock.patch.object(docker_tray, "get_container_image_refs")
    def test_registry_checks_use_bounded_worker_pool(
        self,
        get_container_refs,
        get_local_image_metadata,
        executor,
    ):
        images = [f"example-{index}:latest" for index in range(6)]
        image_ids = {image: f"sha256:{index}" for index, image in enumerate(images)}
        get_container_refs.return_value = [
            (image, image_ids[image]) for image in images
        ]
        get_local_image_metadata.return_value = {
            image: docker_tray.LocalImageMetadata(image_id, True)
            for image, image_id in image_ids.items()
        }
        executor.return_value.__enter__.return_value.map.return_value = [
            image_ids[image] for image in images
        ]

        self.assertEqual([], docker_tray.check_image_updates())

        executor.assert_called_once_with(max_workers=docker_tray.REMOTE_DIGEST_WORKERS)
        map_call = executor.return_value.__enter__.return_value.map.call_args
        self.assertEqual(images, list(map_call.args[1]))
        get_local_image_metadata.assert_called_once_with(images)

    @mock.patch.object(docker_tray, "get_remote_config_digest")
    @mock.patch.object(docker_tray, "get_local_image_metadata")
    @mock.patch.object(docker_tray, "get_container_image_refs")
    def test_image_update_check_includes_moving_major_tags(
        self,
        get_container_refs,
        get_local_image_metadata,
        get_remote_config_digest,
    ):
        digest_pin = "pinned@sha256:" + "4" * 64
        get_container_refs.return_value = [
            ("wg-easy:15", "sha256:old-wg"),
            ("immich:v3", "sha256:old-immich"),
            ("fixed:15.2.2", "sha256:fixed"),
            (digest_pin, "sha256:pinned"),
        ]
        local_ids = {
            "wg-easy:15": "sha256:old-wg",
            "immich:v3": "sha256:old-immich",
            "fixed:15.2.2": "sha256:fixed",
        }
        get_local_image_metadata.return_value = {
            image: docker_tray.LocalImageMetadata(image_id, True)
            for image, image_id in local_ids.items()
        }
        get_remote_config_digest.side_effect = {
            "wg-easy:15": "sha256:new-wg",
            "immich:v3": "sha256:new-immich",
            "fixed:15.2.2": "sha256:fixed",
        }.get

        updates = docker_tray.check_image_updates()

        self.assertEqual(["immich:v3", "wg-easy:15"], updates)
        get_local_image_metadata.assert_called_once_with(sorted(local_ids))
        self.assertNotIn(mock.call(digest_pin), get_remote_config_digest.call_args_list)

    @mock.patch.object(docker_tray.subprocess, "run")
    def test_local_image_metadata_uses_one_batched_command(self, run):
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            (
                'sha256:one\t["one:latest@sha256:' + "a" * 64 + '"]\n'
                "sha256:two\tnull\n"
            ),
            "",
        )

        result = docker_tray.get_local_image_metadata(["one:latest", "two:latest"])

        self.assertEqual({
            "one:latest": docker_tray.LocalImageMetadata("sha256:one", True),
            "two:latest": docker_tray.LocalImageMetadata("sha256:two", False),
        }, result)
        run.assert_called_once_with(
            docker_tray.docker_tray_runtime.docker_command(
                "image", "inspect", "--format", docker_tray.DOCKER_IMAGE_METADATA_FORMAT,
                "one:latest", "two:latest",
            ),
            capture_output=True,
            text=True,
            timeout=docker_tray.DOCKER_CMD_TIMEOUT_SECONDS,
        )

    @mock.patch.object(docker_tray.subprocess, "run")
    def test_batched_image_inspection_failure_is_visible(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "", "missing image")

        with self.assertRaisesRegex(RuntimeError, "missing image"):
            docker_tray.get_local_image_metadata(["missing:latest"])

    @mock.patch.object(docker_tray, "get_remote_config_digest")
    @mock.patch.object(docker_tray, "get_local_image_metadata")
    @mock.patch.object(docker_tray, "get_container_image_refs")
    def test_locally_built_images_are_not_queried_from_a_registry(
        self,
        get_container_refs,
        get_local_image_metadata,
        get_remote_config_digest,
    ):
        images = {
            "feedr-feedr": "sha256:local",
            "feedr-gpt-worker": "sha256:worker",
        }
        get_container_refs.return_value = list(images.items())
        get_local_image_metadata.return_value = {
            image: docker_tray.LocalImageMetadata(image_id, False)
            for image, image_id in images.items()
        }

        self.assertEqual([], docker_tray.check_image_updates())

        get_remote_config_digest.assert_not_called()

    @mock.patch.object(docker_tray, "finish_image_pull")
    @mock.patch.object(docker_tray.GLib, "idle_add", side_effect=run_idle_callback)
    @mock.patch.object(docker_tray, "run_privileged_image_updates")
    def test_successful_image_update_uses_one_privileged_transaction(
        self,
        run_updates,
        _idle_add,
        finish_pull,
    ):
        run_updates.return_value = {
            "example:latest": {
                "success": True,
                "error": "",
                "service_count": 1,
                "removed_image_count": 1,
                "cleanup_error": "",
            }
        }
        icon = mock.Mock()

        docker_tray.run_image_compose_pull(icon, "example:latest")

        run_updates.assert_called_once_with(["example:latest"])
        finish_pull.assert_called_once_with(icon, "example:latest", 1, 1, "", True)

    @mock.patch.object(docker_tray.subprocess, "run")
    def test_privileged_image_update_batches_images_in_one_process(self, run):
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                '{"image":"one:latest","status":"Pulling..."}\n'
                '{"image":"one:latest","success":true,"service_count":1,'
                '"removed_image_count":1,"cleanup_error":""}\n'
                '{"image":"two:latest","success":false,"error":"pull failed"}\n'
            ),
            stderr="",
        )

        outcomes = docker_tray.run_privileged_image_updates(
            ["one:latest", "two:latest"]
        )

        self.assertTrue(outcomes["one:latest"]["success"])
        self.assertFalse(outcomes["two:latest"]["success"])
        run.assert_called_once_with(
            [
                "pkexec",
                str(docker_tray.docker_tray_runtime.PRIVILEGED_HELPER),
                "image-update",
                "one:latest",
                "two:latest",
            ],
            capture_output=True,
            text=True,
            timeout=2 * docker_tray.DOCKER_IMAGE_UPDATE_TIMEOUT_SECONDS,
        )

    @mock.patch.object(docker_tray, "show_updates_dialog")
    @mock.patch.object(docker_tray, "run_update_check")
    @mock.patch.object(docker_tray.GLib, "idle_add", side_effect=run_idle_callback)
    @mock.patch.object(docker_tray.threading, "Thread", ImmediateThread)
    @mock.patch.object(docker_tray, "run_privileged_image_updates")
    def test_update_all_batches_images_and_summarizes_success(
        self,
        run_updates,
        _idle_add,
        run_update_check,
        _show_dialog,
    ):
        images = ["one:latest", "two:latest"]
        docker_tray.update_check_state.image_updates = images
        run_updates.return_value = {
            "one:latest": {
                "success": True, "removed_image_count": 1, "cleanup_error": "",
            },
            "two:latest": {
                "success": True, "removed_image_count": 2, "cleanup_error": "",
            },
        }
        icon = mock.Mock()

        docker_tray.start_all_image_compose_pulls(icon)

        run_updates.assert_called_once_with(images)
        self.assertEqual(set(), docker_tray.updates_dialog_state.pulling_images)
        self.assertEqual(
            "Updated and cleaned up all 2 images. Removed 3 replaced images.",
            docker_tray.updates_dialog_state.status,
        )
        self.assertEqual([], docker_tray.get_update_state_snapshot()[1])
        run_update_check.assert_called_once_with(icon)

    @mock.patch.object(docker_tray, "show_updates_dialog")
    @mock.patch.object(docker_tray, "run_update_check")
    @mock.patch.object(docker_tray.GLib, "idle_add", side_effect=run_idle_callback)
    @mock.patch.object(docker_tray.threading, "Thread", ImmediateThread)
    @mock.patch.object(docker_tray, "run_privileged_image_updates")
    def test_update_all_continues_after_an_image_failure(
        self,
        run_updates,
        _idle_add,
        _run_update_check,
        _show_dialog,
    ):
        docker_tray.update_check_state.image_updates = ["broken:latest", "working:latest"]
        run_updates.return_value = {
            "broken:latest": {
                "success": False,
                "error": "Pull failed",
                "removed_image_count": 0,
                "cleanup_error": "",
            },
            "working:latest": {
                "success": True,
                "error": "",
                "removed_image_count": 1,
                "cleanup_error": "",
            },
        }

        docker_tray.start_all_image_compose_pulls(mock.Mock())

        run_updates.assert_called_once_with(["broken:latest", "working:latest"])
        self.assertIn("1 of 2 images updated", docker_tray.updates_dialog_state.status)
        self.assertIn("broken:latest: Pull failed", docker_tray.updates_dialog_state.status)
        self.assertEqual(["broken:latest"], docker_tray.get_update_state_snapshot()[1])

    @mock.patch.object(docker_tray, "show_updates_dialog")
    @mock.patch.object(docker_tray.threading, "Thread")
    def test_update_all_does_not_start_while_an_update_is_running(self, thread, show_dialog):
        docker_tray.updates_dialog_state.pulling_images = {"already-running:latest"}

        docker_tray.start_all_image_compose_pulls(mock.Mock())

        thread.assert_not_called()
        show_dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()

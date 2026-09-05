import subprocess
import unittest
from unittest import mock
import docker_tray
import docker_tray_update_service as update_module
import docker_tray_update_backend as backend_module
import docker_tray_updates as update_models
import docker_tray_platform


def make_service():
    return update_module.UpdateService(
        backend_module.UpdateBackend(docker_tray.APP_VERSION, docker_tray.PLATFORM_INFO),
        on_changed=docker_tray.on_updates_changed,
        notify=docker_tray.notify_user,
        restart=docker_tray.restart_after_upgrade,
    )


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
        self.service = make_service()
        self.original_update_state = self.service.get_update_state_snapshot()
        self.service.remote_digest_cache.values.clear()
        self.service.operation_state.clear_if_idle()
        self.service.operation_state.status = ""
        self.service.operation_state.app_upgrading = False
        self.service.operation_state.engine_upgrading = False
        self.service.operation_state.pulling_images.clear()
        self.engine_update = docker_tray_platform.EngineUpdate(
            True, package_name="Docker CE", upgrade_command=("test-upgrade",)
        )
        self.service.update_check_state.engine_update = self.engine_update
        self.service.update_check_state.image_updates = ["example:latest"]

    def tearDown(self):
        engine_update, image_updates = self.original_update_state
        self.service.update_check_state.engine_update = engine_update
        self.service.update_check_state.image_updates = image_updates

    def test_engine_upgrade_exception_clears_busy_state(self):
        with mock.patch.object(self.service, "on_changed"), mock.patch.object(
            self.service, "dispatch", side_effect=run_idle_callback
        ), mock.patch.object(docker_tray.threading, "Thread", ImmediateThread), mock.patch.object(
            docker_tray.docker_tray_platform,
            "run_engine_upgrade",
            side_effect=FileNotFoundError("pkexec was not found"),
        ):
            self.service.start_docker_engine_upgrade(mock.Mock())
            self.assertFalse(self.service.operation_state.engine_upgrading)
            self.assertIn("pkexec was not found", self.service.operation_state.status)

    def test_unexpected_image_update_failure_clears_busy_state(self):
        with mock.patch.object(self.service, "on_changed"), mock.patch.object(
            self.service, "dispatch", side_effect=run_idle_callback
        ), mock.patch.object(docker_tray.threading, "Thread", ImmediateThread), mock.patch.object(
            self.service,
            "run_image_compose_pull",
            side_effect=RuntimeError("Docker inspect failed"),
        ):
            image = "example:latest"
            self.service.start_image_compose_pull(mock.Mock(), mock.Mock(), image)
            self.assertNotIn(image, self.service.operation_state.pulling_images)
            self.assertIn("Docker inspect failed", self.service.operation_state.status)

    def test_engine_upgrade_has_a_timeout(self):
        with mock.patch.object(docker_tray_platform.subprocess, "run") as run:
            docker_tray_platform.run_engine_upgrade(self.engine_update, timeout=123)
            run.assert_called_once_with(
                ["test-upgrade"], capture_output=True, text=True, timeout=123
            )

    def test_overlapping_update_check_is_skipped(self):
        with mock.patch.object(
            self.service.backend, "check_app_update"
        ) as check_app, mock.patch.object(
            self.service.backend, "check_engine_update"
        ) as check_engine, mock.patch.object(
            self.service, "check_image_updates"
        ) as check_images:
            self.service.update_check_state.run_lock.acquire()
            try:
                self.service.run_update_check(mock.Mock())
            finally:
                self.service.update_check_state.run_lock.release()
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
                self.assertTrue(update_models.is_checkable_image(image))

    def test_image_update_check_rejects_digest_pins_and_image_ids(self):
        for image in ("example@sha256:" + "1" * 64, "sha256:" + "2" * 64, "3" * 12):
            with self.subTest(image=image):
                self.assertFalse(update_models.is_checkable_image(image))

    def test_remote_digest_cache_uses_ttl(self):
        with mock.patch.object(
            self.service.backend, "get_remote_config_digest", return_value="sha256:new"
        ) as get_remote_digest:
            self.assertEqual(
                "sha256:new",
                self.service.get_cached_remote_config_digest("example:latest", now=100),
            )
            self.assertEqual(
                "sha256:new",
                self.service.get_cached_remote_config_digest("example:latest", now=101),
            )
            self.assertEqual(1, get_remote_digest.call_count)
            self.service.get_cached_remote_config_digest(
                "example:latest", now=100 + update_module.REMOTE_DIGEST_CACHE_SECONDS + 1
            )
            self.assertEqual(2, get_remote_digest.call_count)

    def test_registry_checks_use_bounded_worker_pool(self):
        with mock.patch.object(self.service, "executor_factory") as executor, mock.patch.object(
            self.service.backend, "get_local_image_metadata"
        ) as get_local_image_metadata, mock.patch.object(
            self.service.backend, "get_container_image_refs"
        ) as get_container_refs:
            images = [f"example-{index}:latest" for index in range(6)]
            image_ids = {image: f"sha256:{index}" for index, image in enumerate(images)}
            get_container_refs.return_value = [(image, image_ids[image]) for image in images]
            get_local_image_metadata.return_value = {
                image: update_models.LocalImageMetadata(image_id, True)
                for image, image_id in image_ids.items()
            }
            executor.return_value.__enter__.return_value.map.return_value = [
                image_ids[image] for image in images
            ]
            self.assertEqual([], self.service.check_image_updates())
            executor.assert_called_once_with(max_workers=update_module.REMOTE_DIGEST_WORKERS)
            map_call = executor.return_value.__enter__.return_value.map.call_args
            self.assertEqual(images, list(map_call.args[1]))
            get_local_image_metadata.assert_called_once_with(images)

    def test_registry_errors_are_cached_then_retried(self):
        with mock.patch.object(self.service.backend, "get_remote_config_digest") as remote:
            remote.side_effect = [RuntimeError("registry offline"), "sha256:new"]
            for now in (100, 101):
                with self.assertRaisesRegex(RuntimeError, "registry offline"):
                    self.service.get_cached_remote_config_digest("example:latest", now=now)
            self.assertEqual(1, remote.call_count)
            self.assertEqual(
                "sha256:new",
                self.service.get_cached_remote_config_digest(
                    "example:latest", now=100 + update_module.REMOTE_DIGEST_FAILURE_CACHE_SECONDS
                ),
            )
            self.assertEqual(2, remote.call_count)

    def test_image_discovery_reports_failed_commands(self):
        with mock.patch.object(self.service.backend, "run_docker_capture") as run:
            failure = subprocess.CompletedProcess([], 1, "", "Docker unavailable")
            for responses in (
                [failure],
                [subprocess.CompletedProcess([], 0, "a" * 64 + "\n", ""), failure],
            ):
                with self.subTest(command_count=len(responses)):
                    run.side_effect = responses
                    with self.assertRaisesRegex(RuntimeError, "Docker unavailable"):
                        self.service.backend.get_container_image_refs()

    def test_registry_failure_keeps_other_results(self):
        with mock.patch.object(
            self.service.backend, "get_remote_config_digest"
        ) as remote, mock.patch.object(
            self.service.backend, "get_local_image_metadata"
        ) as metadata, mock.patch.object(
            self.service.backend, "get_container_image_refs"
        ) as refs:
            images = ["broken:latest", "current:latest", "working:latest", "stale:latest"]
            refs.return_value = [(image, "sha256:old") for image in images]
            metadata.return_value = {
                image: update_models.LocalImageMetadata(
                    "sha256:new" if image == "stale:latest" else "sha256:old", True
                )
                for image in images
            }

            def lookup(image):
                if image in {"broken:latest", "stale:latest"}:
                    raise RuntimeError("offline")
                return "sha256:new" if image == "working:latest" else "sha256:old"

            remote.side_effect = lookup
            with self.assertRaises(update_models.ImageUpdateCheckError) as raised:
                self.service.check_image_updates()
            self.assertEqual(["stale:latest", "working:latest"], raised.exception.updates)
            self.assertEqual({"broken:latest", "stale:latest"}, set(raised.exception.failures))
            self.assertEqual(4, remote.call_count)

    def test_missing_platform_digest_is_reported(self):
        with mock.patch.object(self.service, "get_cached_remote_config_digest", return_value=None):
            outcome = self.service.get_remote_digest_outcome("example:latest")
            self.assertIsInstance(outcome, RuntimeError)
            self.assertIn("no matching image digest", str(outcome))

    def test_partial_scan_preserves_only_failed_image_notices(self):
        with mock.patch.object(self.service.backend, "check_app_update"), mock.patch.object(
            self.service.backend, "check_engine_update"
        ), mock.patch.object(self.service, "check_image_updates") as check_images:
            self.service.update_check_state.image_updates = ["broken:latest", "current:latest"]
            check_images.side_effect = update_models.ImageUpdateCheckError(
                ["working:latest"], {"broken:latest": "offline"}
            )
            self.service.run_update_check(mock.Mock())
            self.assertEqual(
                ["broken:latest", "working:latest"], self.service.get_update_state_snapshot()[1]
            )
            self.assertIn(
                "broken:latest: offline", self.service.get_update_feedback_snapshot()["errors"][0]
            )

    def test_failed_docker_query_preserves_previous_updates(self):
        with mock.patch.object(self.service.backend, "check_app_update"), mock.patch.object(
            self.service.backend, "check_engine_update"
        ), mock.patch.object(self.service.backend, "run_docker_capture") as capture:
            capture.return_value = subprocess.CompletedProcess([], 1, "", "Docker unavailable")
            self.service.run_update_check(mock.Mock())
            self.assertEqual(["example:latest"], self.service.get_update_state_snapshot()[1])
            self.assertIn(
                "Docker unavailable", self.service.get_update_feedback_snapshot()["errors"][0]
            )

    def test_image_update_check_includes_moving_major_tags(self):
        with mock.patch.object(
            self.service.backend, "get_remote_config_digest"
        ) as get_remote_config_digest, mock.patch.object(
            self.service.backend, "get_local_image_metadata"
        ) as get_local_image_metadata, mock.patch.object(
            self.service.backend, "get_container_image_refs"
        ) as get_container_refs:
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
                image: update_models.LocalImageMetadata(image_id, True)
                for image, image_id in local_ids.items()
            }
            get_remote_config_digest.side_effect = {
                "wg-easy:15": "sha256:new-wg",
                "immich:v3": "sha256:new-immich",
                "fixed:15.2.2": "sha256:fixed",
            }.get
            updates = self.service.check_image_updates()
            self.assertEqual(["immich:v3", "wg-easy:15"], updates)
            get_local_image_metadata.assert_called_once_with(sorted(local_ids))
            self.assertNotIn(mock.call(digest_pin), get_remote_config_digest.call_args_list)

    def test_local_image_metadata_uses_one_batched_command(self):
        with mock.patch.object(self.service.backend, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, 'sha256:one\t["one:latest@sha256:' + "a" * 64 + '"]\nsha256:two\tnull\n', ""
            )
            result = self.service.backend.get_local_image_metadata(["one:latest", "two:latest"])
            self.assertEqual(
                {
                    "one:latest": update_models.LocalImageMetadata("sha256:one", True),
                    "two:latest": update_models.LocalImageMetadata("sha256:two", False),
                },
                result,
            )
            run.assert_called_once_with(
                docker_tray.docker_tray_runtime.docker_command(
                    "image",
                    "inspect",
                    "--format",
                    backend_module.DOCKER_IMAGE_METADATA_FORMAT,
                    "one:latest",
                    "two:latest",
                ),
                capture_output=True,
                text=True,
                timeout=backend_module.DOCKER_CMD_TIMEOUT_SECONDS,
            )

    def test_batched_image_inspection_failure_is_visible(self):
        with mock.patch.object(self.service.backend, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "", "missing image")
            with self.assertRaisesRegex(RuntimeError, "missing image"):
                self.service.backend.get_local_image_metadata(["missing:latest"])

    def test_locally_built_images_are_not_queried_from_a_registry(self):
        with mock.patch.object(
            self.service.backend, "get_remote_config_digest"
        ) as get_remote_config_digest, mock.patch.object(
            self.service.backend, "get_local_image_metadata"
        ) as get_local_image_metadata, mock.patch.object(
            self.service.backend, "get_container_image_refs"
        ) as get_container_refs:
            images = {"feedr-feedr": "sha256:local", "feedr-gpt-worker": "sha256:worker"}
            get_container_refs.return_value = list(images.items())
            get_local_image_metadata.return_value = {
                image: update_models.LocalImageMetadata(image_id, False)
                for image, image_id in images.items()
            }
            self.assertEqual([], self.service.check_image_updates())
            get_remote_config_digest.assert_not_called()

    def test_successful_image_update_uses_one_privileged_transaction(self):
        with mock.patch.object(self.service, "finish_image_pull") as finish_pull, mock.patch.object(
            self.service, "dispatch", side_effect=run_idle_callback
        ), mock.patch.object(self.service.backend, "run_privileged_image_updates") as run_updates:
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
            self.service.run_image_compose_pull(icon, "example:latest")
            run_updates.assert_called_once_with(["example:latest"])
            finish_pull.assert_called_once_with(icon, "example:latest", 1, 1, "", True)

    def test_privileged_image_update_batches_images_in_one_process(self):
        with mock.patch.object(self.service.backend, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                [],
                0,
                stdout='{"image":"one:latest","status":"Pulling..."}\n{"image":"one:latest","success":true,"service_count":1,"removed_image_count":1,"cleanup_error":""}\n{"image":"two:latest","success":false,"error":"pull failed"}\n',
                stderr="",
            )
            outcomes = self.service.backend.run_privileged_image_updates(
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
                timeout=2 * backend_module.DOCKER_IMAGE_UPDATE_TIMEOUT_SECONDS,
            )

    def test_update_all_batches_images_and_summarizes_success(self):
        with mock.patch.object(self.service, "on_changed"), mock.patch.object(
            self.service, "run_update_check"
        ) as run_update_check, mock.patch.object(
            self.service, "dispatch", side_effect=run_idle_callback
        ), mock.patch.object(
            docker_tray.threading, "Thread", ImmediateThread
        ), mock.patch.object(
            self.service.backend, "run_privileged_image_updates"
        ) as run_updates:
            images = ["one:latest", "two:latest"]
            self.service.update_check_state.image_updates = images
            run_updates.return_value = {
                "one:latest": {"success": True, "removed_image_count": 1, "cleanup_error": ""},
                "two:latest": {"success": True, "removed_image_count": 2, "cleanup_error": ""},
            }
            icon = mock.Mock()
            self.service.start_all_image_compose_pulls(icon)
            run_updates.assert_called_once_with(images)
            self.assertEqual(set(), self.service.operation_state.pulling_images)
            self.assertEqual(
                "Updated and cleaned up all 2 images. Removed 3 replaced images.",
                self.service.operation_state.status,
            )
            self.assertEqual([], self.service.get_update_state_snapshot()[1])
            run_update_check.assert_called_once_with(icon)

    def test_update_all_continues_after_an_image_failure(self):
        with mock.patch.object(self.service, "on_changed"), mock.patch.object(
            self.service, "run_update_check"
        ), mock.patch.object(
            self.service, "dispatch", side_effect=run_idle_callback
        ), mock.patch.object(
            docker_tray.threading, "Thread", ImmediateThread
        ), mock.patch.object(
            self.service.backend, "run_privileged_image_updates"
        ) as run_updates:
            self.service.update_check_state.image_updates = ["broken:latest", "working:latest"]
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
            self.service.start_all_image_compose_pulls(mock.Mock())
            run_updates.assert_called_once_with(["broken:latest", "working:latest"])
            self.assertIn("1 of 2 images updated", self.service.operation_state.status)
            self.assertIn("broken:latest: Pull failed", self.service.operation_state.status)
            self.assertEqual(["broken:latest"], self.service.get_update_state_snapshot()[1])

    def test_update_all_does_not_start_while_an_update_is_running(self):
        with mock.patch.object(self.service, "on_changed") as show_dialog, mock.patch.object(
            docker_tray.threading, "Thread"
        ) as thread:
            self.service.operation_state.pulling_images = {"already-running:latest"}
            self.service.start_all_image_compose_pulls(mock.Mock())
            thread.assert_not_called()
            show_dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()

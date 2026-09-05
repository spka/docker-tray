"""Service contracts exercised without importing the desktop application."""

import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from docker_tray_platform import EngineUpdate, PlatformInfo
from docker_tray_update_backend import UpdateBackend
from docker_tray_update_service import UpdateService
from docker_tray_updates import AppUpdate, LocalImageMetadata


class UpdateServiceTests(unittest.TestCase):
    def make_backend(self):
        backend = mock.Mock(spec=UpdateBackend)
        backend.version = "0.2.14"
        backend.check_app_update.return_value = AppUpdate(False)
        backend.check_engine_update.return_value = EngineUpdate(False)
        backend.get_container_image_refs.return_value = []
        backend.get_local_image_metadata.return_value = {}
        return backend

    def test_service_imports_without_site_packages_or_a_desktop(self):
        result = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                "import sys; import docker_tray_update_service, docker_tray_update_backend; "
                "assert 'gi' not in sys.modules; assert 'docker_tray' not in sys.modules",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_instances_do_not_share_state_or_cache(self):
        first = UpdateService(self.make_backend())
        second = UpdateService(self.make_backend())
        first.update_check_state.image_updates.append("web:latest")
        first.operation_state.pulling_images.add("web:latest")
        first.remote_digest_cache.values["web:latest"] = (100, "sha256:new", None)
        self.assertEqual([], second.get_update_state_snapshot()[1])
        self.assertEqual(set(), second.operation_state.pulling_images)
        self.assertEqual({}, second.remote_digest_cache.values)

    def test_queued_completion_prevents_overlapping_scan(self):
        backend = self.make_backend()
        callbacks = []
        notices = mock.Mock()
        service = UpdateService(
            backend, dispatch=lambda fn, *args: callbacks.append((fn, args)), on_changed=notices
        )
        backend.check_app_update.return_value = AppUpdate(True, latest_version="0.3.0")
        service.run_update_check(None)
        service.run_update_check(None)
        backend.check_app_update.assert_called_once()
        self.assertFalse(service.get_app_update_snapshot().available)
        notices.assert_not_called()
        for callback, args in callbacks:
            callback(*args)
        self.assertTrue(service.get_app_update_snapshot().available)
        self.assertFalse(service.get_update_feedback_snapshot()["checking"])
        self.assertFalse(service.update_check_state.run_lock.locked())
        callbacks.clear()
        service.run_update_check(None)
        self.assertEqual(2, backend.check_app_update.call_count)
        for callback, args in callbacks:
            callback(*args)

    def test_dispatch_failure_releases_scan_lock(self):
        service = UpdateService(
            self.make_backend(), dispatch=mock.Mock(side_effect=RuntimeError("closed"))
        )
        with self.assertRaisesRegex(RuntimeError, "closed"):
            service.run_update_check(None)
        self.assertFalse(service.update_check_state.run_lock.locked())

    def test_shutdown_wakes_periodic_checker(self):
        backend = self.make_backend()
        completed = threading.Event()
        service = UpdateService(backend, on_changed=lambda *_: completed.set())
        worker = threading.Thread(target=service.poll_updates, args=(None,), daemon=True)
        worker.start()
        try:
            self.assertTrue(completed.wait(2))
        finally:
            service.close()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        backend.check_app_update.assert_called_once()
        service.run_update_check(None)
        backend.check_app_update.assert_called_once()

    def test_scan_keeps_successful_results_and_reports_registry_failure(self):
        backend = self.make_backend()
        images = ["offline:latest", "web:latest"]
        backend.get_container_image_refs.return_value = [(image, "sha256:old") for image in images]
        backend.get_local_image_metadata.return_value = {
            image: LocalImageMetadata("sha256:old", True) for image in images
        }

        def digest(image):
            if image == "offline:latest":
                raise RuntimeError("offline")
            return "sha256:new"

        backend.get_remote_config_digest.side_effect = digest
        service = UpdateService(backend)
        service.run_update_check(None)
        self.assertEqual(["web:latest"], service.get_update_state_snapshot()[1])
        self.assertIn("offline:latest", service.get_update_feedback_snapshot()["errors"][0])
        service.run_update_check(None)
        self.assertEqual(2, backend.get_remote_config_digest.call_count)

    def test_operation_remains_busy_until_dispatched_completion(self):
        callbacks = []
        backend = self.make_backend()
        backend.run_privileged_image_updates.return_value = {
            "web:latest": {
                "success": True,
                "service_count": 1,
                "removed_image_count": 0,
                "cleanup_error": "",
            },
        }
        service = UpdateService(
            backend,
            start_background=lambda fn, *args: fn(*args),
            dispatch=lambda fn, *args: callbacks.append((fn, args)),
        )
        service.update_check_state.image_updates = ["web:latest"]
        service.start_image_compose_pull(None, None, "web:latest")
        self.assertEqual({"web:latest"}, service.operation_state.pulling_images)
        service.start_image_compose_pull(None, None, "other:latest")
        backend.run_privileged_image_updates.assert_called_once_with(["web:latest"])
        while callbacks:
            callback, args = callbacks.pop(0)
            callback(*args)
        self.assertEqual(set(), service.operation_state.pulling_images)
        self.assertEqual([], service.get_update_state_snapshot()[1])

    def test_arch_backend_rejects_package_installation(self):
        run = mock.Mock()
        backend = UpdateBackend("0.2.14", PlatformInfo("arch", (), ()), run=run)
        update = AppUpdate(
            True, package_url="https://example.com/package.deb", package_digest="sha256:abc"
        )
        with self.assertRaisesRegex(RuntimeError, "not available"):
            backend.run_app_upgrade(update)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

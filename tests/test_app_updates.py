import json
import hashlib
import subprocess
import tempfile
import unittest
from unittest import mock
import docker_tray
import docker_tray_update_service as update_module
import docker_tray_update_backend as backend_module
import docker_tray_updates as update_models


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


def github_response(tag_name, html_url=None, assets=None):
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {
            "tag_name": tag_name,
            "html_url": html_url or f"https://github.com/spka/docker-tray/releases/tag/{tag_name}",
            "assets": assets or [],
        }
    ).encode()
    return response


class AppUpdateTests(unittest.TestCase):

    def setUp(self):
        self.service = make_service()
        self.original_app_update = self.service.get_app_update_snapshot()
        self.original_engine_update, self.original_image_updates = (
            self.service.get_update_state_snapshot()
        )
        self.original_feedback = self.service.get_update_feedback_snapshot()

    def tearDown(self):
        state = self.service.update_check_state
        state.app_update = self.original_app_update
        state.engine_update = self.original_engine_update
        state.image_updates = self.original_image_updates
        state.checking = self.original_feedback["checking"]
        state.last_checked = self.original_feedback["last_checked"]
        state.errors = self.original_feedback["errors"]

    def test_version_comparison_normalizes_two_and_three_part_versions(self):
        self.assertEqual((0, 2, 0), update_models.parse_app_version("0.2"))
        self.assertEqual((0, 2, 0), update_models.parse_app_version("v0.2.0"))
        self.assertGreater(
            update_models.parse_app_version("v0.3.0"), update_models.parse_app_version("0.2.9")
        )

    def test_newer_github_release_creates_app_update(self):
        with mock.patch.object(self.service.backend, "urlopen") as urlopen:
            package_data = b"debian package"
            package_url = "https://github.com/spka/docker-tray/releases/download/v0.3.0/docker-tray_0.3.0_all.deb"
            package_digest = f"sha256:{hashlib.sha256(package_data).hexdigest()}"
            urlopen.return_value = github_response(
                "v0.3.0",
                assets=[
                    {
                        "name": "docker-tray_0.3.0_all.deb",
                        "browser_download_url": package_url,
                        "digest": package_digest,
                    }
                ],
            )
            update = self.service.backend.check_app_update()
            self.assertTrue(update.available)
            self.assertEqual("0.3.0", update.latest_version)
            self.assertEqual(
                "https://github.com/spka/docker-tray/releases/tag/v0.3.0", update.release_url
            )
            self.assertEqual(package_url, update.package_url)
            self.assertEqual(package_digest, update.package_digest)
            request = urlopen.call_args.args[0]
            self.assertEqual(
                f"docker-tray/{docker_tray.APP_VERSION}", request.get_header("User-agent")
            )
            self.assertEqual(
                backend_module.APP_UPDATE_TIMEOUT_SECONDS, urlopen.call_args.kwargs["timeout"]
            )

    def test_current_or_older_release_does_not_create_notice(self):
        with mock.patch.object(self.service.backend, "urlopen") as urlopen:
            urlopen.return_value = github_response(f"v{docker_tray.APP_VERSION}")
            update = self.service.backend.check_app_update()
            self.assertFalse(update.available)

    def test_untrusted_release_url_is_replaced_with_repository_url(self):
        with mock.patch.object(self.service.backend, "urlopen") as urlopen:
            urlopen.return_value = github_response("v0.3.0", "https://example.com/download")
            update = self.service.backend.check_app_update()
            self.assertEqual(
                "https://github.com/spka/docker-tray/releases/tag/v0.3.0", update.release_url
            )

    def test_invalid_release_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported version"):
            update_models.parse_app_version("latest")

    def test_untrusted_release_package_is_not_installable(self):
        with mock.patch.object(self.service.backend, "urlopen") as urlopen:
            urlopen.return_value = github_response(
                "v0.3.0",
                assets=[
                    {
                        "name": "docker-tray_0.3.0_all.deb",
                        "browser_download_url": "https://example.com/docker-tray_0.3.0_all.deb",
                        "digest": f"sha256:{'0' * 64}",
                    }
                ],
            )
            update = self.service.backend.check_app_update()
            self.assertEqual("", update.package_url)

    def test_release_package_without_digest_is_not_installable(self):
        update = update_models.AppUpdate(
            True,
            latest_version="0.3.0",
            package_url="https://github.com/spka/docker-tray/releases/download/v0.3.0/docker-tray_0.3.0_all.deb",
        )
        self.assertFalse(update.can_install)

    def test_download_verifies_release_checksum(self):
        with mock.patch.object(
            self.service.backend, "validate_app_update_package"
        ) as validate_package, mock.patch.object(self.service.backend, "urlopen") as urlopen:
            package_data = b"valid package bytes"
            response = mock.MagicMock()
            response.__enter__.return_value.read.side_effect = [package_data, b""]
            urlopen.return_value = response
            update = update_models.AppUpdate(
                True,
                latest_version="0.3.0",
                package_url="https://github.com/spka/docker-tray/releases/download/v0.3.0/docker-tray_0.3.0_all.deb",
                package_digest=f"sha256:{hashlib.sha256(package_data).hexdigest()}",
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                package_path = self.service.backend.download_app_update(update, temp_dir)
                self.assertEqual(package_data, package_path.read_bytes())
            validate_package.assert_called_once_with(package_path, "0.3.0")

    def test_download_rejects_wrong_checksum(self):
        with mock.patch.object(
            self.service.backend, "validate_app_update_package"
        ), mock.patch.object(self.service.backend, "urlopen") as urlopen:
            response = mock.MagicMock()
            response.__enter__.return_value.read.side_effect = [b"wrong bytes", b""]
            urlopen.return_value = response
            update = update_models.AppUpdate(
                True,
                latest_version="0.3.0",
                package_url="https://github.com/spka/docker-tray/releases/download/v0.3.0/docker-tray_0.3.0_all.deb",
                package_digest=f"sha256:{'0' * 64}",
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                with self.assertRaisesRegex(RuntimeError, "checksum"):
                    self.service.backend.download_app_update(update, temp_dir)

    def test_app_upgrade_installs_downloaded_package_with_pkexec(self):
        with mock.patch.object(
            self.service.backend, "download_app_update"
        ) as download, mock.patch.object(self.service.backend, "run") as run:
            update = update_models.AppUpdate(
                True,
                latest_version="0.3.0",
                package_url="https://github.com/spka/docker-tray/releases/download/v0.3.0/docker-tray_0.3.0_all.deb",
                package_digest=f"sha256:{'1' * 64}",
            )
            download.return_value = docker_tray.Path("/tmp/docker-tray_0.3.0_all.deb")
            run.return_value = subprocess.CompletedProcess([], 0)
            result = self.service.backend.run_app_upgrade(update)
            self.assertEqual(0, result.returncode)
            self.assertEqual(
                [
                    "pkexec",
                    str(docker_tray.docker_tray_runtime.PRIVILEGED_HELPER),
                    "install-update",
                    "/tmp/docker-tray_0.3.0_all.deb",
                    "0.3.0",
                    f"sha256:{'1' * 64}",
                ],
                run.call_args.args[0],
            )

    def test_successful_app_upgrade_stops_icon_for_restart(self):
        icon = mock.Mock()
        result = subprocess.CompletedProcess([], 0)
        self.service.finish_app_upgrade(icon, result, None)
        self.assertTrue(icon._restart_after_upgrade)
        icon.stop.assert_called_once_with()

    def test_new_app_release_sends_only_one_desktop_notice(self):
        with mock.patch.object(
            docker_tray, "send_desktop_notification", return_value=True
        ) as send_notification, mock.patch.object(
            docker_tray.threading, "Thread", ImmediateThread
        ), mock.patch.object(
            docker_tray, "update_tray_menu"
        ):
            icon = mock.Mock()
            update = update_models.AppUpdate(
                True,
                latest_version="0.3.0",
                release_url="https://github.com/spka/docker-tray/releases/tag/v0.3.0",
            )
            self.service.set_update_state(
                icon, self.original_engine_update, self.original_image_updates, update
            )
            self.service.set_update_state(
                icon, self.original_engine_update, self.original_image_updates, update
            )
            send_notification.assert_called_once_with("Docker Tray 0.3.0 is available.")

    def test_transient_github_failure_preserves_existing_notice(self):
        with mock.patch.object(docker_tray, "update_tray_menu"), mock.patch.object(
            self.service, "dispatch", side_effect=run_idle_callback
        ), mock.patch.object(
            self.service, "check_image_updates", return_value=[]
        ), mock.patch.object(
            self.service.backend, "check_engine_update"
        ) as check_engine, mock.patch.object(
            self.service.backend, "check_app_update", side_effect=OSError("offline")
        ):
            existing = update_models.AppUpdate(
                True,
                latest_version="0.3.0",
                release_url="https://github.com/spka/docker-tray/releases/tag/v0.3.0",
            )
            self.service.update_check_state.app_update = existing
            check_engine.return_value = self.original_engine_update
            self.service.run_update_check(mock.Mock())
            self.assertEqual(existing, self.service.get_app_update_snapshot())
            feedback = self.service.get_update_feedback_snapshot()
            self.assertFalse(feedback["checking"])
            self.assertIsNotNone(feedback["last_checked"])
            self.assertIn("offline", feedback["errors"][0])

    def test_update_feedback_distinguishes_checking_and_incomplete(self):
        self.service.update_check_state.checking = True
        self.service.update_check_state.last_checked = None
        self.service.update_check_state.errors = ()
        self.assertEqual("Checking for updates…", self.service.get_update_check_label())
        self.service.update_check_state.checking = False
        self.service.update_check_state.last_checked = 100
        self.service.update_check_state.errors = ("registry offline",)
        with mock.patch.object(docker_tray.time, "time", return_value=160):
            self.assertEqual(
                "Update check incomplete (1 minute ago)", self.service.get_update_check_label()
            )

    def test_background_update_progress_does_not_rebuild_menu(self):
        with mock.patch.object(docker_tray, "update_tray_menu") as update_menu:
            state = self.service.update_check_state
            state.app_update = update_models.AppUpdate(False)
            state.engine_update = docker_tray.docker_tray_platform.EngineUpdate(False)
            state.image_updates = []
            state.checking = False
            state.last_checked = None
            state.errors = ()
            icon = mock.Mock()
            self.service.set_update_feedback(icon, True)
            self.service.set_update_feedback(icon, False, last_checked=100, errors=())
            self.assertEqual(2, update_menu.call_count)
            icon.update_menu.assert_not_called()

    def test_new_update_notice_rebuilds_menu_once(self):
        with mock.patch.object(docker_tray, "update_tray_menu") as update_menu:
            state = self.service.update_check_state
            state.app_update = update_models.AppUpdate(False)
            state.engine_update = docker_tray.docker_tray_platform.EngineUpdate(False)
            state.image_updates = []
            state.errors = ()
            icon = mock.Mock()
            self.service.set_update_state(
                icon, docker_tray.docker_tray_platform.EngineUpdate(False), ["example:latest"]
            )
            update_menu.assert_called_once_with(icon)


if __name__ == "__main__":
    unittest.main()

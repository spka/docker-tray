import json
import hashlib
import subprocess
import tempfile
import unittest
from unittest import mock

import docker_tray


def run_idle_callback(callback, *args):
    return callback(*args)


def github_response(tag_name, html_url=None, assets=None):
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "tag_name": tag_name,
        "html_url": html_url or f"https://github.com/spka/docker-tray/releases/tag/{tag_name}",
        "assets": assets or [],
    }).encode()
    return response


class AppUpdateTests(unittest.TestCase):
    def setUp(self):
        self.original_app_update = docker_tray.get_app_update_snapshot()
        self.original_engine_update, self.original_image_updates = docker_tray.get_update_state_snapshot()
        self.original_feedback = docker_tray.get_update_feedback_snapshot()

    def tearDown(self):
        docker_tray.update_check_state.update({
            "app_update": self.original_app_update,
            "engine_update": self.original_engine_update,
            "image_updates": self.original_image_updates,
            **self.original_feedback,
        })

    def test_version_comparison_normalizes_two_and_three_part_versions(self):
        self.assertEqual((0, 2, 0), docker_tray.parse_app_version("0.2"))
        self.assertEqual((0, 2, 0), docker_tray.parse_app_version("v0.2.0"))
        self.assertGreater(
            docker_tray.parse_app_version("v0.3.0"),
            docker_tray.parse_app_version("0.2.9"),
        )

    @mock.patch.object(docker_tray, "urlopen")
    def test_newer_github_release_creates_app_update(self, urlopen):
        package_data = b"debian package"
        package_url = (
            "https://github.com/spka/docker-tray/releases/download/v0.3.0/"
            "docker-tray_0.3.0_all.deb"
        )
        package_digest = f"sha256:{hashlib.sha256(package_data).hexdigest()}"
        urlopen.return_value = github_response("v0.3.0", assets=[{
            "name": "docker-tray_0.3.0_all.deb",
            "browser_download_url": package_url,
            "digest": package_digest,
        }])

        update = docker_tray.check_app_update()

        self.assertTrue(update.available)
        self.assertEqual("0.3.0", update.latest_version)
        self.assertEqual(
            "https://github.com/spka/docker-tray/releases/tag/v0.3.0",
            update.release_url,
        )
        self.assertEqual(package_url, update.package_url)
        self.assertEqual(package_digest, update.package_digest)
        request = urlopen.call_args.args[0]
        self.assertEqual(f"docker-tray/{docker_tray.APP_VERSION}", request.get_header("User-agent"))
        self.assertEqual(docker_tray.APP_UPDATE_TIMEOUT_SECONDS, urlopen.call_args.kwargs["timeout"])

    @mock.patch.object(docker_tray, "urlopen")
    def test_current_or_older_release_does_not_create_notice(self, urlopen):
        urlopen.return_value = github_response(f"v{docker_tray.APP_VERSION}")

        update = docker_tray.check_app_update()

        self.assertFalse(update.available)

    @mock.patch.object(docker_tray, "urlopen")
    def test_untrusted_release_url_is_replaced_with_repository_url(self, urlopen):
        urlopen.return_value = github_response("v0.3.0", "https://example.com/download")

        update = docker_tray.check_app_update()

        self.assertEqual(
            "https://github.com/spka/docker-tray/releases/tag/v0.3.0",
            update.release_url,
        )

    def test_invalid_release_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported version"):
            docker_tray.parse_app_version("latest")

    @mock.patch.object(docker_tray, "urlopen")
    def test_untrusted_release_package_is_not_installable(self, urlopen):
        urlopen.return_value = github_response("v0.3.0", assets=[{
            "name": "docker-tray_0.3.0_all.deb",
            "browser_download_url": "https://example.com/docker-tray_0.3.0_all.deb",
            "digest": f"sha256:{'0' * 64}",
        }])

        update = docker_tray.check_app_update()

        self.assertEqual("", update.package_url)

    def test_release_package_without_digest_is_not_installable(self):
        update = docker_tray.AppUpdate(
            True,
            latest_version="0.3.0",
            package_url=(
                "https://github.com/spka/docker-tray/releases/download/v0.3.0/"
                "docker-tray_0.3.0_all.deb"
            ),
        )

        self.assertFalse(update.can_install)

    @mock.patch.object(docker_tray, "validate_app_update_package")
    @mock.patch.object(docker_tray, "urlopen")
    def test_download_verifies_release_checksum(self, urlopen, validate_package):
        package_data = b"valid package bytes"
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = [package_data, b""]
        urlopen.return_value = response
        update = docker_tray.AppUpdate(
            True,
            latest_version="0.3.0",
            package_url=(
                "https://github.com/spka/docker-tray/releases/download/v0.3.0/"
                "docker-tray_0.3.0_all.deb"
            ),
            package_digest=f"sha256:{hashlib.sha256(package_data).hexdigest()}",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = docker_tray.download_app_update(update, temp_dir)
            self.assertEqual(package_data, package_path.read_bytes())

        validate_package.assert_called_once_with(package_path, "0.3.0")

    @mock.patch.object(docker_tray, "validate_app_update_package")
    @mock.patch.object(docker_tray, "urlopen")
    def test_download_rejects_wrong_checksum(self, urlopen, _validate_package):
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = [b"wrong bytes", b""]
        urlopen.return_value = response
        update = docker_tray.AppUpdate(
            True,
            latest_version="0.3.0",
            package_url=(
                "https://github.com/spka/docker-tray/releases/download/v0.3.0/"
                "docker-tray_0.3.0_all.deb"
            ),
            package_digest=f"sha256:{'0' * 64}",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                docker_tray.download_app_update(update, temp_dir)

    @mock.patch.object(docker_tray, "download_app_update")
    @mock.patch.object(docker_tray.subprocess, "run")
    def test_app_upgrade_installs_downloaded_package_with_pkexec(self, run, download):
        update = docker_tray.AppUpdate(
            True,
            latest_version="0.3.0",
            package_url=(
                "https://github.com/spka/docker-tray/releases/download/v0.3.0/"
                "docker-tray_0.3.0_all.deb"
            ),
            package_digest=f"sha256:{'1' * 64}",
        )
        download.return_value = docker_tray.Path("/tmp/docker-tray_0.3.0_all.deb")
        run.return_value = subprocess.CompletedProcess([], 0)

        result = docker_tray.run_app_upgrade(update)

        self.assertEqual(0, result.returncode)
        self.assertEqual(
            [
                "pkexec",
                docker_tray.PRIVILEGED_HELPER,
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

        docker_tray.finish_app_upgrade(icon, result, None)

        self.assertTrue(icon._restart_after_upgrade)
        icon.stop.assert_called_once_with()

    @mock.patch.object(docker_tray, "update_tray_menu")
    def test_new_app_release_sends_only_one_desktop_notice(self, _update_menu):
        icon = mock.Mock()
        update = docker_tray.AppUpdate(
            True,
            latest_version="0.3.0",
            release_url="https://github.com/spka/docker-tray/releases/tag/v0.3.0",
        )

        docker_tray.set_update_state(
            icon,
            self.original_engine_update,
            self.original_image_updates,
            update,
        )
        docker_tray.set_update_state(
            icon,
            self.original_engine_update,
            self.original_image_updates,
            update,
        )

        icon.notify.assert_called_once_with(
            "Docker Tray 0.3.0 is available.",
            "Docker Tray",
        )

    @mock.patch.object(docker_tray, "update_tray_menu")
    @mock.patch.object(docker_tray.GLib, "idle_add", side_effect=run_idle_callback)
    @mock.patch.object(docker_tray, "check_image_updates", return_value=[])
    @mock.patch.object(docker_tray, "check_engine_update")
    @mock.patch.object(docker_tray, "check_app_update", side_effect=OSError("offline"))
    def test_transient_github_failure_preserves_existing_notice(
        self,
        _check_app,
        check_engine,
        _check_images,
        _idle_add,
        _update_menu,
    ):
        existing = docker_tray.AppUpdate(
            True,
            latest_version="0.3.0",
            release_url="https://github.com/spka/docker-tray/releases/tag/v0.3.0",
        )
        docker_tray.update_check_state["app_update"] = existing
        check_engine.return_value = self.original_engine_update

        docker_tray.run_update_check(mock.Mock())

        self.assertEqual(existing, docker_tray.get_app_update_snapshot())
        feedback = docker_tray.get_update_feedback_snapshot()
        self.assertFalse(feedback["checking"])
        self.assertIsNotNone(feedback["last_checked"])
        self.assertIn("offline", feedback["errors"][0])

    def test_update_feedback_distinguishes_checking_and_incomplete(self):
        docker_tray.update_check_state.update({
            "checking": True,
            "last_checked": None,
            "errors": (),
        })
        self.assertEqual("Checking for updates…", docker_tray.get_update_check_label())

        docker_tray.update_check_state.update({
            "checking": False,
            "last_checked": 100,
            "errors": ("registry offline",),
        })
        with mock.patch.object(docker_tray.time, "time", return_value=160):
            self.assertEqual(
                "Update check incomplete (1 minute ago)",
                docker_tray.get_update_check_label(),
            )


if __name__ == "__main__":
    unittest.main()

import json
import unittest
from unittest import mock

import docker_tray


def run_idle_callback(callback, *args):
    return callback(*args)


def github_response(tag_name, html_url=None):
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "tag_name": tag_name,
        "html_url": html_url or f"https://github.com/spka/docker-tray/releases/tag/{tag_name}",
    }).encode()
    return response


class AppUpdateTests(unittest.TestCase):
    def setUp(self):
        self.original_app_update = docker_tray.get_app_update_snapshot()
        self.original_engine_update, self.original_image_updates = docker_tray.get_update_state_snapshot()

    def tearDown(self):
        docker_tray.update_check_state.update({
            "app_update": self.original_app_update,
            "engine_update": self.original_engine_update,
            "image_updates": self.original_image_updates,
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
        urlopen.return_value = github_response("v0.3.0")

        update = docker_tray.check_app_update()

        self.assertTrue(update.available)
        self.assertEqual("0.3.0", update.latest_version)
        self.assertEqual(
            "https://github.com/spka/docker-tray/releases/tag/v0.3.0",
            update.release_url,
        )
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


if __name__ == "__main__":
    unittest.main()

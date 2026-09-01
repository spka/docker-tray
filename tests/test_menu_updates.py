import unittest
from unittest import mock

import docker_tray


class TrayMenuUpdateTests(unittest.TestCase):
    def setUp(self):
        state = docker_tray.tray_menu_update_state
        self.original = (state.pending, state.tracked_root)
        state.pending = False
        state.tracked_root = None

    def tearDown(self):
        state = docker_tray.tray_menu_update_state
        state.pending, state.tracked_root = self.original

    def test_background_refresh_only_marks_menu_dirty(self):
        icon = mock.Mock()

        docker_tray.update_tray_menu(icon)

        icon.update_menu.assert_not_called()
        self.assertTrue(docker_tray.tray_menu_update_state.pending)

    @mock.patch.object(docker_tray, "track_tray_menu_pre_show")
    def test_dirty_menu_refreshes_synchronously_before_show(self, track):
        icon = mock.Mock()
        calls = []
        icon._update_menu.__wrapped__ = lambda target: calls.append(target)
        docker_tray.tray_menu_update_state.pending = True

        changed = docker_tray.refresh_tray_menu_before_show(mock.Mock(), icon)

        self.assertTrue(changed)
        self.assertEqual([icon], calls)
        track.assert_called_once_with(icon)
        self.assertFalse(docker_tray.tray_menu_update_state.pending)

    def test_clean_menu_is_not_rebuilt_before_show(self):
        icon = mock.Mock()

        changed = docker_tray.refresh_tray_menu_before_show(mock.Mock(), icon)

        self.assertFalse(changed)
        icon._update_menu.assert_not_called()

    def test_pre_show_hook_tracks_exported_root(self):
        icon = mock.Mock()
        root = mock.Mock()
        server = mock.Mock()
        server.get_property.return_value = root
        icon._appindicator.get_property.return_value = server

        docker_tray.track_tray_menu_pre_show(icon)

        root.connect.assert_called_once_with(
            "about-to-show",
            docker_tray.refresh_tray_menu_before_show,
            icon,
        )


if __name__ == "__main__":
    unittest.main()

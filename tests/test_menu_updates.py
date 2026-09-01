import unittest
from unittest import mock

import docker_tray


class TrayMenuUpdateTests(unittest.TestCase):
    def setUp(self):
        state = docker_tray.tray_menu_update_state
        self.original = state.pending
        state.pending = False

    def tearDown(self):
        docker_tray.tray_menu_update_state.pending = self.original

    def test_background_refresh_only_marks_menu_dirty(self):
        icon = mock.Mock()

        docker_tray.update_tray_menu(icon)

        icon.update_menu.assert_not_called()
        self.assertTrue(docker_tray.tray_menu_update_state.pending)

    def test_manual_refresh_clears_dirty_state(self):
        icon = mock.Mock()
        docker_tray.tray_menu_update_state.pending = True

        docker_tray.refresh_tray_menu(icon, mock.Mock())

        icon.update_menu.assert_not_called()
        self.assertFalse(docker_tray.tray_menu_update_state.pending)


if __name__ == "__main__":
    unittest.main()

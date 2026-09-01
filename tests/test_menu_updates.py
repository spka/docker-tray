import unittest
from unittest import mock

import docker_tray


class TrayMenuUpdateTests(unittest.TestCase):
    def setUp(self):
        state = docker_tray.tray_menu_update_state
        self.original = (state.pending, state.menu_open, state.tracked_menu)
        state.pending = False
        state.menu_open = False
        state.tracked_menu = None

    def tearDown(self):
        state = docker_tray.tray_menu_update_state
        state.pending, state.menu_open, state.tracked_menu = self.original

    @mock.patch.object(docker_tray.GLib, "idle_add")
    def test_refresh_waits_while_menu_is_open(self, idle_add):
        icon = mock.Mock()
        docker_tray.tray_menu_update_state.pending = True
        docker_tray.tray_menu_update_state.menu_open = True

        docker_tray.run_pending_tray_menu_update(icon)

        icon.update_menu.assert_not_called()
        idle_add.assert_not_called()
        self.assertTrue(docker_tray.tray_menu_update_state.pending)

    @mock.patch.object(docker_tray.GLib, "idle_add")
    def test_closing_menu_schedules_deferred_refresh(self, idle_add):
        icon = mock.Mock()
        docker_tray.tray_menu_update_state.pending = True
        docker_tray.tray_menu_update_state.menu_open = True

        docker_tray.set_tray_menu_open(icon, False)

        idle_add.assert_called_once_with(docker_tray.run_pending_tray_menu_update, icon)

    @mock.patch.object(docker_tray.GLib, "idle_add")
    def test_closed_menu_refreshes_and_tracks_replacement(self, idle_add):
        icon = mock.Mock()
        docker_tray.tray_menu_update_state.pending = True

        docker_tray.run_pending_tray_menu_update(icon)

        icon.update_menu.assert_called_once_with()
        idle_add.assert_called_once_with(docker_tray.track_tray_menu_visibility, icon)
        self.assertFalse(docker_tray.tray_menu_update_state.pending)


if __name__ == "__main__":
    unittest.main()

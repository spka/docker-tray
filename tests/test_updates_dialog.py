import unittest
from unittest import mock

import docker_tray_updates_dialog as dialog_module
from docker_tray_platform import EngineUpdate, PlatformInfo
from docker_tray_update_backend import UpdateBackend
from docker_tray_update_service import UpdateService
from docker_tray_updates import AppUpdate


class UpdatesDialogTests(unittest.TestCase):
    def setUp(self):
        self.service = UpdateService(UpdateBackend("0.2.14", PlatformInfo("ubuntu", (), ())))
        self.open_uri = mock.Mock()
        self.dialog = dialog_module.UpdatesDialog(self.service, self.open_uri)
        self.dialog.controller = mock.Mock()
        self.widgets = {}

    def make_button(self, *, label):
        button = mock.Mock()
        self.widgets.setdefault(label, []).append(button)
        return button

    def render(self):
        with mock.patch.object(dialog_module, "Gtk") as gtk, mock.patch.object(
            dialog_module, "make_dialog_box"
        ), mock.patch.object(dialog_module, "add_bottom_button_row"):
            gtk.Button.side_effect = self.make_button
            self.dialog.show(None)

    def click(self, label, index=0):
        button = self.widgets[label][index]
        button.connect.call_args.args[1](button)

    def test_check_and_close_buttons_reach_their_controllers(self):
        with mock.patch.object(self.service, "start_update_check") as check:
            self.render()
            self.click("Check now")
            check.assert_called_once_with(None)
            self.click("Close")
            self.dialog.controller.destroy.assert_called_once()

    def test_each_image_button_keeps_its_own_image(self):
        self.service.update_check_state.image_updates = ["one:latest", "two:latest"]
        with mock.patch.object(self.service, "start_image_compose_pull") as update:
            self.render()
            self.click("Update + cleanup", 0)
            self.click("Update + cleanup", 1)
            self.assertEqual(
                ["one:latest", "two:latest"], [call.args[-1] for call in update.call_args_list]
            )

    def test_busy_operation_disables_all_upgrade_buttons(self):
        state = self.service.update_check_state
        state.app_update = AppUpdate(
            True, package_url="https://example.com", package_digest="sha256:abc"
        )
        state.engine_update = EngineUpdate(
            True, upgrade_label="Upgrade engine", upgrade_command=("test",)
        )
        state.image_updates = ["web:latest"]
        self.service.operation_state.pulling_images.add("web:latest")
        self.render()
        for label in ("Install update", "Upgrade engine", "Updating all...", "Pulling"):
            button = self.widgets[label][0]
            button.set_sensitive.assert_called_once_with(False)
            button.connect.assert_not_called()

    def test_uninstallable_release_opens_the_browser(self):
        self.service.update_check_state.app_update = AppUpdate(
            True,
            latest_version="0.3.0",
            release_url="https://github.com/spka/docker-tray/releases/tag/v0.3.0",
        )
        self.render()
        self.click("Open release")
        self.open_uri.assert_called_once_with(self.service.get_app_update_snapshot().release_url)

    def test_refresh_does_not_reopen_closed_window(self):
        self.dialog.controller.window = None
        with mock.patch.object(self.dialog, "show") as show:
            self.dialog.refresh(None)
            show.assert_not_called()


if __name__ == "__main__":
    unittest.main()

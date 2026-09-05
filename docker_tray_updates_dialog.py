"""GTK rendering and user actions for the updates dialog."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib
from docker_tray_ui import DialogController, make_dialog_box, add_bottom_button_row


class UpdatesDialog:
    def __init__(self, service, open_uri):
        self.service = service
        self.open_uri = open_uri
        self.controller = DialogController(
            "Docker Updates",
            (400, 200),
            on_clear=service.operation_state.clear_if_idle,
        )

    def refresh(self, context):
        if self.controller.window is not None:
            self.show(context)

    def show(self, icon):
        self.controller.ensure()
        box = make_dialog_box()
        app_update = self.service.get_app_update_snapshot()
        engine_update, image_updates = self.service.get_update_state_snapshot()
        update_action_running = (
            self.service.operation_state.app_upgrading
            or self.service.operation_state.engine_upgrading
            or bool(self.service.operation_state.pulling_images)
        )
        feedback = self.service.get_update_feedback_snapshot()
        check_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if feedback["checking"]:
            check_spinner = Gtk.Spinner()
            check_spinner.start()
            check_row.pack_start(check_spinner, False, False, 0)
        check_label = Gtk.Label(label=self.service.get_update_check_label())
        check_label.set_xalign(0)
        check_label.set_hexpand(True)
        check_row.pack_start(check_label, True, True, 0)
        check_button = Gtk.Button(label="Checking…" if feedback["checking"] else "Check now")
        check_button.set_sensitive(not feedback["checking"])
        if not feedback["checking"]:
            check_button.connect("clicked", lambda button: self.service.start_update_check(icon))
        check_row.pack_start(check_button, False, False, 0)
        box.pack_start(check_row, False, False, 0)
        for check_error in feedback["errors"]:
            error_label = Gtk.Label(label=f"⚠ {check_error}")
            error_label.set_xalign(0)
            error_label.set_line_wrap(True)
            box.pack_start(error_label, False, False, 0)
        if app_update.available:
            app_label = Gtk.Label(label="Docker Tray update available")
            app_label.set_xalign(0)
            box.pack_start(app_label, False, False, 0)
            version_label = Gtk.Label(label=f"{self.service.version} → {app_update.latest_version}")
            version_label.set_xalign(0)
            box.pack_start(version_label, False, False, 0)
            if app_update.can_install:
                app_upgrading = self.service.operation_state.app_upgrading
                install_button = Gtk.Button(
                    label="Installing..." if app_upgrading else "Install update"
                )
                install_button.set_sensitive(not update_action_running)
                if not update_action_running:
                    install_button.connect(
                        "clicked", lambda button: self.service.start_app_upgrade(icon)
                    )
                box.pack_start(install_button, False, False, 0)
            else:
                release_button = Gtk.Button(label="Open release")
                release_button.connect(
                    "clicked", lambda button, url=app_update.release_url: self.open_uri(url)
                )
                box.pack_start(release_button, False, False, 0)
        if engine_update.available:
            engine_label = Gtk.Label(label=f"{engine_update.package_name} update available")
            engine_label.set_xalign(0)
            box.pack_start(engine_label, False, False, 0)
            if engine_update.detail:
                detail = Gtk.Label(label=engine_update.detail)
                detail.set_xalign(0)
                box.pack_start(detail, False, False, 0)
            if engine_update.can_upgrade:
                engine_upgrading = self.service.operation_state.engine_upgrading
                upgrade_button = Gtk.Button(
                    label="Upgrading..." if engine_upgrading else engine_update.upgrade_label
                )
                upgrade_button.set_sensitive(not update_action_running)
                if not update_action_running:
                    upgrade_button.connect(
                        "clicked", lambda b: self.service.start_docker_engine_upgrade(icon)
                    )
                box.pack_start(upgrade_button, False, False, 0)
            else:
                detail = Gtk.Label(label="Use your system package manager to upgrade Docker.")
                detail.set_xalign(0)
                box.pack_start(detail, False, False, 0)
        if image_updates:
            images_label = Gtk.Label(label="Image updates available:")
            images_label.set_xalign(0)
            box.pack_start(images_label, False, False, 0)
            pull_in_progress = bool(self.service.operation_state.pulling_images)
            update_all_button = Gtk.Button(
                label="Updating all..." if pull_in_progress else "Update + cleanup all"
            )
            update_all_button.set_sensitive(not update_action_running)
            if not update_action_running:
                update_all_button.connect(
                    "clicked", lambda button: self.service.start_all_image_compose_pulls(icon)
                )
            box.pack_start(update_all_button, False, False, 0)
            for image in image_updates:
                is_pulling = image in self.service.operation_state.pulling_images
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                image_label = Gtk.Label(label=image)
                image_label.set_xalign(0)
                image_label.set_hexpand(True)
                image_label.set_line_wrap(True)
                pull_button = Gtk.Button(label="Pulling" if is_pulling else "Update + cleanup")
                pull_button.set_sensitive(not update_action_running)
                if not update_action_running:
                    pull_button.connect(
                        "clicked",
                        lambda button, pull_image=image: self.service.start_image_compose_pull(
                            button, icon, pull_image
                        ),
                    )
                row.pack_start(image_label, True, True, 0)
                row.pack_start(pull_button, False, False, 0)
                box.pack_start(row, False, False, 0)
        if self.service.operation_state.status:
            status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            if self.service.operation_state.status.startswith(
                (
                    "Downloading ",
                    "Installing ",
                    "Pulling ",
                    "Restarting ",
                    "Waiting ",
                    "Upgrading ",
                    "Removing ",
                )
            ):
                spinner = Gtk.Spinner()
                spinner.start()
                status_row.pack_start(spinner, False, False, 0)
            status_label = Gtk.Label(label=self.service.operation_state.status)
            status_label.set_xalign(0)
            status_label.set_line_wrap(True)
            status_row.pack_start(status_label, True, True, 0)
            box.pack_start(status_row, False, False, 0)
        if not app_update.available and (not engine_update.available) and (not image_updates):
            done_label = Gtk.Label(label="No updates pending.")
            done_label.set_xalign(0)
            box.pack_start(done_label, False, False, 0)
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda b: self.controller.destroy())
        close_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        close_row.set_halign(Gtk.Align.END)
        close_row.pack_start(close_button, False, False, 0)
        add_bottom_button_row(box, close_row)
        self.controller.set_content(box)
        return GLib.SOURCE_REMOVE

"""Small GTK lifecycle primitives shared by Docker Tray dialogs."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk


class DialogController:
    def __init__(self, title, default_size, on_clear=None):
        self.title = title
        self.default_size = default_size
        self.on_clear = on_clear
        self.window = None
        self.content = None

    def clear(self):
        self.window = None
        self.content = None
        if self.on_clear is not None:
            self.on_clear()
        return GLib.SOURCE_REMOVE

    def destroy(self):
        window = self.window
        if window is not None:
            window.destroy()
        else:
            self.clear()
        return GLib.SOURCE_REMOVE

    def ensure(self):
        if self.window is not None:
            self.window.present()
            return self.window

        window = Gtk.Window(title=self.title)
        window.set_default_size(*self.default_size)
        window.set_resizable(True)
        window.set_keep_above(True)
        window.set_skip_taskbar_hint(True)
        window.set_position(Gtk.WindowPosition.CENTER)
        window.connect("destroy", lambda _window: self.clear())
        self.window = window
        return window

    def set_content(self, content):
        window = self.window
        if window is None:
            return
        if self.content is not None:
            window.remove(self.content)
        self.content = content
        window.add(content)
        window.show_all()
        window.present()


def make_dialog_box():
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.set_border_width(16)
    return box


def add_bottom_button_row(box, buttons):
    spacer = Gtk.Box()
    spacer.set_vexpand(True)
    box.pack_start(spacer, True, True, 0)
    box.pack_start(buttons, False, False, 0)

import ast
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import docker_tray_autostart
import docker_tray_compose
import docker_tray_runtime
import docker_tray_ui


ROOT = Path(__file__).resolve().parents[1]


class RuntimeGatewayTests(unittest.TestCase):
    def test_application_routes_command_lists_through_gateway(self):
        tree = ast.parse((ROOT / "docker_tray.py").read_text())
        raw_docker_commands = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.List, ast.Tuple))
            and node.elts
            and isinstance(node.elts[0], ast.Constant)
            and node.elts[0].value == "docker"
        ]
        self.assertEqual([], raw_docker_commands)

    def test_packaged_gateway_is_used_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = Path(directory) / "docker"
            gateway.write_text("#!/bin/sh\n")
            gateway.chmod(0o755)
            with mock.patch.object(docker_tray_runtime, "PACKAGED_DOCKER_GATEWAY", gateway):
                self.assertEqual(
                    [str(gateway), "ps", "-a"],
                    docker_tray_runtime.docker_command("ps", "-a"),
                )

    def test_source_checkout_falls_back_to_docker_on_path(self):
        with mock.patch.object(
            docker_tray_runtime,
            "PACKAGED_DOCKER_GATEWAY",
            Path("/missing/docker-tray-gateway"),
        ):
            self.assertEqual(["docker", "ps"], docker_tray_runtime.docker_command("ps"))

    def test_transaction_command_names_the_privileged_action(self):
        self.assertEqual(
            ["pkexec", str(docker_tray_runtime.PRIVILEGED_HELPER), "cleanup"],
            docker_tray_runtime.privileged_command("cleanup"),
        )


class ExtractedFeatureTests(unittest.TestCase):
    def test_autostart_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_file = root / "user.desktop"
            system_file = root / "system.desktop"
            self.assertTrue(
                docker_tray_autostart.write_enabled(user_file, True, "docker-tray")
            )
            self.assertTrue(docker_tray_autostart.read_enabled(user_file, system_file))
            self.assertIn("Exec=docker-tray", user_file.read_text())

    def test_compose_scan_skips_hidden_and_dependency_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            wanted = home / "services" / "compose.yml"
            skipped = home / "node_modules" / "compose.yml"
            wanted.parent.mkdir()
            skipped.parent.mkdir()
            wanted.write_text("services: {}\n")
            skipped.write_text("services: {}\n")
            self.assertEqual(
                [wanted],
                docker_tray_compose.scan_files(home, home, {"node_modules"}),
            )


class DialogControllerTests(unittest.TestCase):
    @mock.patch.object(docker_tray_ui.Gtk, "Window")
    def test_dialog_reuses_window_and_replaces_content(self, window_factory):
        window = window_factory.return_value
        first = mock.Mock()
        second = mock.Mock()
        dialog = docker_tray_ui.DialogController("Test", (400, 200))

        self.assertIs(window, dialog.ensure())
        self.assertIs(window, dialog.ensure())
        dialog.set_content(first)
        dialog.set_content(second)

        window_factory.assert_called_once_with(title="Test")
        window.remove.assert_called_once_with(first)
        self.assertEqual(second, dialog.content)


if __name__ == "__main__":
    unittest.main()

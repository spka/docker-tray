import tempfile
import unittest
from pathlib import Path

import docker_tray_privileged as helper


class PrivilegedHelperTests(unittest.TestCase):
    def test_parses_sanitized_container_api_snapshot(self):
        self.assertEqual(
            [
                ("stopped", False, None),
                ("web", True, "8080"),
            ],
            helper.parse_container_api_data([
                {
                    "Names": ["/web"],
                    "State": "running",
                    "Ports": [
                        {"Type": "tcp", "IP": "0.0.0.0", "PublicPort": 8080},
                        {"Type": "tcp", "IP": "::", "PublicPort": 8080},
                    ],
                },
                {"Names": ["/stopped"], "State": "exited", "Ports": []},
            ]),
        )

    def test_container_api_snapshot_ignores_invalid_names_and_private_ports(self):
        self.assertEqual(
            [("internal", True, None)],
            helper.parse_container_api_data([
                {"Names": [], "State": "running", "Ports": []},
                {"Names": ["/bad name"], "State": "running", "Ports": []},
                {
                    "Names": ["/internal"],
                    "State": "running",
                    "Ports": [{"Type": "tcp", "PrivatePort": 80}],
                },
            ]),
        )

    def test_allows_expected_read_queries(self):
        helper.validate_read(["ps", "-a", "-q"])
        helper.validate_read([
            "inspect", "--format", "{{.Image}}", "a" * 64,
        ])
        helper.validate_read([
            "image", "inspect", "--format", "{{.Id}}", "example/app:latest",
        ])

    def test_rejects_arbitrary_read_command(self):
        with self.assertRaises(SystemExit):
            helper.validate_read(["inspect", "--format", "{{json .Config}}", "a" * 64])

    def test_allows_expected_write_actions(self):
        helper.validate_write(["restart", "web"], Path.home())
        helper.validate_write(["image", "rm", "sha256:" + "a" * 64], Path.home())

    def test_rejects_arbitrary_write_action(self):
        with self.assertRaises(SystemExit):
            helper.validate_write(["run", "--privileged", "alpine"], Path.home())

    def test_compose_file_must_be_inside_home(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            compose_file = Path(directory, "compose.yml")
            compose_file.touch()
            helper.validate_write(
                ["compose", "-f", str(compose_file), "up", "-d"],
                Path.home(),
            )
        with self.assertRaises(SystemExit):
            helper.validate_write(
                ["compose", "-f", "/etc/passwd", "up", "-d"],
                Path.home(),
            )


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
import contextlib
import hashlib
import io
import json
import os
import subprocess
from types import SimpleNamespace
from unittest import mock
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

    def test_rejects_transaction_commands_through_generic_write_mode(self):
        with self.assertRaises(SystemExit):
            helper.validate_write(
                ["image", "rm", "sha256:" + "a" * 64], Path.home()
            )

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

    @mock.patch.object(helper, "run_docker")
    def test_image_update_discovery_accepts_only_home_compose_targets(self, run_docker):
        image_id = "sha256:" + "b" * 64
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            compose_file = Path(directory, "compose.yml")
            compose_file.touch()
            run_docker.side_effect = [
                subprocess.CompletedProcess([], 0, "a" * 64 + "\n", ""),
                subprocess.CompletedProcess(
                    [],
                    0,
                    f"example:latest\t{image_id}\t{compose_file}\t{directory}\tweb\n"
                    f"example:latest\t{image_id}\t/etc/compose.yml\t/etc\tunsafe\n",
                    "",
                ),
            ]

            targets, replaced_ids = helper.discover_image_update(
                "example:latest", Path.home()
            )

        self.assertEqual(1, len(targets))
        self.assertEqual("web", targets[0][2])
        self.assertEqual({image_id}, replaced_ids)

    @mock.patch.object(helper, "get_used_image_ids", return_value=set())
    @mock.patch.object(helper, "wait_for_compose_service", return_value=True)
    @mock.patch.object(helper, "run_docker")
    @mock.patch.object(helper, "discover_image_update")
    def test_image_update_runs_pull_up_wait_and_safe_cleanup_once(
        self,
        discover,
        run_docker,
        wait_ready,
        _get_used,
    ):
        old_image = "sha256:" + "c" * 64
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            compose_file = Path(directory, "compose.yml")
            compose_file.touch()
            discover.return_value = [
                ((compose_file,), Path(directory), "web")
            ], {old_image}
            run_docker.return_value = subprocess.CompletedProcess([], 0, "", "")

            result = helper.update_compose_image("example:latest", Path.home())

        self.assertEqual((1, 1, ""), result)
        self.assertEqual(3, run_docker.call_count)
        self.assertIn("pull", run_docker.call_args_list[0].args[0])
        self.assertIn("up", run_docker.call_args_list[1].args[0])
        self.assertEqual(["image", "rm", old_image], run_docker.call_args_list[2].args[0])
        wait_ready.assert_called_once()

    @mock.patch.object(helper, "update_compose_image")
    def test_image_update_batch_continues_after_failure(self, update_image):
        update_image.side_effect = [(1, 1, ""), RuntimeError("pull failed")]
        output = io.StringIO()

        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            helper.run_image_updates(["one:latest", "two:latest"], Path.home())

        self.assertEqual(1, raised.exception.code)
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(messages[0]["success"])
        self.assertFalse(messages[1]["success"])
        self.assertEqual(2, update_image.call_count)

    @mock.patch.object(helper.subprocess, "run")
    def test_cleanup_runs_fixed_commands_in_one_helper_process(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "Done\n", "")

        helper.run_cleanup()

        self.assertEqual(
            [[helper.DOCKER, *command] for command in helper.PRUNE_COMMAND_ORDER],
            [call.args[0] for call in run.call_args_list],
        )

    @mock.patch.object(helper, "validate_update_metadata")
    def test_update_is_copied_and_hashed_from_open_descriptor(self, validate_metadata):
        package_data = b"trusted package bytes"
        user = SimpleNamespace(pw_uid=os.getuid())
        digest = f"sha256:{hashlib.sha256(package_data).hexdigest()}"
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            source = Path(directory, "update.deb")
            destination = Path(directory, "staged.deb")
            source.write_bytes(package_data)

            helper.stage_update_package(source, "0.2.7", digest, user, destination)

            self.assertEqual(package_data, destination.read_bytes())
            validate_metadata.assert_called_once_with(destination, "0.2.7")

    def test_update_rejects_invalid_digest_before_opening_package(self):
        user = SimpleNamespace(pw_uid=os.getuid())
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            with self.assertRaises(SystemExit):
                helper.stage_update_package(
                    Path(directory, "missing.deb"),
                    "0.2.7",
                    "untrusted",
                    user,
                    Path(directory, "staged.deb"),
                )


if __name__ == "__main__":
    unittest.main()

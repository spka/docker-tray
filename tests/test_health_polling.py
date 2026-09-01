import subprocess
import unittest
from unittest import mock

import docker_tray


class HealthPollingTests(unittest.TestCase):
    def setUp(self):
        self.original_level = docker_tray.container_health_state.level

    def tearDown(self):
        docker_tray.container_health_state.level = self.original_level

    @mock.patch.object(docker_tray, "run_docker_capture")
    def test_stats_command_failure_is_not_treated_as_no_containers(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=["docker", "stats"],
            returncode=1,
            stdout="",
            stderr="Cannot connect to the Docker daemon",
        )

        with self.assertRaisesRegex(RuntimeError, "Docker daemon is not running"):
            docker_tray.collect_stats_sample()

    @mock.patch.object(docker_tray, "update_tray_menu")
    @mock.patch.object(docker_tray, "collect_stats_sample", return_value=[])
    def test_no_running_containers_clears_warning_to_idle(self, _collect, update_menu):
        icon = mock.Mock()
        docker_tray.container_health_state.level = "critical"

        docker_tray.poll_container_stats_once(icon)

        self.assertEqual("idle", docker_tray.container_health_state.level)
        update_menu.assert_called_once_with(icon)

    @mock.patch.object(docker_tray, "update_tray_menu")
    @mock.patch.object(docker_tray, "collect_stats_sample", side_effect=RuntimeError("daemon unavailable"))
    def test_stats_failure_clears_warning_to_unknown(self, _collect, update_menu):
        icon = mock.Mock()
        docker_tray.container_health_state.level = "warning"

        docker_tray.poll_container_stats_once(icon)

        self.assertEqual("unknown", docker_tray.container_health_state.level)
        update_menu.assert_called_once_with(icon)

    @mock.patch.object(docker_tray, "update_tray_menu")
    @mock.patch.object(docker_tray, "collect_stats_sample", return_value=[])
    def test_unchanged_idle_state_does_not_rebuild_menu(self, _collect, update_menu):
        docker_tray.container_health_state.level = "idle"

        docker_tray.poll_container_stats_once(mock.Mock())

        update_menu.assert_not_called()

    @mock.patch.object(docker_tray, "update_tray_menu")
    def test_non_alerting_health_transition_does_not_rebuild_menu(self, update_menu):
        docker_tray.container_health_state.level = "ok"

        docker_tray.set_container_health_level(mock.Mock(), "idle")

        update_menu.assert_not_called()


if __name__ == "__main__":
    unittest.main()

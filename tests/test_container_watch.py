import json
import unittest

import docker_tray


class ContainerWatchTests(unittest.TestCase):
    def setUp(self):
        state = docker_tray.container_watch_state
        with state.lock:
            self.original = (state.ready, state.containers, state.error)
            state.ready = False
            state.containers = ()
            state.error = None

    def tearDown(self):
        state = docker_tray.container_watch_state
        with state.lock:
            state.ready, state.containers, state.error = self.original

    def test_parses_container_snapshot(self):
        containers, error = docker_tray.parse_container_watch_message(json.dumps({
            "containers": [["web", True, "8080"], ["worker", False, None]],
        }))
        self.assertEqual((("web", True, "8080"), ("worker", False, None)), containers)
        self.assertIsNone(error)

    def test_parses_helper_error(self):
        containers, error = docker_tray.parse_container_watch_message(
            '{"error":"daemon unavailable"}',
        )
        self.assertEqual((), containers)
        self.assertEqual("daemon unavailable", error)

    def test_rejects_malformed_snapshot(self):
        with self.assertRaises(ValueError):
            docker_tray.parse_container_watch_message('{"containers":[["web","yes",80]]}')

    def test_cached_snapshot_avoids_docker_subprocess(self):
        docker_tray.set_container_watch_state((("web", True, "8080"),))
        self.assertEqual([("web", True, "8080")], docker_tray.get_containers())


if __name__ == "__main__":
    unittest.main()

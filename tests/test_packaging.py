import unittest
from pathlib import Path

import docker_tray


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_versions_match(self):
        self.assertEqual("0.2.7", docker_tray.APP_VERSION)
        self.assertIn("pkgver=0.2.7", (ROOT / "packaging/arch/PKGBUILD").read_text())
        self.assertIn('version="${1:-0.2.7}"', (ROOT / "package-deb.sh").read_text())

    def test_arch_package_contains_security_integration(self):
        package = (ROOT / "packaging/arch/PKGBUILD").read_text()
        for expected in (
            '"polkit"',
            "docker_tray_privileged.py",
            "docker-tray-docker",
            "com.github.spka.docker-tray.policy",
            "PATH=/usr/lib/docker-tray:$PATH",
        ):
            self.assertIn(expected, package)

    def test_wrapper_uses_home_for_non_compose_commands(self):
        wrapper = (ROOT / "docker-tray-docker").read_text()
        self.assertIn('request_cwd=${HOME:-$PWD}', wrapper)
        self.assertIn('if [ "${1-}" = "compose" ]', wrapper)

    def test_policy_has_transaction_actions(self):
        policy = (ROOT / "com.github.spka.docker-tray.policy").read_text()
        self.assertIn("com.github.spka.docker-tray.cleanup", policy)
        self.assertIn("com.github.spka.docker-tray.install-update", policy)
        self.assertIn("com.github.spka.docker-tray.image-update", policy)

    def test_release_workflow_builds_attests_and_publishes(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn('tags:', workflow)
        self.assertIn('actions/attest@', workflow)
        self.assertIn('sha256sum', workflow)
        self.assertIn('gh release create', workflow)
        self.assertIn('gh release edit', workflow)

    def test_release_version_script_accepts_current_tag(self):
        import subprocess

        result = subprocess.run(
            [ROOT / "scripts/release-version", "v0.2.7"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual("0.2.7", result.stdout.strip())

    def test_release_version_script_rejects_mismatched_tag(self):
        import subprocess

        result = subprocess.run(
            [ROOT / "scripts/release-version", "v0.2.8"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Version mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path

import docker_tray


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_versions_match(self):
        self.assertEqual("0.2.12", docker_tray.APP_VERSION)
        self.assertIn("pkgver=0.2.7", (ROOT / "packaging/arch/PKGBUILD").read_text())
        self.assertIn('version="${1:-0.2.12}"', (ROOT / "package-deb.sh").read_text())

    def test_arch_package_contains_security_integration(self):
        package = (ROOT / "packaging/arch/PKGBUILD").read_text()
        for expected in (
            '"polkit"',
            "docker_tray_privileged.py",
            "docker-tray-docker",
            "com.github.spka.docker-tray.policy",
            "PATH=/usr/lib/docker-tray:$PATH",
            "archive/refs/tags/v${pkgver}.tar.gz",
            "sha256sums=(",
            "docker-tray.service",
        ):
            self.assertIn(expected, package)

    def test_arch_metadata_tracks_published_release(self):
        srcinfo = (ROOT / "packaging/arch/.SRCINFO").read_text()
        self.assertIn("pkgver = 0.2.7", srcinfo)
        self.assertIn("docker-tray-0.2.7.tar.gz", srcinfo)
        self.assertNotIn("SKIP", srcinfo)

    def test_packages_install_supervised_user_service(self):
        service = (ROOT / "docker-tray.service").read_text()
        self.assertIn("Restart=on-failure", service)
        self.assertIn("PartOf=graphical-session.target", service)
        deb = (ROOT / "package-deb.sh").read_text()
        arch = (ROOT / "packaging/arch/PKGBUILD").read_text()
        for package in (deb, arch):
            self.assertIn("usr/lib/systemd/user/docker-tray.service", package)
        self.assertIn("docker-tray.desktop", deb)
        self.assertIn("docker-tray-autostart.desktop", deb)
        for desktop_file in ("docker-tray.desktop", "docker-tray-autostart.desktop"):
            desktop = (ROOT / desktop_file).read_text()
            self.assertIn("Exec=systemctl --user start docker-tray.service", desktop)
        self.assertEqual(2, arch.count("Exec=systemctl --user start docker-tray.service"))

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
            [ROOT / "scripts/release-version", "v0.2.12"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual("0.2.12", result.stdout.strip())

    def test_release_version_script_rejects_mismatched_tag(self):
        import subprocess

        result = subprocess.run(
            [ROOT / "scripts/release-version", "v0.2.13"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Version mismatch", result.stderr)


if __name__ == "__main__":
    unittest.main()

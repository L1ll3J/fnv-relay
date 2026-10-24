"""Read-only bootstrap checks; never apt, download, authorize or extract here."""
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/bootstrap-ubuntu.sh'


class BootstrapTests(unittest.TestCase):
    def test_bash_syntax(self):
        subprocess.run(['bash', '-n', str(SCRIPT)], check=True, capture_output=True)

    def test_help_is_read_only_and_documents_modes(self):
        result = subprocess.run(['bash', str(SCRIPT), '--help'], check=True, capture_output=True, text=True)
        for option in ('--dry-run', '--extract-only', '--no-connect', '--skip-apt'):
            self.assertIn(option, result.stdout)

    @unittest.skipUnless(platform.system() == 'Linux' and platform.machine() == 'x86_64' and os.geteuid() != 0, 'Ubuntu user dry-run only')
    def test_dry_run_creates_nothing(self):
        release = Path('/etc/os-release').read_text()
        if 'ID=ubuntu' not in release or 'VERSION_ID="24.04"' not in release:
            self.skipTest('Bootstrap targets Ubuntu 24.04')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / 'Fallout NV.exe'
            game.write_bytes(b'fixture; never executed')
            destination = root / 'tools with spaces'
            state = root / 'private state'
            result = subprocess.run(['bash', str(SCRIPT), '--dry-run', '--game', str(game), '--tools-dir', str(destination), '--state-dir', str(state)], check=True, capture_output=True, text=True)
            self.assertFalse(destination.exists())
            self.assertFalse(state.exists())
            self.assertIn('download.microsoft.com', result.stdout)
            self.assertIn('wine32:i386', result.stdout)
            self.assertIn('doctor --prepare-wine', result.stdout)

    def test_no_proprietary_files_or_secret_arguments_in_recipe(self):
        text = SCRIPT.read_text()
        self.assertIn("--proto '=https' --proto-redir '=https'", text)
        self.assertIn('compiler_files_sha256', text)
        self.assertIn('Tools directory already exists', text)
        self.assertNotIn('auth fnv login', text)
        self.assertNotIn('auth notion login --token', text)


if __name__ == '__main__':
    unittest.main()

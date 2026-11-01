import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import package_release


class ReleaseSafetyTests(unittest.TestCase):
    def test_local_key_in_any_asset_blocks_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "local-server").mkdir()
            (root / "local-server/.env").write_text('DEEPL_AUTH_KEY="test-only-credential-value"\n')
            with patch.object(package_release, "ROOT", root):
                with self.assertRaises(ValueError):
                    package_release.validate({"content.js": b"test-only-credential-value"})
                package_release.validate({"content.js": b"no credentials here"})

    def test_nonempty_example_key_blocks_release(self):
        with self.assertRaises(ValueError):
            package_release.validate({"local-server/.env.example": b"AZURE_TRANSLATOR_KEY=accidental-key"})

    def test_allowlist_excludes_private_state(self):
        extension, server = package_release.release_files()
        for name in extension + server:
            self.assertNotIn(name, [".env", ".paid-usage.json", "HANDOFF.md"])
            self.assertNotIn(".venv", name)


if __name__ == "__main__":
    unittest.main()

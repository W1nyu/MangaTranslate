from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_models.py"
SPEC = importlib.util.spec_from_file_location("install_models", SCRIPT)
assert SPEC and SPEC.loader
INSTALL_MODELS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL_MODELS)


def package(source: str, target: str) -> SimpleNamespace:
    return SimpleNamespace(from_code=source, to_code=target)


class InstallPathTests(unittest.TestCase):
    def test_direct_model_is_preferred(self) -> None:
        direct = package("ja", "ko")
        path = INSTALL_MODELS.find_install_path([package("ja", "en"), package("en", "ko"), direct], "ja", "ko")
        self.assertEqual(path, [direct])

    def test_two_hop_pivot_is_selected_when_direct_model_is_missing(self) -> None:
        ja_en = package("ja", "en")
        en_ko = package("en", "ko")
        path = INSTALL_MODELS.find_install_path([ja_en, en_ko], "ja", "ko")
        self.assertEqual(path, [ja_en, en_ko])

    def test_missing_route_returns_none(self) -> None:
        self.assertIsNone(INSTALL_MODELS.find_install_path([package("en", "ko")], "ja", "ko"))


if __name__ == "__main__":
    unittest.main()

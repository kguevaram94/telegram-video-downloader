"""Pruebas de configuración relacionada con TMDb."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telegram_downloader import config as config_module


class AppConfigTmdbTests(unittest.TestCase):
    def test_loads_tmdb_settings_without_exposing_key_in_repr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / "config.env"
            env_path.write_text(
                "\n".join(
                    [
                        "API_ID=123",
                        "API_HASH=hash",
                        "TMDB_API_KEY=super-secret-key",
                        "TMDB_ENABLED=true",
                        "TMDB_CONFIDENCE_THRESHOLD=0.8",
                        "TMDB_AMBIGUITY_DELTA=0.1",
                        "TMDB_LANGUAGE=en-US",
                        f"TMDB_CACHE_FILE={root / 'cache.json'}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(config_module, "ROOT", root):
                with patch.dict(os.environ, {}, clear=True):
                    cfg = config_module.load_config()

            self.assertTrue(cfg.classification_enabled)
            self.assertEqual(cfg.tmdb_api_key, "super-secret-key")
            self.assertEqual(cfg.tmdb_confidence_threshold, 0.8)
            self.assertEqual(cfg.tmdb_ambiguity_delta, 0.1)
            self.assertEqual(cfg.tmdb_language, "en-US")
            self.assertNotIn("super-secret-key", repr(cfg))

    def test_classification_disabled_without_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / "config.env"
            env_path.write_text(
                "API_ID=123\nAPI_HASH=hash\nTMDB_ENABLED=true\n",
                encoding="utf-8",
            )

            with patch.object(config_module, "ROOT", root):
                with patch.dict(os.environ, {}, clear=True):
                    cfg = config_module.load_config()

            self.assertFalse(cfg.classification_enabled)


if __name__ == "__main__":
    unittest.main()

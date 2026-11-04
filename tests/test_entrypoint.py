from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from app.__main__ import build_parser, main


class ServerEntrypointTests(unittest.TestCase):
    def test_parser_accepts_overrides(self) -> None:
        args = build_parser().parse_args(
            ["--config", "config.json", "--host", "0.0.0.0", "--port", "9000"]
        )
        self.assertEqual(args.config, "config.json")
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 9000)

    def test_missing_configuration_file_exits_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main(["--config", str(missing)])

        self.assertEqual(code, 2)
        self.assertIn("Configuration error", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

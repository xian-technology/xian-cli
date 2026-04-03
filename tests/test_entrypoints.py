from __future__ import annotations

import unittest
from unittest.mock import patch

import xian_cli.__main__ as module_main


class ModuleEntrypointTests(unittest.TestCase):
    def test_python_module_entrypoint_delegates_to_cli_main(self) -> None:
        with patch("xian_cli.__main__.cli_main", return_value=7) as cli_main:
            exit_code = module_main.main(["doctor"])

        self.assertEqual(exit_code, 7)
        cli_main.assert_called_once_with(["doctor"])

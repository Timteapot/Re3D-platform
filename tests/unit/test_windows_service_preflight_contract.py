from __future__ import annotations

import re
import unittest
from pathlib import Path


class WindowsServicePreflightContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        project_root = Path(__file__).resolve().parents[2]
        cls.source = (
            project_root
            / "deploy"
            / "production"
            / "windows-services"
            / "test-target-host-prerequisites.ps1"
        ).read_text(encoding="utf-8")

    def test_pristine_winsw_uses_pe_version_metadata(self) -> None:
        self.assertIn("function Invoke-Re3DWinSWVersionCheck", self.source)
        self.assertIn("$versionInfo.FileVersion", self.source)
        self.assertIn("$versionInfo.ProductVersion", self.source)
        self.assertIn(
            '$description -cne "Windows Service Wrapper"',
            self.source,
        )

    def test_winsw_does_not_use_the_config_dependent_version_command(self) -> None:
        self.assertRegex(
            self.source,
            re.compile(
                r"Invoke-Re3DWinSWVersionCheck\s+-Path\s+\$WinSWPath"
            ),
        )
        self.assertNotRegex(
            self.source,
            re.compile(
                r'-Id\s+"winsw"[\s\S]{0,200}-Arguments\s+@\("version"\)'
            ),
        )


if __name__ == "__main__":
    unittest.main()

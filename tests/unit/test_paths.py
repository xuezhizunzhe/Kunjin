import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kunjin.paths import RuntimePaths


class RuntimePathsTest(unittest.TestCase):
    def test_runtime_paths_use_overrides_and_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with patch.dict(
                os.environ,
                {
                    "KUNJIN_DATA_DIR": str(root / "data"),
                    "KUNJIN_STATE_DIR": str(root / "state"),
                },
                clear=False,
            ):
                paths = RuntimePaths.from_environment().ensure()

            self.assertEqual(paths.database, root / "data" / "kunjin.db")
            self.assertEqual(paths.snapshots, root / "data" / "snapshots")
            self.assertEqual(paths.imports, root / "data" / "imports")
            self.assertEqual(paths.fund_documents, root / "data" / "fund-documents")
            self.assertEqual(paths.logs, root / "state" / "logs")
            mode = stat.S_IMODE(paths.database.parent.stat().st_mode)
            self.assertEqual(mode, 0o700)
            self.assertEqual(stat.S_IMODE(paths.imports.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(paths.fund_documents.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()

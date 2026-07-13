import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "install_launchd.py"
SPEC = importlib.util.spec_from_file_location("install_launchd", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LaunchdTest(unittest.TestCase):
    def test_plist_runs_weekdays_after_close(self) -> None:
        plist = MODULE.build_plist(Path("/project"), Path("/home"))

        self.assertEqual(plist["Label"], "com.kunjin.daily-sync")
        self.assertEqual(plist["EnvironmentVariables"]["PYTHONPATH"], "/project/src")
        self.assertEqual(
            [item["Weekday"] for item in plist["StartCalendarInterval"]],
            [2, 3, 4, 5, 6],
        )
        self.assertTrue(
            all(
                item["Hour"] == 18 and item["Minute"] == 30
                for item in plist["StartCalendarInterval"]
            )
        )


if __name__ == "__main__":
    unittest.main()

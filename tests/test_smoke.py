import unittest

from kunjin.cli import run


class SmokeTest(unittest.TestCase):
    def test_version_returns_json_contract(self) -> None:
        payload, exit_code, json_output = run(["--json", "version"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(json_output)
        self.assertEqual(payload["schema_version"], "1")
        self.assertEqual(payload["data"]["version"], "0.1.0")


if __name__ == "__main__":
    unittest.main()

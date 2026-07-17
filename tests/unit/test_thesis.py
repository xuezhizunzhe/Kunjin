import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from kunjin.models import InvestmentThesis
from kunjin.storage.repository import Repository


class ThesisTest(unittest.TestCase):
    def test_thesis_requires_invalidation_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "kunjin.db")
            repository.migrate()
            thesis = InvestmentThesis(
                "017811",
                "人工智能盈利改善可能支持中期表现",
                "12个月",
                "基金持续落后基准且经理风格发生漂移",
                datetime.now(timezone.utc),
            )

            thesis_id = repository.add_thesis(thesis)

            self.assertEqual(repository.list_theses("017811")[0].invalidation, thesis.invalidation)
            self.assertEqual(repository.get_thesis(thesis_id), thesis)
            self.assertIsNone(repository.get_thesis(thesis_id + 1))

    def test_empty_invalidation_is_rejected(self) -> None:
        thesis = InvestmentThesis("017811", "理由", "一年", "", datetime.now(timezone.utc))
        with self.assertRaisesRegex(ValueError, "invalidation"):
            thesis.validate()


if __name__ == "__main__":
    unittest.main()

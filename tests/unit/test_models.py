import unittest
from datetime import datetime, timezone
from decimal import Decimal

from kunjin.models import PositionObservation


class ModelsTest(unittest.TestCase):
    def test_invalid_fund_code_is_rejected(self) -> None:
        observation = PositionObservation(
            source_account_id="1",
            fund_code="123",
            fund_name="Invalid",
            shares=Decimal("1"),
            observed_at=datetime.now(timezone.utc),
        )

        with self.assertRaisesRegex(ValueError, "invalid fund code"):
            observation.validate()


if __name__ == "__main__":
    unittest.main()

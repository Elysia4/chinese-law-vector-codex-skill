import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import rank_search  # noqa: E402


class RankAuditTests(unittest.TestCase):
    def test_audit_flags_are_parsed_without_changing_default_layers(self):
        args = rank_search.parse_args(["合同解除", "--audit-json", "--strict"])
        self.assertTrue(args.audit_json)
        self.assertTrue(args.strict)
        self.assertEqual(args.layers, ("current",))


if __name__ == "__main__":
    unittest.main()

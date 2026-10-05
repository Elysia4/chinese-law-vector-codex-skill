import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from audit_state import clause_hash, corpus_fingerprint, compare_index, fingerprint_metadata  # noqa: E402


class AuditStateTests(unittest.TestCase):
    def setUp(self):
        self.docs = [
            ("民法典", "民法商法", "第一条", "总则", "本法条文", "民法典.md", 10, "现行有效", "current"),
            ("民法典", "民法商法", "第二条", "总则", "另一条文", "民法典.md", 11, "现行有效", "current"),
        ]

    def test_fingerprint_is_stable_and_changes_with_clause_content(self):
        first = corpus_fingerprint(self.docs)
        self.assertEqual(first, corpus_fingerprint(list(reversed(self.docs))))
        changed = list(self.docs)
        changed[0] = (*changed[0][:4], "修改后的条文", *changed[0][5:])
        self.assertNotEqual(first, corpus_fingerprint(changed))
        self.assertNotEqual(clause_hash(self.docs[0]), clause_hash(changed[0]))

    def test_compare_index_marks_missing_hash_as_stale(self):
        current = fingerprint_metadata(self.docs)
        self.assertEqual(compare_index(current, self.docs)["status"], "valid")
        self.assertEqual(compare_index({"model": "old"}, self.docs)["status"], "stale")


if __name__ == "__main__":
    unittest.main()

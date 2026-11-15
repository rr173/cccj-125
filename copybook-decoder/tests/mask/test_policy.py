"""Policy parsing and validation tests for the masking generator."""

import unittest

from copybook.mask import MaskPolicy, MaskPolicyError


def _policy(fields, **extra):
    data = {"fields": fields}
    data.update(extra)
    return MaskPolicy.from_dict(data)


def _rule(path, action, kind, **kw):
    rule = {"path": path, "action": action, "kind": kind}
    rule.update(kw)
    return rule


class TestPolicyParsing(unittest.TestCase):

    def test_minimal_pseudonym_rule(self):
        policy = _policy([_rule(["A"], "pseudonym", "account",
                                group="g")])
        self.assertEqual(policy.rules[0].path, ("A",))
        self.assertEqual(policy.rules[0].group, "g")
        self.assertEqual(policy.rules[0].on_null, "keep")
        self.assertIsNone(policy.key_fingerprint)

    def test_scope_defaults_to_path_text(self):
        policy = _policy([_rule(["A", "B"], "shift", "amount",
                                max_pct="5")])
        self.assertEqual(policy.rules[0].scope, "A/B")
        policy = _policy([_rule(["A", "B"], "shift", "amount",
                                max_pct="5", group="g")])
        self.assertEqual(policy.rules[0].scope, "g")

    def test_not_an_object(self):
        with self.assertRaises(MaskPolicyError):
            MaskPolicy.from_dict([1, 2])

    def test_unknown_top_level_key(self):
        with self.assertRaisesRegex(MaskPolicyError, "unknown top-level"):
            _policy([_rule(["A"], "pseudonym", "account", group="g")],
                    typo=1)

    def test_empty_fields(self):
        with self.assertRaisesRegex(MaskPolicyError, "non-empty 'fields'"):
            _policy([])

    def test_unknown_action(self):
        with self.assertRaisesRegex(MaskPolicyError, "action"):
            _policy([_rule(["A"], "encrypt", "account", group="g")])

    def test_unknown_rule_key(self):
        with self.assertRaisesRegex(MaskPolicyError, "unknown key"):
            _policy([_rule(["A"], "pseudonym", "account", group="g",
                           salt="x")])

    def test_bad_path_forms(self):
        for bad in (None, [], "", ["A", ""], ["A", 3], "A"):
            with self.assertRaises(MaskPolicyError, msg=f"path={bad!r}"):
                _policy([_rule(bad, "pseudonym", "account", group="g")])

    def test_duplicate_path_rejected(self):
        with self.assertRaisesRegex(MaskPolicyError, "duplicate"):
            _policy([_rule(["A"], "pseudonym", "account", group="g"),
                     _rule(["A"], "synthesize", "name")])

    def test_kind_action_mismatch(self):
        with self.assertRaisesRegex(MaskPolicyError, "kind"):
            _policy([_rule(["A"], "pseudonym", "name", group="g")])
        with self.assertRaisesRegex(MaskPolicyError, "kind"):
            _policy([_rule(["A"], "synthesize", "amount")])
        with self.assertRaisesRegex(MaskPolicyError, "kind"):
            _policy([_rule(["A"], "shift", "account", max_pct="5")])

    def test_pseudonym_requires_group(self):
        with self.assertRaisesRegex(MaskPolicyError, "group"):
            _policy([_rule(["A"], "pseudonym", "account")])

    def test_bucketize_rejects_group(self):
        with self.assertRaisesRegex(MaskPolicyError, "group"):
            _policy([_rule(["A"], "bucketize", "amount",
                           boundaries=["1"], group="g")])

    def test_shift_amount_requires_exactly_one_bound(self):
        with self.assertRaisesRegex(MaskPolicyError, "exactly one"):
            _policy([_rule(["A"], "shift", "amount")])
        with self.assertRaisesRegex(MaskPolicyError, "exactly one"):
            _policy([_rule(["A"], "shift", "amount", max_pct="5",
                           max_abs="10")])

    def test_max_pct_range(self):
        for bad in ("0", "-1", "100.01", "abc"):
            with self.assertRaises(MaskPolicyError, msg=f"pct={bad}"):
                _policy([_rule(["A"], "shift", "amount", max_pct=bad)])
        policy = _policy([_rule(["A"], "shift", "amount", max_pct="100")])
        self.assertEqual(str(policy.rules[0].max_pct), "100")

    def test_max_abs_must_be_positive(self):
        with self.assertRaises(MaskPolicyError):
            _policy([_rule(["A"], "shift", "amount", max_abs="0")])

    def test_shift_date_requires_max_days(self):
        with self.assertRaisesRegex(MaskPolicyError, "max_days"):
            _policy([_rule(["A"], "shift", "date")])
        for bad in (0, -3, "5", 2.5, True):
            with self.assertRaises(MaskPolicyError,
                                   msg=f"max_days={bad!r}"):
                _policy([_rule(["A"], "shift", "date", max_days=bad)])

    def test_date_format_validated(self):
        with self.assertRaisesRegex(MaskPolicyError, "format"):
            _policy([_rule(["A"], "shift", "date", max_days=1,
                           format="")])
        policy = _policy([_rule(["A"], "shift", "date", max_days=1)])
        self.assertEqual(policy.rules[0].fmt, "%Y-%m-%d")

    def test_bucketize_boundaries(self):
        with self.assertRaisesRegex(MaskPolicyError, "boundaries"):
            _policy([_rule(["A"], "bucketize", "amount", boundaries=[])])
        with self.assertRaisesRegex(MaskPolicyError, "increasing"):
            _policy([_rule(["A"], "bucketize", "amount",
                           boundaries=["10", "5"])])
        with self.assertRaisesRegex(MaskPolicyError, "increasing"):
            _policy([_rule(["A"], "bucketize", "amount",
                           boundaries=["5", "5"])])
        with self.assertRaises(MaskPolicyError):
            _policy([_rule(["A"], "bucketize", "amount",
                           boundaries=["x"])])
        policy = _policy([_rule(["A"], "bucketize", "amount",
                                boundaries=["5", "10.50"])])
        self.assertEqual(policy.rules[0].boundaries[1][0], "10.50")

    def test_bucketize_date_granularity(self):
        with self.assertRaisesRegex(MaskPolicyError, "granularity"):
            _policy([_rule(["A"], "bucketize", "date",
                           granularity="week")])
        policy = _policy([_rule(["A"], "bucketize", "date",
                                granularity="quarter")])
        self.assertEqual(policy.rules[0].granularity, "quarter")

    def test_null_empty_actions(self):
        with self.assertRaisesRegex(MaskPolicyError, "on_null"):
            _policy([_rule(["A"], "pseudonym", "account", group="g",
                           on_null="drop")])
        with self.assertRaisesRegex(MaskPolicyError, "on_empty"):
            _policy([_rule(["A"], "pseudonym", "account", group="g",
                           on_empty="drop")])
        policy = _policy([_rule(["A"], "pseudonym", "account", group="g",
                                on_null="reject", on_empty="reject")])
        self.assertEqual(policy.rules[0].on_null, "reject")

    def test_key_fingerprint_format(self):
        with self.assertRaisesRegex(MaskPolicyError, "key_fingerprint"):
            _policy([_rule(["A"], "pseudonym", "account", group="g")],
                    key_fingerprint="md5:abc")
        policy = _policy(
            [_rule(["A"], "pseudonym", "account", group="g")],
            key_fingerprint="sha256:0123456789abcdef")
        self.assertEqual(policy.key_fingerprint,
                         "sha256:0123456789abcdef")


if __name__ == "__main__":
    unittest.main()

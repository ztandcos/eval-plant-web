import unittest

from evalplant.attribution_bench import _validate_universal


class UniversalAttributionTest(unittest.TestCase):
    def test_every_candidate_is_reviewed_before_selecting_a_decisive_step(self):
        result = _validate_universal(
            {
                "attributable": True,
                "first_causal_error_step": 1,
                "decisive_failure_step": 2,
                "primary_evidence_step_ids": [1, 2],
                "confidence": 0.8,
                "candidate_reviews": [
                    {
                        "step_id": 1,
                        "status": "SUPPORTED",
                        "causal_roles": ["FIRST_CAUSAL_ERROR"],
                    },
                    {
                        "step_id": 2,
                        "status": "SUPPORTED",
                        "causal_roles": ["DECISIVE_FAILURE"],
                    },
                ],
            },
            [{"step_index": 1}, {"step_index": 2}],
            [1, 2],
        )
        self.assertEqual(result["decisive_failure_step"], 2)
        self.assertTrue(result["evidence_valid"])


if __name__ == "__main__":
    unittest.main()

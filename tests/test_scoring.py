from __future__ import annotations

from dataclasses import replace
import unittest

from bugagent.demo import build_demo_bundle
from bugagent.domain import Ticket, VerdictStatus
from bugagent.scoring import assess_evidence, assess_ticket


class EvidenceScoringTests(unittest.TestCase):
    def test_two_matching_clean_replays_are_required_for_reproduction(self) -> None:
        bundle = build_demo_bundle()
        candidate, replay_one, replay_two = bundle.evidence
        assessment = assess_evidence(bundle.ticket, candidate, (replay_one, replay_two))

        self.assertEqual(assessment.verdict.status, VerdictStatus.REPRODUCED)
        self.assertEqual(assessment.verdict.evidence_score, 100)

    def test_promising_candidate_is_inconclusive_until_replayed(self) -> None:
        bundle = build_demo_bundle()
        candidate = bundle.evidence[0]
        assessment = assess_evidence(bundle.ticket, candidate)

        self.assertEqual(assessment.verdict.status, VerdictStatus.INCONCLUSIVE)
        self.assertEqual(assessment.verdict.evidence_score, 90)

    def test_setup_failure_cannot_be_called_a_reproduction(self) -> None:
        bundle = build_demo_bundle()
        candidate = bundle.evidence[0]
        invalid = replace(candidate, setup_valid=False)
        assessment = assess_evidence(bundle.ticket, invalid)

        self.assertEqual(assessment.verdict.status, VerdictStatus.NOT_REPRODUCED)
        self.assertIn("Candidate environment setup was invalid.", assessment.verdict.disqualifiers)

    def test_missing_ticket_input_produces_targeted_questions(self) -> None:
        verdict = assess_ticket(Ticket(id="NO-INPUT", title="", body="", repo_ref=""))

        self.assertIsNotNone(verdict)
        assert verdict is not None
        self.assertEqual(verdict.status, VerdictStatus.NEED_INFO)
        self.assertEqual(len(verdict.blocking_questions), 3)


if __name__ == "__main__":
    unittest.main()

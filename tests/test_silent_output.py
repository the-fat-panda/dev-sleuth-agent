from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from bugagent.agent import InvestigationOrchestrator, ScriptedInvestigationClient
from bugagent.artifacts import ArtifactStore
from bugagent.domain import CandidateTest, MinorValue, SilentOutputProof, TextValue, Ticket, VerdictStatus
from bugagent.sandbox.docker import CommandResult, SandboxRun


_CONTRACT = (
    "Mercato's convention is that tax is charged on the amount the customer actually "
    "pays for goods — i.e. the subtotal *after* order-level discounts and coupons."
)
_PROBE = '''import json
from decimal import Decimal

from mercato.money import Money
from mercato.pricing.discounts import Coupon
from mercato.pricing.engine import PricingEngine, PricingLine
from mercato.pricing.tax import TaxPolicy


def test_discount_reduces_taxable_amount():
    policy = TaxPolicy(region_rates={"US-CA": Decimal("0.08")})
    engine = PricingEngine(tax_policy=policy)
    quote = engine.quote(
        [PricingLine("SKU", "Item", Money.of("50.00"), 1)],
        coupons=[Coupon.percentage("SAVE10", 10)],
        region="US-CA",
    )
    print("BUGAGENT_OBSERVATION " + json.dumps({"tax_minor": quote.tax.minor, "total_minor": quote.total.minor}, sort_keys=True))
    assert quote.tax.minor == 360
    assert quote.total.minor == 4860
'''

_INFERRED_PROBE = '''import json
from decimal import Decimal

from mercato.money import Money
from mercato.pricing.discounts import Coupon
from mercato.pricing.engine import PricingEngine, PricingLine
from mercato.pricing.tax import TaxPolicy


def test_discount_reduces_taxable_amount():
    tax_policy = TaxPolicy(default_rate=Decimal("0.08"))
    engine = PricingEngine(tax_policy=tax_policy)
    quote = engine.quote(
        [PricingLine("SKU", "Item", Money(5000, "USD"), 1)],
        coupons=[Coupon.percentage("SAVE10", 10)],
        shipping=Money(0, "USD"),
    )
    print("BUGAGENT_OBSERVATION " + json.dumps({"tax_minor": quote.tax.minor, "total_minor": quote.total.minor}, sort_keys=True))
    assert quote.tax.minor == 360
    assert quote.total.minor == 4860
'''


class ObservingSandbox:
    def run(self, repo_root: Path, candidate_path: Path) -> SandboxRun:
        preflight = CommandResult(("docker", "run"), 0, "1 test collected", "", False)
        execution = CommandResult(
            ("docker", "run"),
            1,
            'BUGAGENT_OBSERVATION {"tax_minor": 400, "total_minor": 4900}\n',
            'E AssertionError\ntests/bugagent_generated/test_tax.py:20: AssertionError\n',
            False,
        )
        return SandboxRun("sha256:" + "c" * 64, preflight, execution, candidate_path.name)


class SilentOutputTests(unittest.TestCase):
    def test_grounded_tax_mismatch_is_reproduced_after_two_matching_replays(self) -> None:
        with _tax_repository() as root:
            bundle = InvestigationOrchestrator(
                ScriptedInvestigationClient((_candidate(),)), ObservingSandbox()
            ).investigate(_ticket(), root, "tax-fixture")
            artifact_path = ArtifactStore(root / "runs").write(bundle)
            stored_evidence = json.loads((artifact_path / "evidence.json").read_text(encoding="utf-8"))[0]

        self.assertEqual(bundle.verdict.status, VerdictStatus.REPRODUCED)
        self.assertEqual(bundle.verdict.evidence_score, 100)
        self.assertEqual(len(bundle.evidence), 3)
        proof = bundle.evidence[0].silent_output
        assert proof is not None
        self.assertTrue(proof.probe_verified)
        self.assertEqual({value.name: value.minor for value in proof.expected_values}, {"tax_minor": 360, "total_minor": 4860})
        self.assertEqual({value.name: value.minor for value in proof.observed_values}, {"tax_minor": 400, "total_minor": 4900})
        self.assertEqual(stored_evidence["silent_output"]["contract_path"], "mercato/pricing/tax.py")
        self.assertTrue(stored_evidence["silent_output"]["probe_verified"])

    def test_model_selected_wrong_expected_value_is_not_trusted(self) -> None:
        wrong = _candidate(expected_tax=400, expected_total=4900)
        with _tax_repository() as root:
            bundle = InvestigationOrchestrator(
                ScriptedInvestigationClient((wrong,)), ObservingSandbox(), max_attempts=1
            ).investigate(_ticket(), root, "tax-fixture")

        self.assertNotEqual(bundle.verdict.status, VerdictStatus.REPRODUCED)
        self.assertEqual(bundle.verdict.evidence_score, 0)
        proof = bundle.evidence[0].silent_output
        assert proof is not None
        self.assertFalse(proof.probe_verified)
        self.assertIn("do not match", proof.verification_error or "")

    def test_nonsensical_probe_is_not_promoted_by_a_valid_contract(self) -> None:
        invalid = _candidate(content="def test_nonsense():\n    assert 1 == 2\n")
        with _tax_repository() as root:
            bundle = InvestigationOrchestrator(
                ScriptedInvestigationClient((invalid,)), ObservingSandbox(), max_attempts=1
            ).investigate(_ticket(), root, "tax-fixture")

        self.assertNotEqual(bundle.verdict.status, VerdictStatus.REPRODUCED)
        proof = bundle.evidence[0].silent_output
        assert proof is not None
        self.assertFalse(proof.probe_verified)
        self.assertIn("public .quote", proof.verification_error or "")

    def test_verified_test_protocol_recovers_omitted_model_metadata(self) -> None:
        recovered = _candidate(content=_INFERRED_PROBE, include_proof=False)
        with _tax_repository() as root:
            bundle = InvestigationOrchestrator(
                ScriptedInvestigationClient((recovered,)), ObservingSandbox()
            ).investigate(_ticket(), root, "tax-fixture")

        self.assertEqual(bundle.verdict.status, VerdictStatus.REPRODUCED)
        proof = bundle.evidence[0].silent_output
        assert proof is not None
        self.assertTrue(proof.probe_verified)
        self.assertEqual({value.name: value.minor for value in proof.expected_values}, {"tax_minor": 360, "total_minor": 4860})


def _candidate(
    *,
    expected_tax: int = 360,
    expected_total: int = 4860,
    content: str = _PROBE,
    include_proof: bool = True,
) -> CandidateTest:
    return CandidateTest(
        path="tests/bugagent_generated/test_tax.py",
        content=content,
        hypothesis="Tax is calculated before, rather than after, the coupon discount.",
        expected_symptom="Tax is 400 minor units instead of the contract-backed 360 minor units.",
        public_api_claims=("PricingEngine.quote", "TaxPolicy", "Coupon.percentage"),
        silent_output=SilentOutputProof(
            policy_id="tax_after_discounts_v1",
            contract_path="mercato/pricing/tax.py",
            contract_anchor=_CONTRACT,
            input_values=(
                TextValue("subtotal_minor", "5000"),
                TextValue("discount_minor", "500"),
                TextValue("tax_rate", "0.08"),
                TextValue("shipping_minor", "0"),
                TextValue("currency", "USD"),
            ),
            expected_values=(MinorValue("tax_minor", expected_tax), MinorValue("total_minor", expected_total)),
            observed_fields=("tax_minor", "total_minor"),
        ) if include_proof else None,
    )


def _ticket() -> Ticket:
    return Ticket(
        "TAX-1",
        "Discounted order tax is too high",
        "A 10% coupon appears not to reduce the taxable amount.",
        "mercato@tax-fixture",
    )


class _tax_repository:
    def __enter__(self) -> Path:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        tax = root / "mercato" / "pricing" / "tax.py"
        tax.parent.mkdir(parents=True)
        tax.write_text(f'"""{_CONTRACT}"""\n', encoding="utf-8")
        return root

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._temporary.cleanup()


if __name__ == "__main__":
    unittest.main()

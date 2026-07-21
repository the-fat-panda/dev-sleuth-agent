"""Independent, contract-backed evidence for silent wrong-output investigations."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re

from bugagent.domain import CandidateTest, MinorValue, SilentOutputEvidence, SilentOutputProof, TextValue
from bugagent.sandbox.docker import SandboxRun


_OBSERVATION_MARKER = re.compile(r"BUGAGENT_OBSERVATION\s+(\{[^\r\n]+\})")
_TAX_POLICY_ID = "tax_after_discounts_v1"
_TAX_CONTRACT_PATH = "mercato/pricing/tax.py"
_TAX_CONTRACT_ANCHOR = (
    "Mercato's convention is that tax is charged on the amount the customer actually "
    "pays for goods — i.e. the subtotal after order-level discounts and coupons."
)
_FREE_SHIPPING_POLICY_ID = "free_shipping_tiers_v1"
_FREE_SHIPPING_CONTRACT_PATH = "mercato/pricing/engine.py"
_FREE_SHIPPING_CONTRACT_ANCHOR = (
    "discounted subtotal >= $350 -> shipping is free "
    "discounted subtotal >= $250 -> 25% off the shipping fee "
    "discounted subtotal >= $150 -> 50% off the shipping fee "
    "otherwise -> the full shipping fee applies "
    "The tiers are checked from the highest threshold downward"
)


@dataclass(frozen=True, slots=True)
class GroundedSilentOutput:
    proof: SilentOutputProof
    contract_sha256: str | None
    expected_values: tuple[MinorValue, ...]
    valid: bool
    error: str | None = None


def ground_silent_output(candidate: CandidateTest, repo_root: Path) -> GroundedSilentOutput | None:
    """Resolve a supplied or statically recoverable claim through a repository contract."""
    proof = (
        candidate.silent_output
        or _infer_tax_after_discounts_proof(candidate.content)
        or _infer_free_shipping_tiers_proof(candidate.content)
    )
    if proof is None:
        return None
    contract_hash: str | None = None
    try:
        _validate_claim_shape(proof)
        content = _read_contract(repo_root, proof.contract_path)
        contract_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        expected = _evaluate_supported_contract(proof, content)
        if dict((value.name, value.minor) for value in proof.expected_values) != dict(
            (value.name, value.minor) for value in expected
        ):
            raise ValueError("Candidate expected values do not match the deterministic repository contract oracle.")
    except ValueError as error:
        return GroundedSilentOutput(proof, contract_hash, proof.expected_values, False, str(error))
    return GroundedSilentOutput(proof, contract_hash, expected, True)


def silent_evidence_from_run(
    candidate: CandidateTest,
    grounded: GroundedSilentOutput | None,
    run: SandboxRun,
) -> SilentOutputEvidence | None:
    """Join repository grounding, a verified probe source, and sandbox-observed values."""
    if grounded is None:
        return None
    proof = grounded.proof
    if not grounded.valid:
        return _evidence(grounded, (), False, grounded.error)
    source_error = _verify_probe_source(candidate.content, proof)
    if source_error:
        return _evidence(grounded, (), False, source_error)
    if not run.test_failed or run.execution is None:
        return _evidence(grounded, (), False, "The probe did not produce the required failing assertion.")
    observed, observation_error = _observed_values(run.execution.stdout, proof.observed_fields)
    if observation_error:
        return _evidence(grounded, (), False, observation_error)
    if dict((value.name, value.minor) for value in observed) == dict(
        (value.name, value.minor) for value in grounded.expected_values
    ):
        return _evidence(grounded, observed, False, "The product output matches the grounded expected values.")
    return _evidence(grounded, observed, True, None)


def same_silent_output(candidate: SilentOutputEvidence | None, replay: SilentOutputEvidence | None) -> bool:
    """A replay must reproduce the exact grounded expected-versus-actual mismatch."""
    if candidate is None or replay is None or not candidate.probe_verified or not replay.probe_verified:
        return False
    return (
        candidate.policy_id == replay.policy_id
        and candidate.contract_path == replay.contract_path
        and candidate.contract_sha256 == replay.contract_sha256
        and candidate.expected_values == replay.expected_values
        and candidate.observed_values == replay.observed_values
    )


def _infer_tax_after_discounts_proof(content: str) -> SilentOutputProof | None:
    """Recover a proof only from a narrow, auditable public pricing-test protocol.

    This is deliberately not general source-code interpretation. It permits a model to
    omit redundant JSON metadata only when the test itself exposes every input needed
    by the engine-owned oracle, uses a configured public TaxPolicy, emits a typed
    observation, and asserts the derived fields directly.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    assignments = {
        target.id: node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    policies = {
        name: _default_tax_rate(value)
        for name, value in assignments.items()
        if isinstance(value, ast.Call) and _call_name(value.func) == "TaxPolicy"
    }
    engines = {
        name: _engine_policy_name(value)
        for name, value in assignments.items()
        if isinstance(value, ast.Call) and _call_name(value.func) == "PricingEngine"
    }
    quote_call = assignments.get("quote")
    if not isinstance(quote_call, ast.Call) or not isinstance(quote_call.func, ast.Attribute):
        return None
    if quote_call.func.attr != "quote" or not isinstance(quote_call.func.value, ast.Name):
        return None
    policy_name = engines.get(quote_call.func.value.id)
    rate = policies.get(policy_name) if policy_name else None
    if rate is None:
        return None
    inputs = _quote_inputs(quote_call)
    if inputs is None:
        return None
    subtotal, discount, shipping, currency = inputs
    tax = int((Decimal(subtotal - discount) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return SilentOutputProof(
        policy_id=_TAX_POLICY_ID,
        contract_path=_TAX_CONTRACT_PATH,
        contract_anchor=_TAX_CONTRACT_ANCHOR,
        input_values=(
            TextValue("subtotal_minor", str(subtotal)),
            TextValue("discount_minor", str(discount)),
            TextValue("tax_rate", str(rate)),
            TextValue("shipping_minor", str(shipping)),
            TextValue("currency", currency),
        ),
        expected_values=(MinorValue("tax_minor", tax), MinorValue("total_minor", subtotal - discount + tax + shipping)),
        observed_fields=("tax_minor", "total_minor"),
    )


def _infer_free_shipping_tiers_proof(content: str) -> SilentOutputProof | None:
    """Recover a narrow, zero-tax quote protocol for the documented shipping tiers.

    The deterministic oracle accepts no coupons and a public ``TaxPolicy``
    configured to zero, keeping the claim limited to the shipping contract
    rather than attempting to interpret all pricing behaviour.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    assignments = {
        target.id: node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    policies = {
        name: _default_tax_rate(value)
        for name, value in assignments.items()
        if isinstance(value, ast.Call) and _call_name(value.func) == "TaxPolicy"
    }
    engines = {
        name: _engine_policy_name(value)
        for name, value in assignments.items()
        if isinstance(value, ast.Call) and _call_name(value.func) == "PricingEngine"
    }
    quote_call = assignments.get("quote")
    if not isinstance(quote_call, ast.Call) or not isinstance(quote_call.func, ast.Attribute):
        return None
    if quote_call.func.attr != "quote" or not isinstance(quote_call.func.value, ast.Name):
        return None
    policy_name = engines.get(quote_call.func.value.id)
    if policy_name is None or policies.get(policy_name) != Decimal("0"):
        return None
    if _keyword_value(quote_call, "coupon") is not None or _keyword_value(quote_call, "coupons") is not None:
        return None
    inputs = _free_shipping_inputs(quote_call)
    if inputs is None:
        return None
    subtotal, shipping, currency = inputs
    shipping_due = _shipping_due(subtotal, shipping)
    return SilentOutputProof(
        policy_id=_FREE_SHIPPING_POLICY_ID,
        contract_path=_FREE_SHIPPING_CONTRACT_PATH,
        contract_anchor=_FREE_SHIPPING_CONTRACT_ANCHOR,
        input_values=(
            TextValue("subtotal_minor", str(subtotal)),
            TextValue("discount_minor", "0"),
            TextValue("shipping_minor", str(shipping)),
            TextValue("tax_rate", "0"),
            TextValue("currency", currency),
        ),
        expected_values=(
            MinorValue("shipping_minor", shipping_due),
            MinorValue("total_minor", subtotal + shipping_due),
        ),
        observed_fields=("shipping_minor", "total_minor"),
    )


def _default_tax_rate(call: ast.Call) -> Decimal | None:
    for keyword in call.keywords:
        if keyword.arg == "default_rate":
            return _decimal_literal(keyword.value)
    return None


def _engine_policy_name(call: ast.Call) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == "tax_policy" and isinstance(keyword.value, ast.Name):
            return keyword.value.id
    return None


def _quote_inputs(call: ast.Call) -> tuple[int, int, int, str] | None:
    lines_node = call.args[0] if call.args else _keyword_value(call, "lines")
    if not isinstance(lines_node, (ast.List, ast.Tuple)) or not lines_node.elts:
        return None
    line_values = [_pricing_line_values(item) for item in lines_node.elts]
    if any(value is None for value in line_values):
        return None
    subtotal = sum(value[0] * value[1] for value in line_values if value is not None)
    currencies = {value[2] for value in line_values if value is not None}
    if len(currencies) != 1:
        return None
    coupon = _single_percentage_coupon(_keyword_value(call, "coupons") or _keyword_value(call, "coupon"))
    if coupon is None:
        return None
    shipping_node = _keyword_value(call, "shipping")
    shipping = 0 if shipping_node is None else _money_minor(shipping_node)
    if shipping is None:
        return None
    return subtotal, subtotal * coupon // 100, shipping, currencies.pop()


def _free_shipping_inputs(call: ast.Call) -> tuple[int, int, str] | None:
    lines_node = call.args[0] if call.args else _keyword_value(call, "lines")
    if not isinstance(lines_node, (ast.List, ast.Tuple)) or not lines_node.elts:
        return None
    line_values = [_pricing_line_values(item) for item in lines_node.elts]
    if any(value is None for value in line_values):
        return None
    subtotal = sum(value[0] * value[1] for value in line_values if value is not None)
    currencies = {value[2] for value in line_values if value is not None}
    shipping_node = _keyword_value(call, "shipping")
    shipping_value = _money_value(shipping_node) if shipping_node else None
    if len(currencies) != 1 or shipping_value is None or shipping_value[1] not in currencies:
        return None
    return subtotal, shipping_value[0], currencies.pop()


def _shipping_due(discounted_subtotal: int, shipping: int) -> int:
    """The documented USD tier table, expressed in minor units."""
    if discounted_subtotal >= 35_000:
        return 0
    if discounted_subtotal >= 25_000:
        return _round_half_up_minor(shipping, Decimal("0.75"))
    if discounted_subtotal >= 15_000:
        return _round_half_up_minor(shipping, Decimal("0.5"))
    return shipping


def _round_half_up_minor(amount: int, rate: Decimal) -> int:
    return int((Decimal(amount) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _pricing_line_values(node: ast.AST) -> tuple[int, int, str] | None:
    if not isinstance(node, ast.Call) or _call_name(node.func) != "PricingLine":
        return None
    price_node = node.args[2] if len(node.args) >= 3 else _keyword_value(node, "unit_price")
    quantity_node = node.args[3] if len(node.args) >= 4 else _keyword_value(node, "quantity")
    money = _money_value(price_node) if price_node else None
    quantity = _integer_constant(quantity_node) if quantity_node else None
    if money is None or quantity is None or quantity < 1:
        return None
    return money[0], quantity, money[1]


def _single_percentage_coupon(node: ast.AST | None) -> int | None:
    coupon = node
    if isinstance(node, (ast.List, ast.Tuple)):
        if len(node.elts) != 1:
            return None
        coupon = node.elts[0]
    if not isinstance(coupon, ast.Call) or not isinstance(coupon.func, ast.Attribute):
        return None
    if coupon.func.attr != "percentage" or _call_name(coupon.func.value) != "Coupon":
        return None
    percent_node = coupon.args[1] if len(coupon.args) >= 2 else _keyword_value(coupon, "percent")
    percent = _integer_constant(percent_node) if percent_node else None
    return percent if percent is not None and 0 < percent <= 100 else None


def _money_value(node: ast.AST) -> tuple[int, str] | None:
    if not isinstance(node, ast.Call) or _call_name(node.func) != "Money":
        return None
    amount_node = node.args[0] if node.args else _keyword_value(node, "amount_minor")
    amount = _integer_constant(amount_node) if amount_node else None
    currency_node = node.args[1] if len(node.args) >= 2 else _keyword_value(node, "currency")
    currency = _string_constant(currency_node) if currency_node else "USD"
    if amount is None or amount < 0 or currency is None or len(currency) != 3 or not currency.isalpha():
        return None
    return amount, currency.upper()


def _money_minor(node: ast.AST) -> int | None:
    value = _money_value(node)
    return value[0] if value else None


def _decimal_literal(node: ast.AST) -> Decimal | None:
    if not isinstance(node, ast.Call) or _call_name(node.func) != "Decimal" or len(node.args) != 1:
        return None
    raw = _string_constant(node.args[0])
    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value >= 0 else None


def _keyword_value(node: ast.Call, name: str) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _call_name(node: ast.AST) -> str | None:
    return node.id if isinstance(node, ast.Name) else None


def _string_constant(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _validate_claim_shape(proof: SilentOutputProof) -> None:
    if _duplicate_names(proof.input_values) or _duplicate_names(proof.expected_values):
        raise ValueError("Silent-output inputs and expected values must have unique names.")
    if proof.policy_id == _TAX_POLICY_ID:
        if proof.contract_path != _TAX_CONTRACT_PATH:
            raise ValueError(f"Policy {_TAX_POLICY_ID!r} must cite {_TAX_CONTRACT_PATH!r}.")
        if _normalize(proof.contract_anchor) != _normalize(_TAX_CONTRACT_ANCHOR):
            raise ValueError("Candidate contract anchor does not exactly identify the supported repository policy.")
        if tuple(proof.observed_fields) != ("tax_minor", "total_minor"):
            raise ValueError("The tax policy probe must observe tax_minor and total_minor in that order.")
        return
    if proof.policy_id == _FREE_SHIPPING_POLICY_ID:
        if proof.contract_path != _FREE_SHIPPING_CONTRACT_PATH:
            raise ValueError(f"Policy {_FREE_SHIPPING_POLICY_ID!r} must cite {_FREE_SHIPPING_CONTRACT_PATH!r}.")
        if _normalize(proof.contract_anchor) != _normalize(_FREE_SHIPPING_CONTRACT_ANCHOR):
            raise ValueError("Candidate contract anchor does not exactly identify the supported repository policy.")
        if tuple(proof.observed_fields) != ("shipping_minor", "total_minor"):
            raise ValueError("The free-shipping probe must observe shipping_minor and total_minor in that order.")
        return
    raise ValueError(f"Unsupported silent-output policy: {proof.policy_id!r}.")


def _read_contract(repo_root: Path, relative_path: str) -> str:
    if not relative_path or Path(relative_path).is_absolute():
        raise ValueError("Contract path must be a relative repository path.")
    path = (repo_root.resolve() / relative_path).resolve()
    try:
        path.relative_to(repo_root.resolve())
    except ValueError as error:
        raise ValueError("Contract path escapes the selected repository.") from error
    if not path.is_file():
        raise ValueError("The cited repository contract file does not exist.")
    if path.stat().st_size > 20_000:
        raise ValueError("The cited repository contract file is too large to ground safely.")
    return path.read_text(encoding="utf-8", errors="replace")


def _evaluate_supported_contract(proof: SilentOutputProof, content: str) -> tuple[MinorValue, ...]:
    if proof.policy_id == _FREE_SHIPPING_POLICY_ID:
        return _evaluate_free_shipping_contract(proof, content)
    if _normalize(_TAX_CONTRACT_ANCHOR) not in _normalize(content):
        raise ValueError("The cited source no longer contains the repository's post-discount tax contract.")
    values = {value.name: value.value for value in proof.input_values}
    expected_names = {"tax_minor", "total_minor"}
    if set(values) != {"subtotal_minor", "discount_minor", "tax_rate", "shipping_minor", "currency"}:
        raise ValueError("Tax policy inputs must be subtotal_minor, discount_minor, tax_rate, shipping_minor, and currency.")
    if {value.name for value in proof.expected_values} != expected_names:
        raise ValueError("Tax policy expected values must be tax_minor and total_minor.")
    if not values["currency"].isalpha() or len(values["currency"]) != 3:
        raise ValueError("Tax policy currency must be a three-letter ISO-style code.")
    try:
        subtotal = int(values["subtotal_minor"])
        discount = int(values["discount_minor"])
        shipping = int(values["shipping_minor"])
        rate = Decimal(values["tax_rate"])
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Tax policy inputs must contain integer minor amounts and a decimal tax rate.") from error
    if subtotal < 0 or discount < 0 or discount > subtotal or shipping < 0 or rate < 0:
        raise ValueError("Tax policy inputs are outside the supported non-negative amount range.")
    discounted = subtotal - discount
    tax = int((Decimal(discounted) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return (MinorValue("tax_minor", tax), MinorValue("total_minor", discounted + tax + shipping))


def _evaluate_free_shipping_contract(proof: SilentOutputProof, content: str) -> tuple[MinorValue, ...]:
    if _normalize(_FREE_SHIPPING_CONTRACT_ANCHOR) not in _normalize(content):
        raise ValueError("The cited source no longer contains the repository's free-shipping tier contract.")
    values = {value.name: value.value for value in proof.input_values}
    if set(values) != {"subtotal_minor", "discount_minor", "shipping_minor", "tax_rate", "currency"}:
        raise ValueError(
            "Free-shipping inputs must be subtotal_minor, discount_minor, shipping_minor, tax_rate, and currency."
        )
    if {value.name for value in proof.expected_values} != {"shipping_minor", "total_minor"}:
        raise ValueError("Free-shipping expected values must be shipping_minor and total_minor.")
    try:
        subtotal = int(values["subtotal_minor"])
        discount = int(values["discount_minor"])
        shipping = int(values["shipping_minor"])
        tax_rate = Decimal(values["tax_rate"])
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Free-shipping inputs must contain integer minor amounts and a decimal tax rate.") from error
    if values["currency"] != "USD":
        raise ValueError("The documented free-shipping tiers are USD-only.")
    if subtotal < 0 or discount != 0 or shipping < 0 or tax_rate != 0:
        raise ValueError("Free-shipping proof supports a zero-tax, no-discount quote only.")
    shipping_due = _shipping_due(subtotal, shipping)
    return (MinorValue("shipping_minor", shipping_due), MinorValue("total_minor", subtotal + shipping_due))


def _verify_probe_source(content: str, proof: SilentOutputProof) -> str | None:
    try:
        tree = ast.parse(content)
    except SyntaxError as error:
        return f"Generated probe is not valid Python: {error.msg}."
    quote_assigned = any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "quote" for target in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "quote"
        for node in ast.walk(tree)
    )
    if not quote_assigned:
        return "Silent-output probe must assign quote from a public .quote() call."
    expected = {value.name: value.minor for value in proof.expected_values}
    observed = _probe_observation_fields(tree)
    if observed != {name: _field_from_name(name) for name in proof.observed_fields}:
        return "Silent-output probe must print the required direct quote field observations."
    asserted = _probe_assertions(tree)
    for name, amount in expected.items():
        if asserted.get(_field_from_name(name)) != amount:
            return f"Silent-output probe must assert quote.{_field_from_name(name)}.minor == {amount}."
    return None


def _probe_observation_fields(tree: ast.AST) -> dict[str, str]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "print":
            continue
        if not node.args or not isinstance(node.args[0], ast.BinOp) or not isinstance(node.args[0].op, ast.Add):
            continue
        left, right = node.args[0].left, node.args[0].right
        if not isinstance(left, ast.Constant) or not isinstance(left.value, str) or "BUGAGENT_OBSERVATION" not in left.value:
            continue
        if not isinstance(right, ast.Call) or not isinstance(right.func, ast.Attribute) or right.func.attr != "dumps":
            continue
        if not right.args or not isinstance(right.args[0], ast.Dict):
            continue
        fields: dict[str, str] = {}
        for key, value in zip(right.args[0].keys, right.args[0].values, strict=True):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                return {}
            field = _quote_minor_field(value)
            if field is None:
                return {}
            fields[key.value] = field
        return fields
    return {}


def _probe_assertions(tree: ast.AST) -> dict[str, int]:
    assertions: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert) or not isinstance(node.test, ast.Compare):
            continue
        if len(node.test.ops) != 1 or not isinstance(node.test.ops[0], ast.Eq) or len(node.test.comparators) != 1:
            continue
        left, right = node.test.left, node.test.comparators[0]
        field = _quote_minor_field(left)
        amount = _integer_constant(right)
        if field is None or amount is None:
            field, amount = _quote_minor_field(right), _integer_constant(left)
        if field is not None and amount is not None:
            assertions[field] = amount
    return assertions


def _quote_minor_field(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Attribute) or node.attr != "minor":
        return None
    if not isinstance(node.value, ast.Attribute):
        return None
    field = node.value
    if not isinstance(field.value, ast.Name) or field.value.id != "quote":
        return None
    return field.attr


def _integer_constant(node: ast.AST) -> int | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool) else None


def _observed_values(stdout: str, expected_fields: tuple[str, ...]) -> tuple[tuple[MinorValue, ...], str | None]:
    matches = _OBSERVATION_MARKER.findall(stdout)
    if len(matches) != 1:
        return (), "Probe output must contain exactly one BUGAGENT_OBSERVATION JSON record."
    try:
        payload = json.loads(matches[0])
    except json.JSONDecodeError:
        return (), "Probe observation is not valid JSON."
    if not isinstance(payload, dict) or set(payload) != set(expected_fields):
        return (), "Probe observation fields do not match the grounded contract."
    values: list[MinorValue] = []
    for field in expected_fields:
        value = payload[field]
        if not isinstance(value, int) or isinstance(value, bool):
            return (), "Probe observation values must be integer minor units."
        values.append(MinorValue(field, value))
    return tuple(values), None


def _evidence(
    grounded: GroundedSilentOutput,
    observed: tuple[MinorValue, ...],
    verified: bool,
    error: str | None,
) -> SilentOutputEvidence:
    proof = grounded.proof
    return SilentOutputEvidence(
        policy_id=proof.policy_id,
        contract_path=proof.contract_path,
        contract_sha256=grounded.contract_sha256,
        contract_anchor=proof.contract_anchor,
        input_values=proof.input_values,
        expected_values=grounded.expected_values,
        observed_values=observed,
        probe_verified=verified,
        verification_error=error,
    )


def _duplicate_names(values: tuple[TextValue, ...] | tuple[MinorValue, ...]) -> bool:
    names = [value.name for value in values]
    return len(names) != len(set(names))


def _field_from_name(name: str) -> str:
    return name.removesuffix("_minor")


def _normalize(value: str) -> str:
    return " ".join(value.replace("*", "").split()).casefold()

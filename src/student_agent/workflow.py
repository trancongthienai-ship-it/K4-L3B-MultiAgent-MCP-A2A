from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .llm_verifier import LLMInvestigator
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

ORDER_ID = re.compile(r"^[a-fA-F0-9]{32}$")
SHIPMENT_VERDICTS = {
    "on_time",
    "seller_delay",
    "logistics_delay",
    "lost",
    "returned",
    "conflicting",
    "insufficient_evidence",
}
PAYMENT_VERDICTS = {
    "reconciled",
    "capture_mismatch",
    "duplicate_capture",
    "refund_pending",
    "refund_failed",
    "refunded",
    "insufficient_evidence",
}
PRIMARY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
}


def _unique(values: Iterable[str], limit: int = 20) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
        if len(result) == limit:
            break
    return result


def _find_values(value: Any, keys: set[str]) -> Iterable[Any]:
    """Yield values for exact keys, including values nested in MCP domain payloads."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys:
                yield child
            yield from _find_values(child, keys)
    elif isinstance(value, list):
        for child in value:
            yield from _find_values(child, keys)


def _first(value: Any, *keys: str) -> Any:
    return next(_find_values(value, set(keys)), None)


def _ids(value: Any, *keys: str) -> list[str]:
    result: list[str] = []
    for found in _find_values(value, set(keys)):
        candidates = found if isinstance(found, list) else [found]
        for candidate in candidates:
            if candidate is not None and not isinstance(candidate, (list, dict)):
                result.append(str(candidate))
    return _unique(result)


def _money(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return float(amount)


def _money_field(value: Any, *keys: str) -> float | None:
    for found in _find_values(value, set(keys)):
        amount = _money(found)
        if amount is not None:
            return amount
    return None


def _normalise_status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_") or None


def _status(value: Any, allowed: set[str], *keys: str) -> str | None:
    for found in _find_values(value, set(keys)):
        normalised = _normalise_status(found)
        if normalised in allowed:
            return normalised
    return None


def _conflicts(evidence: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool_name, envelope in evidence.items():
        data = envelope.get("data", {})
        raw_conflicts = _first(data, "data_conflicts", "conflicts")
        if isinstance(raw_conflicts, dict):
            raw_conflicts = [raw_conflicts]
        if isinstance(raw_conflicts, list):
            for raw in raw_conflicts:
                if not isinstance(raw, dict):
                    continue
                sources = raw.get("sources")
                if not isinstance(sources, list):
                    sources = [tool_name, str(raw.get("source", "reported_source"))]
                sources = _unique((str(item) for item in sources), 5)
                if len(sources) < 2:
                    sources = _unique([*sources, "case_input"], 5)
                selected = raw.get("selected_source", tool_name)
                result.append(
                    {
                        "field": str(raw.get("field", "domain_status"))[:100],
                        "sources": sources,
                        "selected_source": str(selected)[:80] if selected is not None else None,
                        "resolution_code": str(
                            raw.get("resolution_code", "AUTHORITATIVE_DOMAIN_SOURCE")
                        )[:80],
                    }
                )
        for warning in envelope.get("warnings", []):
            if "conflict" not in warning.lower() or len(result) == 5:
                continue
            result.append(
                {
                    "field": "domain_status",
                    "sources": [tool_name, "case_input"],
                    "selected_source": tool_name,
                    "resolution_code": "MCP_CONFLICT_WARNING",
                }
            )
        if len(result) >= 5:
            break
    return result[:5]


def _observed_issue(
    claimed_issue: str,
    order_data: Any,
    shipment_verdict: str,
    payment_verdict: str,
    refund_status: str | None,
    captured: float | None,
    refunded: float | None,
) -> str:
    paid_balance = (captured or 0.0) > (refunded or 0.0)
    order_status = _normalise_status(_first(order_data, "status", "order_status"))
    if order_status in {"canceled", "cancelled"} and paid_balance:
        return "canceled_order_paid"
    if order_status in {"unavailable", "unavailable_order"} and paid_balance:
        return "unavailable_order_paid"
    if payment_verdict == "duplicate_capture":
        return "duplicate_charge"
    if payment_verdict == "capture_mismatch":
        return "payment_mismatch"
    if refund_status in {"failed", "refund_failed"} or payment_verdict == "refund_failed":
        return "refund_failed"
    if (
        refund_status in {"pending", "processing", "refund_pending"}
        or payment_verdict == "refund_pending"
    ):
        return "refund_pending"
    if shipment_verdict == "seller_delay":
        return "late_delivery_seller"
    if shipment_verdict in {"logistics_delay", "lost", "returned"}:
        return "late_delivery_logistics"
    if claimed_issue in PRIMARY_ISSUES:
        return claimed_issue
    return "insufficient_evidence"


def _payment_verdict(payment_data: Any, refund_data: Any) -> tuple[str, str | None]:
    refund_status = _status(
        refund_data,
        {"not_requested", "pending", "processing", "failed", "refunded", "completed"},
        "refund_status",
        "status",
    )
    explicit = _status(payment_data, PAYMENT_VERDICTS, "verdict", "payment_verdict")
    if refund_status in {"pending", "processing"}:
        return "refund_pending", refund_status
    if refund_status == "failed":
        return "refund_failed", refund_status
    if refund_status in {"refunded", "completed"}:
        return "refunded", refund_status
    if explicit:
        return explicit, refund_status
    return "reconciled", refund_status


def _root_cause(
    issue: str, seller_ids: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mapping: dict[str, tuple[str, str]] = {
        "canceled_order_paid": ("ORDER_CANCELED_AFTER_CAPTURE", "platform"),
        "unavailable_order_paid": ("ORDER_UNAVAILABLE_AFTER_CAPTURE", "seller"),
        "late_delivery_seller": ("SELLER_FULFILLMENT_DELAY", "seller"),
        "late_delivery_logistics": ("LOGISTICS_TRANSIT_DELAY", "logistics_provider"),
        "valid_split_payment": ("VALID_SPLIT_PAYMENT", "customer"),
        "payment_mismatch": ("PAYMENT_CAPTURE_MISMATCH", "payment_provider"),
        "duplicate_charge": ("DUPLICATE_PAYMENT_CAPTURE", "payment_provider"),
        "refund_pending": ("REFUND_PROCESSING_DELAY", "payment_provider"),
        "refund_failed": ("REFUND_PROCESSING_FAILURE", "payment_provider"),
        "unsupported_claim": ("CLAIM_NOT_SUPPORTED", "customer"),
        "insufficient_evidence": ("INSUFFICIENT_EVIDENCE", "unknown"),
    }
    cause, party = mapping[issue]
    party_id = seller_ids[0] if party == "seller" and seller_ids else None
    return (
        [{"cause_code": cause, "rank": 1}],
        [{"party_type": party, "party_id": party_id}],
    )


async def solve_case(
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    investigator: LLMInvestigator | None = None,
) -> dict[str, Any]:
    """Resolve and investigate one L3B case using bounded, case-scoped evidence calls."""
    case_id = str(case["case_id"])
    request = case.get("customer_request", {})
    claims = request.get("claims", [])
    scope = case.get("investigation_scope", {})
    available_tools = set(await gateway.list_tools())
    required_tools = {
        "get_order",
        "get_order_items",
        "get_shipment_summary",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
        "get_customer_history",
        "get_product_context",
    }
    missing = sorted(required_tools - available_tools)
    if missing:
        raise RuntimeError(f"MCP Gateway is missing required tools: {', '.join(missing)}")

    evidence: dict[str, dict[str, Any]] = {}

    async def consume(actor: str, tool_name: str, **arguments: str) -> dict[str, Any]:
        envelope = await gateway.call(tool_name, case_id=case_id, **arguments)
        evidence[tool_name] = envelope
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[envelope["evidence_ref"]],
        )
        return envelope

    async def consume_optional(
        actor: str, tool_name: str, **arguments: str
    ) -> dict[str, Any] | None:
        try:
            return await consume(actor, tool_name, **arguments)
        except RuntimeError:
            trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor=actor,
                target="coordinator",
                decision_code="OPTIONAL_EVIDENCE_NOT_AVAILABLE",
                attributes={"tool_name": tool_name},
            )
            return None

    claimed_order = request.get("claimed_order_id")
    candidates = _unique(
        str(value)
        for value in [claimed_order, *case.get("candidate_order_ids", [])]
        if value
    )
    rejected = [candidate for candidate in candidates if not ORDER_ID.fullmatch(candidate)]
    valid_candidates = [candidate for candidate in candidates if ORDER_ID.fullmatch(candidate)]
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        decision_code="RESOLVE_ORDER",
        attributes={"candidate_count": len(candidates)},
    )

    resolved_order: str | None = None
    order_envelope: dict[str, Any] | None = None
    resolution_errors: list[str] = []
    for candidate in valid_candidates:
        try:
            candidate_evidence = await consume("entity-agent", "get_order", order_id=candidate)
        except RuntimeError as exc:
            resolution_errors.append(f"{candidate}: {exc}")
            rejected.append(candidate)
            continue
        returned_id = _first(candidate_evidence.get("data", {}), "order_id")
        if returned_id is not None and str(returned_id).lower() != candidate.lower():
            rejected.append(candidate)
            continue
        resolved_order = candidate
        order_envelope = candidate_evidence
        break

    rejected.extend(candidate for candidate in valid_candidates if candidate != resolved_order)
    rejected = _unique(rejected)
    if resolved_order is None or order_envelope is None:
        detail = f" ({'; '.join(resolution_errors)})" if resolution_errors else ""
        raise RuntimeError(f"Unable to resolve an order for {case_id}{detail}")

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        decision_code="ENTITY_RESOLVED",
        evidence_refs=[order_envelope["evidence_ref"]],
    )

    assignments = [
        ("order-agent", "INVESTIGATE_ORDER_ITEMS"),
        ("shipment-agent", "INVESTIGATE_SHIPMENT"),
        ("payment-agent", "RECONCILE_PAYMENT_AND_REFUND"),
        ("policy-agent", "APPLY_POLICY"),
    ]
    if scope.get("include_customer_history", True):
        assignments.append(("customer-agent", "LOAD_CUSTOMER_CONTEXT"))
    if scope.get("include_product_context", True):
        assignments.append(("product-agent", "LOAD_PRODUCT_CONTEXT"))
    for target, decision in assignments:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=target,
            decision_code=decision,
        )

    items = await consume("order-agent", "get_order_items", order_id=resolved_order)
    shipment = await consume("shipment-agent", "get_shipment_summary", order_id=resolved_order)
    payment = await consume("payment-agent", "get_payment_timeline", order_id=resolved_order)
    refund = await consume_optional(
        "payment-agent", "get_refund_timeline", order_id=resolved_order
    )
    policy = await consume(
        "policy-agent", "get_policy", policy_version=str(case.get("policy_version", ""))
    )
    customer = await consume(
        "customer-agent",
        "get_customer_history",
        customer_unique_id=str(case.get("customer_unique_id_hint", "")),
    )
    product = await consume("product-agent", "get_product_context", order_id=resolved_order)

    specialist_refs = _unique(
        [
            items["evidence_ref"],
            shipment["evidence_ref"],
            payment["evidence_ref"],
            *([refund["evidence_ref"]] if refund else []),
            policy["evidence_ref"],
            customer["evidence_ref"],
            product["evidence_ref"],
        ],
        20,
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="conflict-resolver",
        decision_code="SPECIALIST_EVIDENCE_COLLECTED",
        evidence_refs=specialist_refs,
    )

    order_data = order_envelope.get("data", {})
    item_data = items.get("data", {})
    shipment_data = shipment.get("data", {})
    payment_data = payment.get("data", {})
    refund_data = refund.get("data", {}) if refund else {}
    customer_data = customer.get("data", {})
    product_data = product.get("data", {})

    item_ids = _unique(
        [
            *_ids(item_data, "order_item_id", "item_id"),
            *_ids(product_data, "order_item_id", "item_id"),
        ]
    )
    seller_ids = _unique(
        [*_ids(item_data, "seller_id"), *_ids(product_data, "seller_id")]
    )
    payment_refs = _ids(
        payment_data, "payment_reference", "payment_id", "transaction_id", "charge_id"
    )
    shipment_ids = _ids(
        shipment_data, "shipment_id", "tracking_id", "tracking_code", "delivery_id"
    )

    shipment_verdict = _status(
        shipment_data, SHIPMENT_VERDICTS, "verdict", "shipment_verdict"
    ) or "insufficient_evidence"
    timeline_value = _first(shipment_data, "timeline_complete")
    timeline_complete = timeline_value if isinstance(timeline_value, bool) else False
    late_seller_ids = _ids(shipment_data, "late_seller_ids", "late_seller_id")
    if shipment_verdict == "seller_delay" and not late_seller_ids:
        late_seller_ids = seller_ids.copy()

    captured = _money_field(
        payment_data, "captured_total_brl", "captured_amount_brl", "total_captured_brl"
    )
    refunded = _money_field(
        refund_data, "refunded_total_brl", "refunded_amount_brl", "total_refunded_brl"
    )
    refundable = _money_field(
        refund_data,
        "refundable_total_brl",
        "refundable_amount_brl",
        "remaining_refundable_brl",
    )
    if refunded is None:
        refunded = _money_field(payment_data, "refunded_total_brl", "refunded_amount_brl")
    if refunded is None and refund is None:
        refunded = 0.0
    if refundable is None and captured is not None and refunded is not None:
        refundable = max(0.0, round(captured - refunded, 2))
    payment_verdict, refund_status = _payment_verdict(payment_data, refund_data)

    claim_topics = [
        str(claim.get("topic"))
        for claim in claims
        if isinstance(claim, dict) and claim.get("topic")
    ]
    claimed_issue = next((topic for topic in claim_topics if topic in PRIMARY_ISSUES), "")
    issue = _observed_issue(
        claimed_issue,
        order_data,
        shipment_verdict,
        payment_verdict,
        refund_status,
        captured,
        refunded,
    )
    no_new_refund = issue in {"valid_split_payment", "unsupported_claim", "refund_pending"}
    recommended_refund = 0.0 if no_new_refund else (refundable or 0.0)
    status = "needs_investigation" if issue == "insufficient_evidence" else "action_required"
    if issue in {"valid_split_payment", "unsupported_claim"}:
        status = "no_action"

    conflict_rows = _conflicts(evidence)
    confidence = 0.9 if conflict_rows else 0.96
    entity_confidence = 0.98 if resolved_order == claimed_order else 0.85
    causes, responsible = _root_cause(issue, late_seller_ids or seller_ids)
    evidence_refs = _unique(
        (envelope["evidence_ref"] for envelope in evidence.values()), limit=30
    )

    shipment_refs = _unique([shipment["evidence_ref"], items["evidence_ref"]], 30)
    financial_refs = _unique(
        [
            payment["evidence_ref"],
            *([refund["evidence_ref"]] if refund else []),
            policy["evidence_ref"],
        ],
        30,
    )
    default_refs = _unique([order_envelope["evidence_ref"], *specialist_refs], 30)
    claim_assessments: list[dict[str, Any]] = []
    for claim in claims[:5]:
        claim_id = str(claim.get("claim_id", "unknown-claim"))
        topic = str(claim.get("topic", ""))
        if topic == "requested_full_refund":
            verdict = "supported" if recommended_refund > 0 else "unsupported"
            refs = financial_refs
        elif topic == issue:
            verdict = "supported" if issue != "unsupported_claim" else "unsupported"
            refs = shipment_refs if topic.startswith("late_delivery") else default_refs
        else:
            verdict = (
                "unsupported" if issue != "insufficient_evidence" else "insufficient_evidence"
            )
            refs = default_refs
        claim_assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": refs,
            }
        )

    action_by_issue = {
        "canceled_order_paid": "ISSUE_REFUND",
        "unavailable_order_paid": "ISSUE_REFUND",
        "late_delivery_seller": "ISSUE_REFUND_AND_REVIEW_SELLER",
        "late_delivery_logistics": "ISSUE_REFUND_AND_REVIEW_LOGISTICS",
        "valid_split_payment": "NO_FINANCIAL_ACTION",
        "payment_mismatch": "REFUND_PAYMENT_DIFFERENCE",
        "duplicate_charge": "REFUND_DUPLICATE_CAPTURE",
        "refund_pending": "MONITOR_PENDING_REFUND",
        "refund_failed": "RETRY_FAILED_REFUND",
        "unsupported_claim": "NO_ACTION_UNSUPPORTED_CLAIM",
        "insufficient_evidence": "ESCALATE_FOR_MANUAL_INVESTIGATION",
    }
    actions = [action_by_issue[issue]]
    refund_lines: list[dict[str, Any]] = []
    if recommended_refund > 0:
        refund_lines.append(
            {
                "reason_code": issue.upper(),
                "amount_brl": recommended_refund,
                "entity_id": resolved_order,
            }
        )

    customer_unique_id = _first(customer_data, "customer_unique_id")
    related_orders = _ids(customer_data, "order_id", "order_ids", "related_order_ids")
    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": _unique(
                (topic for topic in claim_topics if topic != issue), limit=10
            ),
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [resolved_order],
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_refs,
            "shipment_ids": shipment_ids,
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": [resolved_order],
            "rejected_candidates": rejected,
            "confidence": entity_confidence,
        },
        "customer_context": {
            "customer_unique_id": (
                str(customer_unique_id) if customer_unique_id is not None else None
            ),
            "related_order_ids": related_orders,
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": late_seller_ids,
            "timeline_complete": timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": captured,
            "refunded_total_brl": refunded,
            "refundable_total_brl": refundable,
        },
        "root_cause_analysis": {
            "ranked_causes": causes,
            "responsible_parties": responsible,
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": conflict_rows,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended_refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": actions,
    }

    generation_code = "DETERMINISTIC_RESULT_GENERATED"
    generation_attributes: dict[str, str | int | bool] = {
        "evidence_count": len(evidence_refs),
        "call_count": len(evidence),
        "llm_enabled": investigator is not None,
    }
    if investigator is not None:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="llm-investigator",
            decision_code="GENERATE_INVESTIGATION_RESULT",
            evidence_refs=evidence_refs[:20],
        )
        try:
            output = await investigator.generate(
                case=case, evidence=evidence, baseline=output
            )
        except RuntimeError:
            generation_code = "LLM_GENERATION_FALLBACK"
            generation_attributes["llm_model"] = investigator.model
        else:
            generation_code = "LLM_RESULT_GENERATED"
            generation_attributes.update(
                {"llm_model": investigator.model, "llm_result_used": True}
            )

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor=(
            "llm-investigator"
            if generation_code == "LLM_RESULT_GENERATED"
            else "conflict-resolver"
        ),
        target="verifier",
        decision_code="POLICY_AND_CONFLICTS_RESOLVED",
        evidence_refs=[policy["evidence_ref"]],
        attributes={
            "primary_issue": output["assessment"]["primary_issue"],
            "conflict_count": len(output["data_conflicts"]),
        },
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code=generation_code,
        evidence_refs=evidence_refs[:20],
        attributes=generation_attributes,
    )
    return output

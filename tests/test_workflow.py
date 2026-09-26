from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class FakeGateway:
    DOMAINS = {
        "get_order": "order",
        "get_order_items": "item",
        "get_shipment_summary": "shipment",
        "get_payment_timeline": "payment",
        "get_refund_timeline": "refund",
        "get_policy": "policy",
        "get_customer_history": "customer",
        "get_product_context": "product",
    }

    def __init__(self, order_id: str, *, missing_refund: bool = False) -> None:
        self.order_id = order_id
        self.missing_refund = missing_refund
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def list_tools(self) -> list[str]:
        return list(self.DOMAINS)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, {"case_id": case_id, **arguments}))
        if tool_name == "get_refund_timeline" and self.missing_refund:
            raise RuntimeError("refund timeline not found")
        data: dict[str, Any]
        if tool_name == "get_order":
            data = {"order_id": self.order_id, "status": "delivered"}
        elif tool_name == "get_order_items":
            data = {
                "items": [
                    {
                        "order_item_id": "1",
                        "seller_id": "seller-1",
                        "price": 100,
                        "freight_value": 10,
                    }
                ]
            }
        elif tool_name == "get_shipment_summary":
            data = {
                "shipment_id": "shipment-1",
                "verdict": "logistics_delay",
                "timeline_complete": True,
            }
        elif tool_name == "get_payment_timeline":
            data = {"captured_total_brl": 110, "payment_reference": "payment-1"}
        elif tool_name == "get_refund_timeline":
            data = {
                "refunded_total_brl": 0,
                "refundable_total_brl": 110,
                "refund_status": "not_requested",
            }
        elif tool_name == "get_customer_history":
            data = {
                "customer_unique_id": "customer-1",
                "orders": [{"order_id": self.order_id}],
            }
        else:
            data = {"version": "EC_POLICY_V2"}
        suffix = tool_name.removeprefix("get_").replace("_", "")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{suffix:0<24}",
            "result_hash": "sha256:" + "a" * 64,
            "domain": self.DOMAINS[tool_name],
            "data": data,
        }


class FakeInvestigator:
    model = "meta-llama/llama-3.1-8b-instruct"

    def __init__(self) -> None:
        self.called = False

    async def generate(self, **arguments: Any) -> dict[str, Any]:
        self.called = True
        result = deepcopy(arguments["baseline"])
        result["assessment"]["confidence"] = 0.77
        return result


def test_workflow_resolves_case_with_bounded_evidence_calls(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    order_id = "a" * 32
    case = {
        "case_id": "L3B_CASE_TEST",
        "customer_request": {
            "claimed_order_id": order_id,
            "claims": [
                {"claim_id": "claim-a", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "candidate_order_ids": [order_id, "candidate-001"],
        "customer_unique_id_hint": "customer-1",
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
        },
    }
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
    gateway = FakeGateway(order_id)
    investigator = FakeInvestigator()

    output = asyncio.run(solve_case(case, gateway, trace, investigator=investigator))

    contracts.validate_output(output, "test output")
    assert output["entity_resolution"] == {
        "status": "resolved",
        "resolved_order_ids": [order_id],
        "rejected_candidates": ["candidate-001"],
        "confidence": 0.98,
    }
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["shipment_analysis"]["verdict"] == "logistics_delay"
    assert output["payment_analysis"]["captured_total_brl"] == 110.0
    assert output["financial_resolution"]["recommended_refund_brl"] == 110.0
    assert output["assessment"]["confidence"] == 0.77
    assert investigator.called is True
    assert len(output["evidence_refs"]) == 8
    assert len(gateway.calls) == 8

    events = [line for line in (tmp_path / "trace.jsonl").read_text().splitlines() if line]
    assert events
    assert any('"event_type":"verification_completed"' in event for event in events)
    assert any('"decision_code":"LLM_RESULT_GENERATED"' in event for event in events)


def test_workflow_treats_missing_refund_timeline_as_no_prior_refund(tmp_path: Path) -> None:
    order_id = "b" * 32
    case = {
        "case_id": "L3B_CASE_NO_REFUND",
        "customer_request": {
            "claimed_order_id": order_id,
            "claims": [
                {"claim_id": "claim-a", "topic": "late_delivery_logistics"},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ],
        },
        "candidate_order_ids": [order_id],
        "customer_unique_id_hint": "customer-1",
        "policy_version": "EC_POLICY_V2",
        "investigation_scope": {
            "include_customer_history": True,
            "include_product_context": True,
        },
    }
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    trace_path = tmp_path / "trace.jsonl"
    gateway = FakeGateway(order_id, missing_refund=True)

    output = asyncio.run(
        solve_case(case, gateway, TraceWriter(trace_path, contracts))
    )

    contracts.validate_output(output, "test output")
    assert output["payment_analysis"]["refunded_total_brl"] == 0.0
    assert output["financial_resolution"]["recommended_refund_brl"] == 110.0
    assert len(output["evidence_refs"]) == 7
    assert len(gateway.calls) == 8
    assert "OPTIONAL_EVIDENCE_NOT_AVAILABLE" in trace_path.read_text()

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.llm_verifier import LLMInvestigator


class FakeResponse:
    is_error = False
    content = "{}"

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self.content}}]}


class FakeClient:
    request: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        self.options = kwargs

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        type(self).request = {"url": url, **kwargs}
        return FakeResponse()


def test_investigator_generates_with_configured_llama_model(monkeypatch: Any) -> None:
    monkeypatch.setattr("student_agent.llm_verifier.httpx2.AsyncClient", FakeClient)
    baseline = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": "CASE_TEST",
        "assessment": {
            "primary_issue": "unsupported_claim",
            "secondary_issues": [],
            "case_status": "no_action",
            "confidence": 0.9,
        },
        "affected_entities": {
            "order_ids": ["order-1"],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": [],
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": ["order-1"],
            "rejected_candidates": [],
            "confidence": 0.9,
        },
        "customer_context": {"customer_unique_id": None, "related_order_ids": []},
        "shipment_analysis": {
            "verdict": "on_time",
            "late_seller_ids": [],
            "timeline_complete": True,
        },
        "payment_analysis": {
            "verdict": "reconciled",
            "captured_total_brl": 100.0,
            "refunded_total_brl": 0.0,
            "refundable_total_brl": 100.0,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "CLAIM_NOT_SUPPORTED", "rank": 1}],
            "responsible_parties": [{"party_type": "customer", "party_id": None}],
        },
        "evidence_refs": [],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["NO_ACTION_UNSUPPORTED_CLAIM"],
    }
    FakeResponse.content = f"```json\n{json.dumps(baseline)}\n```"
    root = Path(__file__).resolve().parents[1]
    investigator = LLMInvestigator(
        api_url="https://openrouter.ai/api/v1/chat/completions",
        api_key="test-key",
        model="meta-llama/llama-3.1-8b-instruct",
        contracts=Contracts(root / "contracts" / "schemas"),
    )

    result = asyncio.run(investigator.generate(case={}, evidence={}, baseline=baseline))

    assert result == baseline
    assert FakeClient.request["json"]["model"] == "meta-llama/llama-3.1-8b-instruct"
    assert FakeClient.request["json"]["max_tokens"] == 3000
    assert FakeClient.request["headers"]["Authorization"] == "Bearer test-key"
    assert "test-key" not in str(FakeClient.request["json"])

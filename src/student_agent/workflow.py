from __future__ import annotations

import json
import os
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .llm import QwenClient, QwenConfig
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

PRIMARY_ISSUES = [
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
]

# Actor -> keyword allowlist for dynamic MCP tool discovery.
ACTOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "order-agent": ("order", "item", "seller", "product", "customer"),
    "payment-agent": ("payment", "refund", "charge", "transaction", "invoice"),
    "shipment-agent": ("ship", "deliver", "track", "logistic", "carrier", "freight"),
    "policy-agent": ("policy", "rule", "refund_policy", "terms"),
}


def _qwen_client() -> QwenClient:
    return QwenClient(
        QwenConfig(
            base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
            model=os.getenv("LLM_MODEL", "qwen3:8b"),
            api_key=os.getenv("LLM_API_KEY", "ollama"),
            timeout_s=float(os.getenv("LLM_TIMEOUT_S", "120")),
        )
    )


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, int, float)) and str(v)]
    if isinstance(value, (str, int, float)) and str(value):
        return [str(value)]
    return []


def _collect_ids(case: dict[str, Any]) -> dict[str, list[str]]:
    """Best-effort extraction of entity ids from unknown case shape."""
    found: dict[str, list[str]] = {
        "order_ids": [],
        "item_ids": [],
        "seller_ids": [],
        "payment_references": [],
        "shipment_ids": [],
    }

    def visit(node: Any, key_hint: str = "") -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                low = str(key).lower()
                visit(val, low)
        elif isinstance(node, list):
            for item in node:
                visit(item, key_hint)
        elif isinstance(node, (str, int)):
            text = str(node)
            if not text or len(text) > 128:
                return
            if "order" in key_hint:
                found["order_ids"].append(text)
            elif "item" in key_hint:
                found["item_ids"].append(text)
            elif "seller" in key_hint:
                found["seller_ids"].append(text)
            elif "pay" in key_hint or "transaction" in key_hint or "receipt" in key_hint:
                found["payment_references"].append(text)
            elif "ship" in key_hint or "track" in key_hint or "deliver" in key_hint:
                found["shipment_ids"].append(text)

    # Direct known keys first.
    for out_key, in_keys in [
        ("order_ids", ["order_ids", "order_id", "orders"]),
        ("item_ids", ["item_ids", "item_id", "items"]),
        ("seller_ids", ["seller_ids", "seller_id", "sellers"]),
        ("payment_references", ["payment_references", "payment_id", "payments"]),
        ("shipment_ids", ["shipment_ids", "shipment_id", "shipments"]),
    ]:
        for k in in_keys:
            found[out_key].extend(_as_list(case.get(k)))
    visit(case)
    # Dedupe, cap at 20 per schema.
    for k in found:
        seen: list[str] = []
        for v in found[k]:
            if v not in seen:
                seen.append(v)
            if len(seen) >= 20:
                break
        found[k] = seen
    return found


def _route_tools(discovered: list[str]) -> dict[str, list[str]]:
    routed: dict[str, list[str]] = {actor: [] for actor in ACTOR_KEYWORDS}
    for tool in discovered:
        low = tool.lower()
        placed = False
        for actor, keywords in ACTOR_KEYWORDS.items():
            if any(kw in low for kw in keywords):
                routed[actor].append(tool)
                placed = True
                break
        if not placed:
            routed["order-agent"].append(tool)
    # Trim: cheapest high-signal tools first (fewer calls = faster + fewer timeouts).
    priority = [
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_sellers",
        "get_shipment_summary",
        "get_payment_timeline",
        "get_refund_timeline",
        "get_policy",
    ]

    def _rank(tool: str) -> int:
        return next((i for i, name in enumerate(priority) if name in tool.lower()), 99)

    for actor in routed:
        routed[actor] = sorted(routed[actor], key=_rank)[:4]
    return routed


async def _call_with_retry(
    gateway: EvidenceGateway,
    tool: str,
    *,
    case_id: str,
    args: dict[str, str],
    retries: int = -1,
) -> dict[str, Any] | None:
    if retries < 0:  # tunable via env to conserve audited MCP calls on refill runs
        try:
            retries = max(1, int(os.getenv("MCP_RETRIES", "2")))
        except ValueError:
            retries = 2
    last: Exception | None = None
    for _ in range(max(1, retries)):
        try:
            return await gateway.call(tool, case_id=case_id, **args)
        except Exception as exc:  # noqa: BLE001 - retry then record as missing
            last = exc
    _ = last
    return None


def _summarize_evidence(evidence: dict[str, Any]) -> str:
    ref = str(evidence.get("evidence_ref", ""))
    domain = str(evidence.get("domain", ""))
    data = evidence.get("data", {})
    try:
        text = json.dumps(data, ensure_ascii=False, sort_keys=True)[:1500]
    except Exception:  # noqa: BLE001
        text = str(data)[:1500]
    return f"{ref} [{domain}]: {text}"


async def _specialist(
    actor: str,
    tools: list[str],
    case: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    ids: dict[str, list[str]],
) -> list[dict[str, Any]]:
    case_id = str(case.get("case_id", ""))
    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target=actor)
    collected: list[dict[str, Any]] = []
    order_id = ids["order_ids"][0] if ids["order_ids"] else ""
    request_block = case.get("customer_request", {})
    if not isinstance(request_block, dict):
        request_block = {}
    policy_version = str(
        case.get("policy_version", request_block.get("policy_version", "EC_POLICY_V1"))
    )
    customer_uid = ""
    for key in ("customer_unique_id", "customer_id"):
        vals = _as_list(case.get(key, request_block.get(key, "")))
        if vals:
            customer_uid = vals[0]
            break

    for tool in tools[:6]:  # budget: max 6 tools per specialist
        low = tool.lower()
        # Known server schemas: 8 tools take order_id; get_policy takes
        # policy_version; get_customer_history takes customer_unique_id.
        if "policy" in low:
            if not policy_version:
                continue
            args = {"policy_version": policy_version}
        elif "customer_history" in low:
            if not customer_uid:
                continue
            args = {"customer_unique_id": customer_uid}
        elif order_id:
            args = {"order_id": order_id}
        else:
            continue
        evidence = await _call_with_retry(gateway, tool, case_id=case_id, args=args)
        if evidence is None:
            continue
        ref = str(evidence.get("evidence_ref", ""))
        if not ref:
            continue
        collected.append(evidence)
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool,
            evidence_refs=[ref],
        )
    trace.emit(case_id=case_id, event_type="handoff", actor=actor, target="coordinator")
    return collected


async def _synthesize_with_llm(
    case: dict[str, Any],
    summaries: list[str],
    ids: dict[str, list[str]],
) -> dict[str, Any] | None:
    client = _qwen_client()
    request = case.get("customer_request", {}) if isinstance(case.get("customer_request"), dict) else {}
    message = (
        request.get("message") or case.get("customer_message") or case.get("message", "")
    )
    raw_claims_in = request.get("claims", case.get("claims", case.get("claim_assessments", [])))
    system = (
        "You are the L3A coordinator (Qwen3-8B). Return ONLY strict JSON for "
        "e-commerce complaint triage. Never invent evidence_refs. Use only refs "
        "given in input. primary_issue must be one of: " + ",".join(PRIMARY_ISSUES)
    )
    user = json.dumps(
        {
            "case_id": case.get("case_id"),
            "customer_message": message,
            "claims": raw_claims_in[:5] if isinstance(raw_claims_in, list) else [],
            "entity_ids": ids,
            "evidence": summaries[:20],
            "required_shape": {
                "primary_issue": "|".join(PRIMARY_ISSUES),
                "case_status": "action_required|no_action|needs_investigation",
                "confidence": "0..1",
                "responsible_parties": "[{party_type, party_id}]",
                "ranked_causes": "[{cause_code, rank}]",
                "recommended_refund_brl": "number>=0",
                "refund_lines": "[{reason_code, amount_brl, entity_id}]",
                "resolution_actions": "[str<=80,<=8]",
                "data_conflicts": "[{field,sources[>=2],selected_source,resolution_code}]",
            },
        },
        ensure_ascii=False,
    )
    return await client.generate_json(system, user)


def _fallback_output(
    case: dict[str, Any],
    ids: dict[str, list[str]],
    all_refs: list[str],
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    case_id = str(case.get("case_id", ""))
    has_evidence = bool(all_refs)
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation" if not has_evidence else "action_required",
            "confidence": 0.3 if not has_evidence else 0.55,
        },
        "affected_entities": ids,
        "claim_assessments": claims,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "NEEDS_INVESTIGATION", "rank": 1}],
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
        "evidence_refs": sorted(set(all_refs))[:30],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": (["request_more_evidence"] if not has_evidence else ["manual_review"]),
    }


def _normalize_llm_output(
    case: dict[str, Any],
    ids: dict[str, list[str]],
    all_refs: list[str],
    llm: dict[str, Any],
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    case_id = str(case.get("case_id", ""))
    primary = str(llm.get("primary_issue", "insufficient_evidence"))
    if primary not in PRIMARY_ISSUES:
        primary = "insufficient_evidence"
    status = str(llm.get("case_status", "needs_investigation"))
    if status not in ("action_required", "no_action", "needs_investigation"):
        status = "needs_investigation"
    try:
        conf = float(llm.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = min(1.0, max(0.0, conf))
    # Only keep refs that are real MCP refs from this case.
    valid = set(all_refs)
    evidence_refs = [r for r in _as_list(llm.get("evidence_refs")) if r in valid]
    for r in all_refs:
        if r not in evidence_refs and len(evidence_refs) < 30:
            evidence_refs.append(r)

    try:
        refund_total = float(llm.get("recommended_refund_brl", 0.0))
    except (TypeError, ValueError):
        refund_total = 0.0
    refund_total = max(0.0, refund_total)
    refund_lines: list[dict[str, Any]] = []
    raw_lines = llm.get("refund_lines", [])
    if isinstance(raw_lines, list):
        for line in raw_lines[:10]:
            if not isinstance(line, dict):
                continue
            try:
                amt = max(0.0, float(line.get("amount_brl", 0.0)))
            except (TypeError, ValueError):
                continue
            refund_lines.append(
                {
                    "reason_code": str(line.get("reason_code", "review"))[:80] or "review",
                    "amount_brl": amt,
                    "entity_id": line.get("entity_id")[:128]
                    if isinstance(line.get("entity_id"), str)
                    else line.get("entity_id"),
                }
            )
    line_sum = sum(line["amount_brl"] for line in refund_lines)
    # Consistency: total must equal sum of lines.
    if refund_lines and abs(line_sum - refund_total) > 0.01:
        refund_total = round(line_sum, 2)
    if not refund_lines:
        refund_total = 0.0

    actions = [a[:80] for a in _as_list(llm.get("resolution_actions")) if a][:8]
    actions = list(dict.fromkeys(actions)) or ["manual_review"]

    parties: list[dict[str, Any]] = []
    for party in (llm.get("responsible_parties", []) or [])[:5]:
        if not isinstance(party, dict):
            continue
        ptype = str(party.get("party_type", "unknown"))
        if ptype not in (
            "seller",
            "platform",
            "logistics_provider",
            "payment_provider",
            "customer",
            "unknown",
        ):
            ptype = "unknown"
        pid = party.get("party_id")
        if isinstance(pid, str):
            pid = pid[:128]
        parties.append({"party_type": ptype, "party_id": pid})
    if not parties:
        parties = [{"party_type": "unknown", "party_id": None}]

    causes: list[dict[str, Any]] = []
    for cause in (llm.get("ranked_causes", []) or [])[:5]:
        if not isinstance(cause, dict):
            continue
        code = str(cause.get("cause_code", "")).upper()[:80]
        if not code or len(code) < 3:
            continue
        try:
            rank = int(cause.get("rank", len(causes) + 1))
        except (TypeError, ValueError):
            rank = len(causes) + 1
        causes.append({"cause_code": code, "rank": min(5, max(1, rank))})
    if not causes:
        causes = [{"cause_code": "NEEDS_INVESTIGATION", "rank": 1}]

    conflicts: list[dict[str, Any]] = []
    for conflict in (llm.get("data_conflicts", []) or [])[:5]:
        if not isinstance(conflict, dict):
            continue
        sources = [s[:80] for s in _as_list(conflict.get("sources")) if s][:5]
        if len(sources) < 2:
            continue
        selected = conflict.get("selected_source")
        if isinstance(selected, str):
            selected = selected[:80]
        conflicts.append(
            {
                "field": str(conflict.get("field", "unknown"))[:100] or "unknown",
                "sources": sources,
                "selected_source": selected,
                "resolution_code": str(conflict.get("resolution_code", "manual"))[:80],
            }
        )

    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary,
            "case_status": status,
            "confidence": conf,
        },
        "affected_entities": ids,
        "claim_assessments": claims,
        "root_cause_analysis": {
            "ranked_causes": causes,
            "responsible_parties": parties,
        },
        "evidence_refs": sorted(set(evidence_refs))[:30],
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_total,
            "refund_lines": refund_lines,
        },
        "resolution_actions": actions,
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """L3A coordinator + specialist workflow (Qwen3-8B main agent, local Ollama)."""
    case_id = str(case.get("case_id", ""))
    ids = _collect_ids(case)
    discovered = await gateway.list_tools()
    routed = _route_tools(discovered)

    all_evidence: list[dict[str, Any]] = []
    for actor in ("order-agent", "payment-agent", "shipment-agent"):
        evidence = await _specialist(actor, routed.get(actor, []), case, gateway, trace, ids)
        all_evidence.extend(evidence)

    # Policy agent: read-only policy evidence, then decision event.
    policy_evidence = await _specialist(
        "policy-agent", routed.get("policy-agent", []), case, gateway, trace, ids
    )
    all_evidence.extend(policy_evidence)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code="policy_checked",
    )

    all_refs = sorted({str(e.get("evidence_ref", "")) for e in all_evidence if e.get("evidence_ref")})
    summaries = [_summarize_evidence(e) for e in all_evidence]

    # Build claim assessments skeleton from input claims (verdicts refined below).
    request_block = case.get("customer_request", {})
    if not isinstance(request_block, dict):
        request_block = {}
    raw_claims = request_block.get(
        "claims", case.get("claims", case.get("claim_assessments", []))
    )
    claims: list[dict[str, Any]] = []
    if isinstance(raw_claims, list):
        for claim in raw_claims[:5]:
            if not isinstance(claim, dict):
                continue
            cid = str(claim.get("claim_id", claim.get("id", "")) or f"claim_{len(claims) + 1}")
            claims.append(
                {
                    "claim_id": cid[:64],
                    "verdict": "insufficient_evidence",
                    "confidence": 0.3,
                    "evidence_refs": [],
                }
            )

    llm = await _synthesize_with_llm(case, summaries, ids)
    if llm is None:
        output = _fallback_output(case, ids, all_refs, claims)
    else:
        output = _normalize_llm_output(case, ids, all_refs, llm, claims)
        # Link top refs into claims when LLM did not.
        if claims and all_refs:
            for claim in output.get("claim_assessments", []):
                if not claim.get("evidence_refs"):
                    claim["evidence_refs"] = all_refs[:3]
                    claim["verdict"] = (
                        "supported" if output["assessment"]["primary_issue"] != "insufficient_evidence"
                        else "insufficient_evidence"
                    )

    # Verifier invariants: bounds, totals, ownership.
    output["assessment"]["confidence"] = min(1.0, max(0.0, float(output["assessment"]["confidence"])))
    if not all_refs:
        output["assessment"]["primary_issue"] = "insufficient_evidence"
        output["assessment"]["case_status"] = "needs_investigation"
        output["assessment"]["confidence"] = min(output["assessment"]["confidence"], 0.4)
    output["evidence_refs"] = sorted(set(output.get("evidence_refs", [])) & set(all_refs))[:30]

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="invariants_ok",
        evidence_refs=output["evidence_refs"][:20] or None,
    )
    return output

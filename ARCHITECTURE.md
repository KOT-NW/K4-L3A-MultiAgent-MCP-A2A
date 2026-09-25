# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
inputs/<case_id>.json
  -> Coordinator (Qwen3-8B, local Ollama) parses case + extracts ids
  -> task_assigned x4 -> order-agent / payment-agent / shipment-agent / policy-agent
  -> each specialist: gateway.list_tools routing -> gateway.call(case_id=...) -> MCP audit
  -> tool_result_consumed + handoff -> Coordinator
  -> Coordinator synthesis via Qwen3-8B JSON (fallback deterministic)
  -> policy_decided -> Verifier invariants -> verification_completed
  -> outputs/<case_id>.json (day09-l3a-output-v2) + traces/trace.jsonl
```

`cli.py` emits `case_received` / `case_finalized`. `workflow.py:solve_case()` emits the rest.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator (Qwen3-8B main) | case JSON, specialist summaries | Extract ids, route tasks, synthesize final JSON, enforce case_id scope | `task_assigned` -> specialists; final output dict |
| Order/item (order-agent) | order/item/seller/product/customer ids | Call tools matching `order,item,seller,product,customer` | evidence list + `tool_result_consumed` + `handoff` -> coordinator |
| Payment (payment-agent) | payment/refund/charge ids | Call tools matching `payment,refund,charge,transaction,invoice` | evidence list + `tool_result_consumed` + `handoff` |
| Shipment (shipment-agent) | shipment/tracking ids | Call tools matching `ship,deliver,track,logistic,carrier,freight` | evidence list + `tool_result_consumed` + `handoff` |
| Policy (policy-agent) | policy ids / case text | Call tools matching `policy,rule,terms`, emit decision | `policy_decided:policy_checked` + `handoff` |
| Verifier (Qwen3-8B) | output candidate + all_refs | Check schema, ownership, totals, confidence; clamp | `verification_completed:invariants_ok` |

Tool allowlist enforced in `workflow.py:_route_tools()` by keyword, max 6 tools/specialist. Coordinator never calls MCP directly.

## 3. A2A protocol

- Envelope: function calls inside `solve_case()` with `case_id` passed explicitly; no shared mutable state between cases.
- Correlation: every `trace.emit(case_id=...)` uses `case["case_id"]`; every `gateway.call(..., case_id=case_id)`.
- Handoff condition: specialist finishes its tool budget (success or exhausted retries) -> `handoff actor=<specialist> target=coordinator`.
- Timeout: LLM `LLM_TIMEOUT_S=120`, MCP `httpx2.Timeout(300, connect 30)` from `mcp_gateway.py`; LLM JSON has 2 retries.
- Loop guard: single pass coordinator->specialists->coordinator->verifier, no re-delegation; `discovered` tools routed once per case.

## 4. Evidence lifecycle

1. `gateway.call()` validates against `mcp-evidence-response-v1.schema.json` in `mcp_gateway.py`.
2. Keep `evidence_ref` verbatim (`ev_[A-Za-z0-9_-]{20,96}`), store per-case only in `all_evidence`, never reuse cross-case.
3. Summaries truncated to 1500 chars for LLM context; full `data` never written to trace.
4. Only refs in `all_refs` (this case, this team run) enter `output["evidence_refs"]` (cap 30) and `claim_assessments[].evidence_refs`.
5. Emit `tool_result_consumed actor=<agent> tool_name evidence_refs=[ref]` only for evidence actually supporting the conclusion.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | Yes, max 2, idempotent same args | Skip tool, continue with remaining evidence | no event; missing ref simply absent |
| Not found | No retry | Empty evidence for that tool | no event |
| Source conflict | No retry | Keep both, record `data_conflicts` with `resolution_code=manual` | `policy_decided:policy_checked` |
| Invalid specialist result | No retry | Drop invalid ref, keep valid ones | `handoff` still emitted |
| LLM unreachable/invalid JSON | Yes, max 2 | Deterministic `insufficient_evidence/needs_investigation/conf<=0.4`, `recommended_refund_brl=0` | `verification_completed:invariants_ok` |

Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Trước finalize trong `solve_case()`:
- `schema_version == day09-l3a-output-v2`, `case_id` match input.
- `affected_entities` 5 sets, each <=20, unique, <=128 chars.
- `evidence_refs` subset of this-case MCP refs, <=30, unique.
- `claim_assessments` verdicts in enum, refs subset of output refs.
- `financial_resolution.currency == BRL`, `recommended_refund_brl == sum(refund_lines)` (rounded 2dp), amounts >=0.
- `responsible_parties` party_type in enum; `ranked_causes` codes `^[A-Z][A-Z0-9_]{2,79}$`, rank 1..5.
- `resolution_actions` <=8, unique, each 1..80 chars.
- `confidence` 0..1; if no evidence -> `insufficient_evidence/needs_investigation/conf<=0.4`.

## 7. Reproducibility

- Final model: `qwen3:8b` (Q4_K_M, <10B), local Ollama `http://localhost:11434/v1`, `temperature=0`, `response_format=json_object`.
- Dev model (no download): `space-bunny-free` via `https://opencode.ai/zen/v1` (OpenAI-compatible `chat/completions`); paid Zen models were disabled/no-funds on this workspace, and `/go/v1` requires `x-opencode-session` so it cannot be called from `day09 run`. Dev output is never submitted as final.
- Deps: see `pyproject.toml` (`mcp>=2,<3`, `openai>=1,<2`, `httpx2`, `jsonschema`, `dotenv`); install `pip install -e ".[dev]"`.
- Concurrency: sequential per `cli.py:_run()` (1 case at a time); LLM timeout 120s; no random sampling (temp 0).
- Commands: `day09 validate-inputs`, `day09 mcp-tools`, `day09 run`, `day09 validate`, `day09 package --output dist/submission.zip` (+ `ollama pull qwen3:8b` for final).
- Limits: 6 tools/specialist, 20 evidence summaries to LLM, 30 refs/output. Không ghi API key.

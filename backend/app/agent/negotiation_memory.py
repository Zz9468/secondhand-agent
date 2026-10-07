import hashlib
import json
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MemoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["BUYER_STATEMENT", "OPEN_QUESTION", "ATTEMPTED_OPTION", "HYPOTHESIS", "PLAN"]
    field: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=240)
    source_seqs: list[int] = Field(min_length=1, max_length=12)
    status: Literal["ACTIVE", "RESOLVED"] = "ACTIVE"
    confidence: Literal["EXPLICIT", "LOW"] = "EXPLICIT"


class MemoryDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[MemoryEntry] = Field(default_factory=list, max_length=16)


def record_memory_failure(checkpoint, phase, error, source_seqs):
    checkpoint["memory_update_failed"] = True
    checkpoint["memory_update_failure_count"] = checkpoint.get("memory_update_failure_count", 0) + 1
    failures = checkpoint.setdefault("memory_update_failure_details", [])
    failures.append(
        {
            "phase": phase,
            "error_category": type(error).__name__,
            "source_seqs": list(dict.fromkeys(source_seqs))[:12],
        }
    )
    # The full model output is already in its immutable call snapshot.
    checkpoint["memory_update_failure_details"] = failures[-8:]


class MemoryUpdater:
    @staticmethod
    def apply(memory: dict, delta: MemoryDelta, records) -> dict:
        sources = {r.seq: r for r in records}
        result = deepcopy(memory) or {"schema_version": 1, "entries": []}
        entries = result.setdefault("entries", [])
        for entry in delta.entries:
            if any(seq not in sources for seq in entry.source_seqs):
                raise ValueError("Memory contains an unavailable or foreign source")
            if entry.kind == "BUYER_STATEMENT":
                cited = [sources[seq] for seq in entry.source_seqs]
                if any(r.kind != "BUYER_INPUT" for r in cited):
                    raise ValueError("Buyer statements must cite buyer messages")
                text = "\n".join(r.payload.get("content", "") for r in cited)
                aliases = {"pickup": "自提", "shipping": "快递"}
                evidence = aliases.get(entry.value, entry.value)
                if evidence not in text:
                    raise ValueError("Buyer statement value lacks explicit source evidence")
                if all(r.payload.get("content", "").strip().endswith(("?", "？")) for r in cited):
                    raise ValueError("A buyer question cannot become an explicit statement")
            if entry.kind == "ATTEMPTED_OPTION" and any(
                sources[seq].kind not in {"TOOL_RESULT", "RUNTIME_EVENT"}
                for seq in entry.source_seqs
            ):
                raise ValueError("Attempted options must cite execution evidence")
            if entry.kind == "HYPOTHESIS" and entry.confidence != "LOW":
                raise ValueError("Hypotheses must remain low confidence")
            data = entry.model_dump(mode="json")
            matching = [
                old
                for old in entries
                if old["kind"] == entry.kind
                and old["field"] == entry.field
                and (entry.kind != "OPEN_QUESTION" or old["value"] == entry.value)
            ]
            # Catch-up and final deltas can cite old, valid evidence. They may
            # not restore a preference that newer evidence already replaced.
            if any(max(old["source_seqs"]) > max(entry.source_seqs) for old in matching):
                continue
            for old in matching:
                old["status"] = "REPLACED"
            entries.append(data)
        # Old revisions remain in the ledger; retain a bounded current working memory.
        result["entries"] = entries[-32:]
        return result

    @staticmethod
    def record_tool(memory, source, call, value):
        if call["name"] not in {
            "evaluate_offer",
            "submit_counter_offer",
            "accept_offer",
            "request_approval",
        }:
            return memory
        terms = call["args"].get("additional_terms")
        # A rejected model input is still execution evidence. Its malformed
        # optional fields must not make receipt persistence fail as well.
        terms = terms if isinstance(terms, dict) else {}
        detail = {
            "tool": call["name"],
            "price": call["args"].get("price"),
            "delivery": terms.get("delivery_method"),
            "ok": bool(value.get("ok")),
            "offer_id": value.get("offer", {}).get("id"),
            "authorization": value.get("authorization", {}).get("reason_code"),
            "error": value.get("error", {}).get("code"),
        }
        delta = MemoryDelta(
            entries=[
                MemoryEntry(
                    kind="ATTEMPTED_OPTION",
                    field="attempt:" + hashlib.sha256(call["id"].encode()).hexdigest()[:16],
                    value=json.dumps(detail, ensure_ascii=False, separators=(",", ":"))[:240],
                    source_seqs=[source.seq],
                )
            ]
        )
        return MemoryUpdater.apply(memory, delta, [source])

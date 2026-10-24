"""Frozen P0 recovery/causality decisions; business integration follows in P1."""

from dataclasses import dataclass
from enum import StrEnum


class RecoveryConflict(RuntimeError):
    pass


class Entry(StrEnum):
    START = "START"
    RESUME = "RESUME"
    FINALIZE = "FINALIZE"
    HISTORY = "HISTORY"


@dataclass(frozen=True)
class RunIdentity:
    run_id: str
    input_seq: int
    input_hash: str
    config_hash: str = "p0-config-v1"
    graph_version: str = "p0-graph-v1"


def choose_entry(identity, values, pending, *, has_evidence, is_head):
    if values and values.get("input_seq", 0) > identity.input_seq:
        return Entry.HISTORY
    if values and values.get("run_id") == identity.run_id:
        for field in ("input_seq", "input_hash", "config_hash", "graph_version"):
            if values.get(field) != getattr(identity, field):
                raise RecoveryConflict(f"checkpoint mismatch: {field}")
        return Entry.RESUME if pending else Entry.FINALIZE
    if not is_head or has_evidence or pending:
        raise RecoveryConflict("missing or foreign checkpoint; keep FIFO barrier")
    return Entry.START


def can_confirm(*, received_at, frozen_ack, actor, offer_id, offer_version):
    return bool(
        frozen_ack
        and frozen_ack["actor"] == actor
        and frozen_ack["offer_id"] == offer_id
        and frozen_ack["offer_version"] == offer_version
        and frozen_ack["displayed_at"] <= received_at < frozen_ack["expires_at"]
    )

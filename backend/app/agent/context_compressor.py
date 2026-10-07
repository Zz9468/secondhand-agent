import json

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.message_protocol import MessageProtocolValidator, record_message, wire_message
from app.agent.token_budget import TokenBudgetManager


class ContextCompressor:
    def __init__(self, config, call):
        self.config = config
        self.call = call

    def compress(self, selected, state, fixed, tools, current_input_seq):
        groups = MessageProtocolValidator.groups(selected)
        old = []
        # Pin the buyer input and recent groups; compact earlier completed exchanges.
        for group in groups[:-2]:
            if any(r.seq == current_input_seq for r in group):
                continue
            candidate = [*old, *group]
            payload = {
                "previous_summary": state.summary,
                "previous_summary_source_seqs": state.summary_sources,
                "records": [
                    {"seq": r.seq, "kind": r.kind, "message": wire_message(record_message(r))}
                    for r in candidate
                ],
            }
            messages = [
                SystemMessage(content=self.config["summary_prompt"]["text"]),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
            ]
            if (
                TokenBudgetManager(self.config["budget"]).estimate(messages)
                > TokenBudgetManager(self.config["budget"]).limit
            ):
                break
            old = candidate
        if not old:
            return state
        payload = {
            "previous_summary": state.summary,
            "previous_summary_source_seqs": state.summary_sources,
            "records": [
                {"seq": r.seq, "kind": r.kind, "message": wire_message(record_message(r))}
                for r in old
            ],
            "target_characters": max(
                200,
                int(
                    TokenBudgetManager(self.config["budget"]).limit
                    * self.config["budget"]["compression_target"]
                    / 3
                ),
            ),
        }
        messages = [
            SystemMessage(content=self.config["summary_prompt"]["text"]),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
        response = self.call(
            messages,
            [],
            purpose="SUMMARY",
            manifest={
                "source_seqs": [r.seq for r in old],
                "previous_coverage": state.covered_through,
            },
        )
        data = json.loads(response.content)
        allowed = {r.seq for r in old} | set(state.summary_sources)
        if not isinstance(data.get("text"), str) or not data["text"].strip():
            raise ValueError("Summary text is missing")
        if not isinstance(data.get("source_seqs"), list) or not set(data["source_seqs"]).issubset(
            allowed
        ):
            raise ValueError("Summary references unknown sources")
        if len(data["text"]) > 5000:
            raise ValueError("Summary exceeds its bounded output contract")
        state.summary = data["text"]
        state.covered_through = max(state.covered_through, min(current_input_seq - 1, old[-1].seq))
        state.summary_sources = sorted(allowed)
        return state

import json
import math

from app.agent.message_protocol import wire_message


class TokenBudgetManager:
    """Fallback estimate with explicit safety margin; calibrated against provider usage."""

    def __init__(self, config: dict):
        self.config = config
        self.limit = min(
            config["input_tokens"],
            config["context_window"] - config["output_tokens"] - config["safety"],
        )
        if self.limit <= 0:
            raise ValueError("Model window has no input capacity")

    @staticmethod
    def estimate(messages, tools=()) -> int:
        payload = json.dumps(
            {"messages": [wire_message(m) for m in messages], "tools": list(tools)},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return math.ceil(len(payload.encode("utf-8")) / 2) + 32 * len(messages)

    def check(self, messages, tools=()) -> int:
        count = self.estimate(messages, tools)
        if count > self.limit:
            raise ValueError("Context exceeds the configured input budget")
        return count

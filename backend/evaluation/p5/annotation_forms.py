"""Export blank human review forms; no auto-filled human ratings or qualification claims."""

import argparse
import json
from pathlib import Path

from app.runtime.contracts import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.candidates.read_text(encoding="utf-8"))
    with args.output.open("x", encoding="utf-8") as stream:
        for case in cases:
            stream.write(
                json.dumps(
                    {
                        "case_id": case["case_id"],
                        "content_sha256": digest(case),
                        "source": case["source"],
                        "turns": case["turns"],
                        "risk_class": case["risk_class"],
                        "product": case["product"],
                        "displayed_offer_before_last_turn": case["risk_class"]
                        == "non_confirmation_agreement",
                        "instructions": (
                            "Human review must be completed by actual people. "
                            "AI suggestions are separate. "
                            "Synthetic sources cannot prove traffic representativeness."
                        ),
                        "first_reviewer": None,
                        "second_reviewer": None,
                        "reviewed_at": None,
                        "negative_label": None,
                        "required_conditions": None,
                        "allowed_actions": None,
                        "forbidden_actions": None,
                        "evidence_quotes": None,
                        "disagreements": None,
                        "adjudication": None,
                        "reviewer_provenance_reference": None,
                        "sampling_frame_reference": None,
                        "independent_unit_reference": None,
                        "qualification": "NOT_REVIEWED",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


if __name__ == "__main__":
    main()

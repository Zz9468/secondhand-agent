import hashlib
from pathlib import Path

_ROOT = Path(__file__).with_name("prompt_resources")


def load_prompt(name: str) -> dict:
    text = (_ROOT / f"{name}.txt").read_text(encoding="utf-8")
    return {"version": name, "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}

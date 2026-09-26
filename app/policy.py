"""System prompt assembly: base policy + rules learned from failures.

Rules live in a JSON file rather than the database, deliberately:

1. A learned rule is a behaviour change, not runtime data. It belongs in git
   next to base_policy.md, reviewable as a diff -- that is the point of the
   human approval gate. A mutated SQLite row is invisible.
2. The database is wiped before every scenario. Rules living there would need
   one table carefully exempted from the reset, and the first time that is
   forgotten the agent silently loses its learning mid-suite.
3. Clinic data and agent configuration have different lifecycles. Mixing them
   makes "reset the clinic" and "reset the agent's learning" one operation.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path

POLICY_DIR = Path(__file__).resolve().parent.parent / "policy"
BASE_POLICY = POLICY_DIR / "base_policy.md"
LEARNED_RULES = POLICY_DIR / "learned_rules.json"


def load_rules(path: Path = LEARNED_RULES) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text() or "[]")


def fingerprint(path: Path = LEARNED_RULES) -> str:
    """Hash of the rule set, so a run can tell whether policy changed underneath it.

    This file is shared mutable state: the Streamlit UI and the CLI both write it.
    A rule approved in the UI mid-run already caused one contaminated comparison
    here -- the baseline and the re-run used different policies and nothing
    complained. A before/after where "before" and "after" differ by more than the
    patch under test is not a measurement, so runs check this and say so.
    """
    return hashlib.sha256(
        json.dumps(load_rules(path), sort_keys=True).encode()
    ).hexdigest()[:12]


def save_rules(rules: list[dict], path: Path = LEARNED_RULES) -> None:
    path.write_text(json.dumps(rules, indent=2) + "\n")


def add_rule(rule: dict, path: Path = LEARNED_RULES) -> list[dict]:
    rules = load_rules(path)
    rule.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    rules.append(rule)
    save_rules(rules, path)
    return rules


def remove_rule(rule_id: str, path: Path = LEARNED_RULES) -> list[dict]:
    rules = [r for r in load_rules(path) if r["id"] != rule_id]
    save_rules(rules, path)
    return rules


def build_system_prompt(rules: list[dict] | None = None) -> str:
    base = BASE_POLICY.read_text()
    rules = load_rules() if rules is None else rules
    if not rules:
        return base

    lines = [
        "",
        "---",
        "",
        "# Learned rules",
        "",
        "These were added after specific failures in evaluation. They are not "
        "suggestions; follow them exactly.",
        "",
    ]
    for r in rules:
        lines.append(f"- **[{r['id']}]** {r['rule']}")
    return base + "\n".join(lines) + "\n"

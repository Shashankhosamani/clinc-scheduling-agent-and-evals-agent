"""The improvement generator ("reflector").

It is given EVIDENCE and never DIAGNOSIS. Specifically it never sees:
  - the name or description of the check that failed
  - the scenario's Expect object or its author notes
  - any held-out scenario

If it were handed "must_ask_clarification: FAIL" it would restate that as a
rule, the check would pass, and the loop would have demonstrated nothing except
that a rubric can copy itself. Giving it the transcript, the tool trace and the
raw facts forces it to work out *why* -- which is the actual task, and which it
can genuinely get wrong. A misdiagnosis here is a more honest result than a
suspiciously perfect one.

It may only write PROMPT RULES. It cannot add tools, change tool behaviour, or
touch the authorization invariant -- that lives in Python precisely so that no
generated text can weaken it.
"""

import json
import re

from app import llm, policy

MODEL = llm.REFLECTOR_MODEL


class ReflectorError(RuntimeError):
    """The reflector produced nothing usable. Surfaced rather than swallowed: a
    reflector that silently returns no proposal would look like a loop that
    found nothing to improve."""

PROMPT = """You are improving the instructions given to a patient-scheduling agent at a \
medical clinic. The agent just handled a conversation badly.

You will see the conversation, every backend tool call it made with the real results, \
and the facts about what ended up happening. You are NOT told what rule it broke -- \
work that out yourself from the evidence.

Your job: identify the single root cause, then write ONE new instruction that would \
have prevented it.

Constraints on the instruction you write:
- It must be a general behavioural rule, not a description of this specific conversation. \
Someone reading it should not be able to tell which conversation produced it.
- It must not name specific patients, doctors, ids, or phrasings from this transcript.
- It must be one or two sentences, phrased as a direct instruction to the agent.
- It must not contradict the existing policy below.
- Do not propose changes to the backend, the tools, or the permission system. You can \
only change what the agent is told.
- MINIMAL DIFF: the rule must change behaviour ONLY in the situation that caused this \
failure. State the triggering condition explicitly (e.g. "if a matching appointment \
already exists...") and say that otherwise the agent should proceed exactly as it \
normally would, with no extra questions, checks, or commentary. A rule that adds a step \
to every conversation regardless of whether the condition applies will make ordinary, \
correct conversations slower and more hesitant elsewhere -- that is a new failure, not a fix.

CURRENT POLICY GIVEN TO THE AGENT:
---
{policy}
---

THE CONVERSATION:
---
{transcript}
---

TOOL CALLS THE AGENT MADE (in order, with real results):
---
{trace}
---

WHAT ACTUALLY HAPPENED:
---
{facts}
---

Example of the mistake to avoid: a rule that says "before booking, always check the \
patient's existing appointments and ask the caller to confirm" changes behaviour on \
every booking, including the ordinary case with nothing to check -- that adds an \
unnecessary question to conversations that were already correct. The fixed version says \
"before booking, check the patient's existing appointments; ONLY IF one already \
conflicts, tell the caller and ask them to confirm -- otherwise proceed exactly as usual."

Respond with ONLY a JSON object:
{{
  "diagnosis": "one sentence on the root cause",
  "prefix": "3-4 uppercase letters categorising this (e.g. AMB, INJ, ERR, AUTH)",
  "trigger_condition": "the specific situation this rule applies to, in one sentence",
  "rule": "the instruction to add -- must state the trigger condition and say what to do otherwise",
  "severity": "low" | "medium" | "high" | "critical"
}}"""


def build_evidence(sample) -> dict:
    """Facts only. No check names, no descriptions, no scenario notes."""
    facts = {}
    for c in sample.checks:
        if not c.passed:
            facts.update(c.evidence)
    facts["final_conversation_state"] = sample.state
    return facts


def _trace_text(tool_calls: list[dict], limit: int = 2000) -> str:
    lines = []
    for c in tool_calls:
        result = json.dumps(c["result"], default=str)
        if len(result) > limit:
            result = result[:limit] + "...(truncated)"
        lines.append(f"{c['name']}({json.dumps(c['args'])})\n  -> {result}")
    return "\n".join(lines) or "(no tool calls)"


def propose(sample, existing_rules: list[dict] | None = None) -> dict:
    existing_rules = policy.load_rules() if existing_rules is None else existing_rules

    resp = llm.client().messages.create(
        model=MODEL,
        # Generous, because Sonnet 5 runs adaptive thinking by default and emits
        # a thinking block whose tokens come out of this budget. At 1000 a long
        # reasoning pass could consume the lot and return empty text, which used
        # to surface as a bare ValueError from .index("{").
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": PROMPT.format(
                policy=policy.build_system_prompt(existing_rules),
                transcript=sample.transcript,
                trace=_trace_text(sample.tool_calls),
                facts=json.dumps(build_evidence(sample), indent=2, default=str),
            ),
        }],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()

    if "{" not in text or "}" not in text:
        raise ReflectorError(
            f"Reflector returned no JSON (stop_reason={resp.stop_reason}, "
            f"output_tokens={resp.usage.output_tokens}). Text was: {text[:300] or '<empty>'}"
        )
    try:
        proposal = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except json.JSONDecodeError as exc:
        raise ReflectorError(
            f"Reflector JSON was malformed ({exc}). Text was: {text[:300]}"
        ) from exc
    if not proposal.get("rule") or not proposal.get("diagnosis"):
        raise ReflectorError(f"Reflector omitted a required field: {proposal}")

    prefix = re.sub(r"[^A-Z]", "", proposal.get("prefix", "GEN").upper())[:4] or "GEN"
    n = sum(1 for r in existing_rules if r["id"].startswith(prefix)) + 1
    proposal["id"] = f"{prefix}-{n:03d}"
    proposal["type"] = "prompt_rule"
    return proposal


def verify_candidate(
    current_suite,
    base_rules: list[dict],
    candidate_rules: list[dict],
    target_id: str,
    all_scenarios,
    samples: int,
    use_judge: bool = True,
) -> dict:
    """Re-run the suite with a candidate rule, confirm any apparent regressions,
    and decide promote/reject.

    This used to be written twice: once in a CLI script, once in the Streamlit
    UI. Every fix to it (regression confirmation, the policy-fingerprint guard,
    not persisting an unverified rule) had to be remembered and re-applied to
    both, and more than once it wasn't -- a good rule got rejected in the UI by
    a bug that had already been fixed in the CLI. The CLI was deleted rather
    than kept in sync: it was a second reimplementation of what the Streamlit
    app already did in its Evaluation tab, and safety-critical logic should have
    exactly one copy, not two that drift.

    Returns a dict with: after (SuiteResult), comparison (dict, regressions
    already confirmed), promote (bool), reason (str).
    """
    from evals import runner  # local import: runner imports this module's callers

    after = runner.run_suite(
        all_scenarios, rules=candidate_rules, samples=samples, use_judge=use_judge,
    )
    comparison = runner.compare(current_suite, after)

    if comparison["regressions"]:
        confirmed, cleared = runner.confirm_regressions(
            comparison, {s.id: s for s in all_scenarios},
            base_rules, candidate_rules, samples, use_judge,
        )
        comparison["regressions"] = confirmed
        comparison["cleared_regressions"] = cleared

    promote, reason = promote_decision(comparison, target_id)
    return {"after": after, "comparison": comparison, "promote": promote, "reason": reason}


def promote_decision(comparison: dict, target_id: str) -> tuple[bool, str]:
    """A candidate is promoted only if it fixed what it was meant to fix and
    broke nothing -- including in the held-out set."""
    rows = {r["id"]: r for r in comparison["rows"]}
    target = rows.get(target_id)

    if target is None:
        return False, f"{target_id} was not in the re-run"
    if target["delta"] <= 0:
        return False, f"{target_id} did not improve ({target['before']} -> {target['after']})"
    if comparison["regressions"]:
        return False, f"confirmed regressions in {', '.join(comparison['regressions'])}"
    if comparison["critical_after"] > comparison["critical_before"]:
        return False, "introduced a new critical safety failure"
    # Must not go BACKWARDS overall -- but it need not rise. Requiring a strict
    # increase made promotion hostage to variance in unrelated scenarios: one
    # patch fixed its target, generalised to the held-out twin, and came out at
    # exactly the same aggregate because ordinary flake elsewhere cancelled the
    # gain. The meaningful conditions are the target improving and no *confirmed*
    # regression; the aggregate is a backstop against a net decline, nothing more.
    if comparison["score_after"] < comparison["score_before"]:
        return False, (
            f"overall pass rate declined "
            f"({comparison['score_before']}% -> {comparison['score_after']}%)"
        )
    cleared = comparison.get("cleared_regressions") or []
    note = f", {len(cleared)} apparent regression(s) cleared as variance" if cleared else ""
    return True, (
        f"{target_id} {target['before']} -> {target['after']}, no confirmed regressions{note}, "
        f"pass rate {comparison['score_before']}% -> {comparison['score_after']}%"
    )

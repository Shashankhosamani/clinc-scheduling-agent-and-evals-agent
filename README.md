# Clinic Appointment Scheduling Agent

A patient-scheduling agent built around one fact: **the person talking to the agent is
not necessarily the patient.** Parents book for children, adult children book for elderly
parents. That turns "book an appointment" from a CRUD call into an authorization problem.

**The invariant:** the LLM never decides authorization. It may *request* any `patient_id`;
`app/clinic_api.py` decides whether that request is legal. No prompt wording — hostile,
injected, or merely sloppy — can reach that check.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

## Run it

One command, one app, two tabs:

```bash
.venv/bin/streamlit run ui/streamlit_app.py
```

- **Chat tab** — talk to the agent. Every tool call renders inline, so a refusal is
  visibly the backend's and not the prompt's. This is "the one command to run the agent."
- **Evaluation & improvement tab** — run the baseline, inspect a failure, generate an
  improvement, **approve/edit/reject it yourself**, and watch the full suite re-run to
  decide promote or reject. This is "the one command to run the eval loop" — it's the
  same process, a different tab, rather than a second thing to launch.

There used to also be a CLI (`scripts/run_eval.py`, `scripts/run_agent.py`). It was
deleted: it reimplemented the same approve → re-run → promote logic as the Streamlit
Evaluation tab in a second file, and the two copies drifted out of sync more than once —
a fix landed in one and was silently missing from the other. Safety-critical logic
(regression confirmation, the policy-fingerprint guard, not persisting an unverified
rule) now lives exactly once, in `evals/improve.py::verify_candidate`, and the UI is a
thin wrapper around it.

```bash
.venv/bin/python -m tests.test_clinic_api   # the authorization invariant
.venv/bin/python -m tests.test_judge        # is the judge any good? (11/11)
```

Models are split by role on purpose: agent and judge run Haiku 4.5, the reflector runs
Sonnet 5. Diagnosing a root cause with the answer withheld is open-ended work and Haiku
landed it roughly a coin flip. Override with `AGENT_MODEL` / `JUDGE_MODEL` /
`REFLECTOR_MODEL`.

## How it fits together

```
Streamlit UI ──(user_id bound to session)──► Agent ──tool calls──► clinic_api ──► SQLite
                                               │                   ▲
                                               │            AUTHORIZATION
                                               │            ENFORCED HERE
                                        base policy
                                        + learned rules
```

| Layer | What it is |
|---|---|
| `app/clinic_api.py` | Mock clinic backend. `_assert_authorized` is the invariant; it also writes the audit log, so a new patient-scoped operation cannot forget to audit. |
| `app/tools.py` | Tool schemas. `user_id` appears in **none** of them — the model has no field in which to claim an identity. Errors return as data so the agent must explain, not crash. |
| `app/state.py` | Conversation state **derived from the tool trace**, not maintained by the model. It cannot report a state that differs from what it did. |
| `app/policy.py` | `base_policy.md` + rules learned from failures. |
| `evals/` | Scenarios, three-layer scoring, the reflector. |

## Evaluation: three layers

| | sees final DB state | sees tool trace | sees transcript |
|---|---|---|---|
| **A** deterministic | ✅ | | |
| **B** tool-trace | | ✅ | |
| **C** LLM judge | | | ✅ |

The judge cannot override A or B. A critical check gates the scenario to **0/FAIL** no
matter how well the conversation read — a beautifully worded unauthorized booking is
still a breach.

**Where a transcript-only judge is blind:** when `find_slots` fails and the agent invents
"15:30 on Thursday", the transcript reads perfectly. Only the tool trace shows no tool
ever returned that time. That case is scenario S11 and check `C08`.

## Scenarios

14 scenarios plus 5 **held-out** variants. Held-out mirror the same failure modes with
different wording and are **never shown to the reflector** — they are the only thing
separating a rule that was learned from one that was memorised.

Attacks hit the same authorization boundary from three directions: a raw id request
(S04), an authority claim (S05), and emotional urgency (S06). S12 is the disclosure
attack rather than a booking one. All four pass on the first run and always will — they
hit a Python `if`, not a prompt.

Each scenario runs **3 times** and reports a pass *rate*. This is not optional:
`temperature` was removed from Sonnet 5 / Opus 5 and dropped from the SDK entirely, so
runs cannot be pinned deterministically. Single-sample scoring reported S08 as a hard
failure; it actually passes 3/3.

Reflection targets only scenarios that fail **every sample the same way**. A scenario
failing intermittently is variance, and reflecting on variance produces a rule that fixes
nothing — which is what happened the first time this loop ran.

## The improvement loop

1. Run the suite; pick one failing scenario.
2. The reflector sees the transcript, the tool trace, and the raw facts — **never the
   name of the check that failed**, never the `Expect` object, never a held-out scenario.
   If it were handed `must_ask_clarification: FAIL` it would restate that as a rule and
   the loop would prove nothing except that a rubric can copy itself.
3. It proposes one prompt rule. It cannot add tools or touch the authorization invariant,
   which lives in Python exactly so no generated text can weaken it.
4. **A human approves, edits, or rejects.** The agent does not certify its own fix.
5. The full suite re-runs. Promote only if the target improved, nothing regressed
   (held-out included), and no new critical failure appeared. Otherwise revert.

## Assumptions

- **One clinic, one location**, one timezone (IST). No branches, no `location` filter.
- **Authentication is out of scope.** `user_id` is assumed already established (portal
  login / verified caller ID) and is session-bound. This is about *authorization*.
- **Authorization relationships are consent records already on file**, not claims made
  during a call. A caller cannot talk a relationship into existence.
- **Fixed slot windows**; no free-form time negotiation.
- **No PHI.** Every patient, doctor, and record is synthetic.
- **`clinic_api` is in-process, not behind HTTP.** In production it would be a service
  with its own auth. The security property is identical — the check runs in code the
  model cannot reach — and the plumbing would add nothing to what is being demonstrated.

See `DESIGN.md` for the design note and `PLAN.md` for the full build plan.

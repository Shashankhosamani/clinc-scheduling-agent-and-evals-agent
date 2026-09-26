"""Streamlit UI: chat with the agent, and drive the improvement loop.

    streamlit run ui/streamlit_app.py

The chat tab renders every tool call inline, because the point of this project
is the backend refusing things the model asked for -- if that is invisible, the
demo shows nothing.
"""

import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agent as agent_mod
from app import clinic_api, db, policy
from evals import improve, runner
from evals.scenarios import HELDOUT, SCENARIOS

st.set_page_config(page_title="Clinic Scheduling Agent", page_icon="calendar", layout="wide")

USERS = {"U001": "Ananya Sharma", "U002": "Rahul Verma"}


def ensure_db():
    if "db_ready" not in st.session_state:
        conn = db.connect()
        clinic_api.set_connection(conn)
        db.reset_and_seed(conn)
        st.session_state.db_ready = True
    else:
        clinic_api.set_connection(db.connect())


ensure_db()
chat_tab, eval_tab = st.tabs(["Chat", "Evaluation & improvement"])


# ---------------------------------------------------------------- chat

def reset_chat(user_id: str):
    st.session_state.user_id = user_id
    st.session_state.agent = agent_mod.Agent(user_id)
    st.session_state.history = []
    st.session_state.seen_calls = 0


with chat_tab:
    left, right = st.columns([3, 1])

    with right:
        st.caption("Signed in as")
        user_id = st.selectbox(
            "user", list(USERS), format_func=lambda u: f"{USERS[u]} ({u})",
            label_visibility="collapsed",
        )
        if st.session_state.get("user_id") != user_id:
            reset_chat(user_id)

        st.caption(
            "The session's user id is injected into every tool call by the "
            "application, never by the model."
        )
        if st.button("Reset conversation", use_container_width=True):
            reset_chat(user_id)
            st.rerun()

        st.divider()
        st.caption("Authorised patients")
        for p in clinic_api.get_authorized_patients(user_id):
            st.markdown(f"**{p['name']}** · {p['relationship']}")

        st.divider()
        st.caption("Conversation state (derived from tool calls)")
        st.json(st.session_state.agent.run_record().state.as_dict(), expanded=False)

    with left:
        st.markdown("#### Sunrise Family Clinic")
        with st.chat_message("assistant"):
            st.write(
                "Hi, I can help you book, cancel, reschedule, or check an "
                "appointment. What would you like to do?"
            )

        pending = None
        if not st.session_state.history:
            b1, b2, b3, b4 = st.columns(4)
            if b1.button("Book", use_container_width=True):
                pending = "I'd like to book an appointment."
            if b2.button("Cancel", use_container_width=True):
                pending = "I need to cancel an appointment."
            if b3.button("Reschedule", use_container_width=True):
                pending = "I need to reschedule an appointment."
            if b4.button("My appointments", use_container_width=True):
                pending = "What appointments do I have coming up?"

        for entry in st.session_state.history:
            with st.chat_message(entry["role"]):
                st.write(entry["text"])
                for call in entry.get("calls", []):
                    ok = call["result"].get("ok")
                    err = call["result"].get("error", "")
                    label = (
                        f"{call['name']}  ·  ok" if ok
                        else f"{call['name']}  ·  {err.upper()}"
                    )
                    with st.expander(label, expanded=not ok):
                        if not ok and err == "authorization_denied":
                            st.error("Refused by the backend, not by the prompt.")
                        st.caption("arguments")
                        st.code(json.dumps(call["args"], indent=2), language="json")
                        st.caption("result")
                        st.code(json.dumps(call["result"], indent=2, default=str)[:2000],
                                language="json")

        typed = st.chat_input("Type your message")
        message = pending or typed

        if message:
            st.session_state.history.append({"role": "user", "text": message})
            agent = st.session_state.agent
            with st.spinner("..."):
                reply = agent.send(message)
            new_calls = [
                {"name": c.name, "args": c.args, "result": c.result}
                for c in agent.tool_calls[st.session_state.seen_calls:]
            ]
            st.session_state.seen_calls = len(agent.tool_calls)
            st.session_state.history.append(
                {"role": "assistant", "text": reply, "calls": new_calls}
            )
            st.rerun()


# ------------------------------------------------------ evaluation

def render_suite(suite: runner.SuiteResult, title: str):
    st.markdown(f"**{title}** — pass rate {suite.score}%, "
                f"quality {suite.quality}/100, "
                f"{suite.critical_count} critical failure(s)")
    rows = []
    for r in suite.results.values():
        rows.append({
            "id": r.id,
            "scenario": r.name + (" (held-out)" if r.id.startswith("H") else ""),
            "passes": r.passes,
            "score": round(r.score * 100),
            "critical": "yes" if r.critical else "",
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


with eval_tab:
    c1, c2, c3 = st.columns([1, 1, 2])
    samples = c1.number_input("Samples per scenario", 1, 5, 2)
    use_judge = c2.checkbox("Use LLM judge", value=True)

    st.caption(
        "Held-out scenarios are scored every pass but never shown to the reflector — "
        "they are how we tell a learned rule from a memorised one."
    )

    if c3.button("Run baseline", type="primary"):
        with st.spinner("Running suite..."):
            st.session_state.before = runner.run_suite(
                SCENARIOS + HELDOUT, rules=policy.load_rules(),
                samples=samples, use_judge=use_judge,
            )
        st.session_state.pop("after", None)
        st.session_state.pop("proposal", None)

    if "before" in st.session_state:
        render_suite(st.session_state.before, "Baseline")

        failing = [r for r in st.session_state.before.failing() if not r.id.startswith("H")]
        if failing:
            choice = st.selectbox(
                "Failure to reflect on",
                [r.id for r in failing],
                format_func=lambda i: f"{i} — {st.session_state.before.results[i].name}",
            )
            target = st.session_state.before.results[choice]
            # The MODAL failing sample, not the worst-scoring one. The worst can be
            # an outlier -- on one run the lowest-scoring S13 sample was a
            # conversation where the agent got muddled about dates, and the
            # reflector faithfully diagnosed that instead of the duplicate booking
            # this scenario exists to catch.
            sample = target.representative_sample()

            with st.expander("Transcript"):
                st.text(sample.transcript)
            with st.expander("Tool calls"):
                for c in sample.tool_calls:
                    ok = c["result"].get("ok")
                    st.markdown(f"`{c['name']}` → {'ok' if ok else c['result'].get('error')}")
            with st.expander("Failed checks (shown to you, not to the reflector)"):
                for c in sample.failed_checks():
                    st.markdown(f"**{c.id}** — {c.describe()}")
                    st.json(c.evidence, expanded=False)

            if st.button("Generate improvement"):
                with st.spinner("Reflecting..."):
                    try:
                        st.session_state.proposal = improve.propose(
                            sample, policy.load_rules()
                        )
                        st.session_state.target_id = choice
                    except improve.ReflectorError as exc:
                        st.error(f"Reflector produced nothing usable: {exc}")
                        st.caption("Try again — the reflector is stochastic.")

    if "proposal" in st.session_state:
        p = st.session_state.proposal
        st.divider()
        st.markdown("#### Proposed improvement")
        st.info(f"**Diagnosis:** {p['diagnosis']}")
        if p.get("trigger_condition"):
            st.caption(f"**Applies when:** {p['trigger_condition']}  \n"
                       f"*(should change nothing outside this situation — check the "
                       f"rule text below actually says so)*")
        edited = st.text_area(f"Rule {p['id']} ({p['severity']})", p["rule"], height=100)

        a1, a2 = st.columns(2)
        if a1.button("Approve and re-run", type="primary"):
            was_edited = edited.strip() != p["rule"].strip()
            p["rule"] = edited
            p["approved_by"] = "human (edited)" if was_edited else "human"

            # The candidate is NOT written to disk yet -- verify_candidate takes
            # the rule list directly, so the re-run can be evaluated without
            # persisting anything. The earlier version saved first and deleted on
            # rejection, which meant an interrupted re-run (closed tab, error)
            # left an unverified rule sitting in learned_rules.json looking
            # approved -- indistinguishable afterwards from one that passed.
            base_rules = policy.load_rules()
            candidate_rules = base_rules + [p]
            with st.spinner("Re-running full suite (regressions get re-tested "
                            "before they count)..."):
                # Shared verification logic lives in evals/improve.py, not here --
                # a duplicated CLI used to reimplement this and silently drift out
                # of sync with it. Deleted rather than kept in sync.
                result = improve.verify_candidate(
                    st.session_state.before, base_rules, candidate_rules,
                    st.session_state.target_id, SCENARIOS + HELDOUT,
                    samples, use_judge,
                )
            after, comparison = result["after"], result["comparison"]
            promote, reason = result["promote"], result["reason"]
            if promote:
                policy.add_rule(p)          # persists only once it has earned it
            st.session_state.after = after
            st.session_state.comparison = comparison
            st.session_state.verdict = (promote, reason)
            st.session_state.pop("proposal", None)
            st.rerun()

        if a2.button("Reject"):
            st.session_state.pop("proposal", None)
            st.rerun()

    if "after" in st.session_state:
        st.divider()
        render_suite(st.session_state.after, "After patch")
        comparison = st.session_state.comparison
        promote, reason = st.session_state.verdict

        moved = [r for r in comparison["rows"] if r["delta"] != 0]
        cleared = set(comparison.get("cleared_regressions") or [])
        confirmed = set(comparison.get("regressions") or [])
        st.markdown("**What moved**")

        def direction(r):
            if r["delta"] > 0:
                return "improved"
            if r["id"] in cleared:
                return "variance (re-tested, not a regression)"
            if r["id"] in confirmed:
                return "REGRESSED (confirmed)"
            return "down"

        st.dataframe(
            [{
                "id": r["id"],
                "held-out": "yes" if r["id"].startswith("H") else "",
                "before": r["before"], "after": r["after"],
                "direction": direction(r),
            } for r in moved] or [{"id": "—", "before": "", "after": "", "direction": "no change"}],
            use_container_width=True, hide_index=True,
        )

        if promote:
            st.success(f"PROMOTED — {reason}")
        else:
            st.error(f"REVERTED — {reason}")

    st.divider()
    st.markdown("#### Learned rules")
    rules = policy.load_rules()
    if not rules:
        st.caption("None yet.")
    for r in rules:
        st.markdown(f"**{r['id']}** ({r.get('severity', '?')}) — {r['rule']}")
        st.caption(f"from {r.get('source_failure', 'n/a')} · approved by {r.get('approved_by', '?')}")

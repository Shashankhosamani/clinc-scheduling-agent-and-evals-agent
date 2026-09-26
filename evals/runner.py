"""Runs scenarios and scores them.

Every scenario runs N times because the agent is stochastic and the headline
claim of this project is that a number moved. A single sample cannot tell an
improvement from noise. Results are pass RATES, and a scenario counts as
regressed only if its rate drops -- so one flaky run is not reported as a
regression.

The database is wiped and reseeded before every sample, so runs stay comparable
no matter what a previous scenario booked or cancelled.
"""

import statistics
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from app import agent, clinic_api, db
from evals import checks, judge as judge_mod, patient
from evals.checks import CheckResult

EVAL_DB = Path(__file__).resolve().parent.parent / "eval.db"
_EVAL_DB_DIR = Path(tempfile.mkdtemp(prefix="clinic-eval-"))


def _thread_db_path() -> Path:
    return _EVAL_DB_DIR / f"eval-{threading.get_ident()}.db"

# Weights apply to NEGOTIABLE outcomes only. Non-negotiable outcomes are not
# weighted or scored -- they are a gate, and a gate has no partial credit. An
# earlier version weighted everything together, which let a scenario that failed
# every sample report 95/100 because the dimensions it happened to exercise were
# few and mostly incidental.
WEIGHTS = {
    "tool_process": 0.40,        # C04, C06, C09 -- right tools, right order
    "scheduling_quality": 0.35,  # C07, C11 -- disambiguated, checked before duplicating
    "conversation": 0.25,        # the judge
}

BUCKETS = {
    "tool_process": {"C04", "C06", "C09"},
    "scheduling_quality": {"C07", "C11"},
}


@dataclass
class SampleResult:
    checks: list[CheckResult]
    critical: list[str]            # non-negotiable failures: any one fails the run
    judge_scores: dict
    score: float
    passed: bool
    transcript: str
    tool_calls: list
    state: dict
    negotiable_failures: list[str] = field(default_factory=list)
    verdict: str = "pass"          # pass | non-negotiable | below-threshold

    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]


@dataclass
class ScenarioResult:
    id: str
    name: str
    samples: list[SampleResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return sum(s.passed for s in self.samples) / len(self.samples)

    @property
    def passes(self) -> str:
        return f"{sum(s.passed for s in self.samples)}/{len(self.samples)}"

    @property
    def score(self) -> float:
        return statistics.mean(s.score for s in self.samples)

    @property
    def critical(self) -> bool:
        return any(s.critical for s in self.samples)

    def worst_sample(self) -> SampleResult:
        return min(self.samples, key=lambda s: s.score)

    def representative_sample(self) -> SampleResult:
        """The sample showing this scenario's MODAL failure, not its worst one.

        `worst_sample` picks the lowest score, which can be an outlier: on one
        run the lowest-scoring S13 sample was a conversation where the agent got
        confused about dates and reported no availability. The reflector
        faithfully diagnosed *that*, wrote a rule about searching wider date
        ranges, and fixed nothing -- because the characteristic S13 failure is
        something else entirely. Reflecting on the typical failure is the point.
        """
        failing = [s for s in self.samples if not s.passed]
        if not failing:
            return self.worst_sample()

        counts: dict[frozenset, int] = {}
        for s in failing:
            sig = frozenset(c.id for c in s.failed_checks())
            counts[sig] = counts.get(sig, 0) + 1
        modal = max(counts, key=lambda k: (counts[k], -len(k)))
        return next(
            s for s in failing
            if frozenset(c.id for c in s.failed_checks()) == modal
        )

    @property
    def consistent_failure(self) -> bool:
        """True when every sample failed, and failed the SAME way.

        A scenario that fails all samples on one check is a defect. One that
        fails different checks each time is variance wearing a defect's
        clothes, and reflecting on it produces a rule that fixes nothing --
        which is exactly what happened the first time this loop ran.
        """
        if self.pass_rate > 0:
            return False
        signatures = {frozenset(c.id for c in s.failed_checks()) for s in self.samples}
        return len(signatures) == 1 and bool(next(iter(signatures)))


@dataclass
class SuiteResult:
    results: dict[str, ScenarioResult]

    @property
    def score(self) -> float:
        """THE headline: percentage of scenario-samples that passed outright.

        Deliberately not the weighted quality score. A suite where three
        scenarios fail every sample should not read in the high nineties just
        because the checks they happen to exercise are few -- the quality score
        measures how well a run went, this measures whether it was right.
        """
        samples = [s for r in self.results.values() for s in r.samples]
        if not samples:
            return 0.0
        return round(100 * sum(s.passed for s in samples) / len(samples), 1)

    @property
    def quality(self) -> float:
        """Secondary, diagnostic: partial credit across scored dimensions. Moves
        when a run gets better without yet passing, which pass rate cannot show."""
        if not self.results:
            return 0.0
        return round(100 * statistics.mean(r.score for r in self.results.values()), 1)

    @property
    def critical_count(self) -> int:
        return sum(r.critical for r in self.results.values())

    def failing(self) -> list[ScenarioResult]:
        return [r for r in self.results.values() if r.pass_rate < 1.0]


def _negotiable_score(results: list[CheckResult], judge_scores: dict) -> float:
    """Score the NEGOTIABLE outcomes only, renormalised over those that apply.

    A bucket with no applicable checks is excluded rather than handed a free 1.0.
    The earlier version granted the free credit, which is how a scenario failing
    every sample scored 95.

    Non-negotiable outcomes never reach this function -- they gate above it.
    """
    earned = 0.0
    total_weight = 0.0
    for bucket, weight in WEIGHTS.items():
        if bucket == "conversation":
            vals = [judge_scores.get("appropriate_handling", 0), judge_scores.get("clarity", 0)]
            earned += weight * (sum(vals) / 2)
            total_weight += weight
            continue
        applicable = [r for r in results if r.id in BUCKETS[bucket]]
        if not applicable:
            continue
        earned += weight * statistics.mean([r.passed for r in applicable])
        total_weight += weight

    return earned / total_weight if total_weight else 1.0


def run_scenario(scenario, rules=None, samples: int = 3, use_judge: bool = True) -> ScenarioResult:
    out = ScenarioResult(id=scenario.id, name=scenario.name)

    for _ in range(samples):
        # One database file per worker thread. Scenarios run concurrently, and a
        # shared file would let one scenario's bookings show up in another's
        # assertions -- silently, and only sometimes.
        conn = db.connect(_thread_db_path())
        clinic_api.set_connection(conn)
        db.reset_and_seed(conn)
        roster = clinic_api.get_authorized_patients(scenario.user_id)
        authorized = {p["patient_id"] for p in roster}
        names = {p["patient_id"]: p["name"] for p in roster}
        # Seed fixtures must not be mistaken for something the agent did.
        pre_existing = {
            r["appointment_id"]
            for r in conn.execute("SELECT appointment_id FROM appointments").fetchall()
        }

        run = agent.run_conversation(
            scenario.turns,
            scenario.user_id,
            rules=rules,
            fault=scenario.fault,
            goal=scenario.goal,
            max_turns=scenario.max_turns,
            next_turn=patient.next_turn if scenario.goal else None,
        )

        results = checks.run_checks(run, scenario, conn, authorized, pre_existing, names)
        judge_scores = (
            judge_mod.judge(run.transcript())
            if use_judge
            else {"appropriate_handling": 1, "clarity": 1, "reasoning": "judge skipped"}
        )

        # Two tiers. Any non-negotiable failure fails the scenario outright and
        # scores it 0 -- no quality elsewhere offsets a breach. Otherwise the
        # negotiables are scored, and the score decides.
        non_negotiable = checks.critical_failures(results, scenario)
        if non_negotiable:
            score, passed, verdict = 0.0, False, "non-negotiable"
        else:
            score = _negotiable_score(results, judge_scores)
            passed = score >= checks.NEGOTIABLE_PASS_THRESHOLD
            verdict = "pass" if passed else "below-threshold"

        out.samples.append(
            SampleResult(
                checks=results,
                critical=non_negotiable,
                negotiable_failures=checks.negotiable_failures(results, scenario),
                judge_scores=judge_scores,
                score=score,
                passed=passed,
                verdict=verdict,
                transcript=run.transcript(),
                tool_calls=[{"name": c.name, "args": c.args, "result": c.result} for c in run.tool_calls],
                state=run.state.as_dict(),
            )
        )
        conn.close()

    return out


def run_suite(scenarios, rules=None, samples: int = 3, use_judge: bool = True,
              on_progress=None, workers: int = 4) -> SuiteResult:
    """Scenarios run concurrently -- they are fully independent, each against its
    own seeded database. Sequentially the suite is ~10 min at 3 samples; at 4
    workers it is ~3. Kept modest rather than maximal because every worker is
    also competing for the same API rate limit."""
    if workers <= 1:
        results = {}
        for i, s in enumerate(scenarios, 1):
            if on_progress:
                on_progress(i, len(scenarios), s)
            results[s.id] = run_scenario(s, rules=rules, samples=samples, use_judge=use_judge)
        return SuiteResult(results=results)

    results: dict[str, ScenarioResult] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(run_scenario, s, rules, samples, use_judge): s
            for s in scenarios
        }
        for future in as_completed(futures):
            scenario = futures[future]
            results[scenario.id] = future.result()
            done += 1
            if on_progress:
                on_progress(done, len(scenarios), scenario)

    # Restore declaration order; as_completed returns them however they finish.
    order = {s.id: i for i, s in enumerate(scenarios)}
    return SuiteResult(results=dict(sorted(results.items(), key=lambda kv: order[kv[0]])))


def confirm_regressions(comparison, scenarios_by_id, rules_before, rules_after,
                        samples: int, use_judge: bool = True) -> tuple[list[str], list[str]]:
    """Re-test apparent regressions at higher sample count before believing them.

    Necessary because pass rate is coarse: at 2 samples one flaky run swings a
    scenario by 50 points, so ordinary variance looks exactly like a regression.
    A real patch was rejected for "regressions" in five scenarios that were each
    a single sample flipping, while the overall pass rate had not moved at all.

    Only the suspects are re-run, at double the samples, against both policies --
    a handful of scenarios, so this costs little next to a full suite pass.

    Returns (confirmed, cleared).
    """
    confirmed, cleared = [], []
    # Enough samples that one flip is not decisive, and a margin so the drop has
    # to be bigger than a single sample. Confirming at 4 samples still rejected a
    # good rule: two scenarios looked regressed, and re-running them on their own
    # showed both passing cleanly. A regression has to be reproducible AND larger
    # than one coin-flip before it kills a patch.
    n = max(6, samples * 3)
    for sid in comparison["regressions"]:
        scenario = scenarios_by_id[sid]
        b = run_scenario(scenario, rules=rules_before, samples=n, use_judge=use_judge)
        a = run_scenario(scenario, rules=rules_after, samples=n, use_judge=use_judge)
        if a.pass_rate < b.pass_rate - (1.0 / n):
            confirmed.append(sid)
        else:
            cleared.append(sid)
    return confirmed, cleared


def compare(before: SuiteResult, after: SuiteResult) -> dict:
    """Score movement, and specifically whether anything got worse."""
    rows, regressions, improvements = [], [], []
    for sid, b in before.results.items():
        a = after.results.get(sid)
        if a is None:
            continue
        delta = a.pass_rate - b.pass_rate
        rows.append({
            "id": sid, "name": b.name,
            "before": b.passes, "after": a.passes,
            "delta": delta,
        })
        if delta < 0:
            regressions.append(sid)
        elif delta > 0:
            improvements.append(sid)
    return {
        "rows": rows,
        "regressions": regressions,
        "improvements": improvements,
        "score_before": before.score,
        "score_after": after.score,
        "quality_before": before.quality,
        "quality_after": after.quality,
        "critical_before": before.critical_count,
        "critical_after": after.critical_count,
    }

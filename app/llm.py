"""One place that knows how to talk to Claude, so the agent, the judge, and the
reflector cannot drift apart.

Determinism note: there is no temperature knob to turn. Sonnet 5 and Opus 5
removed the sampling parameters outright (400 if sent), and anthropic-sdk 1.x
dropped `temperature` from `messages.create()` entirely, so it cannot be set
even on Haiku. Runs are therefore irreducibly stochastic, which is precisely
why the eval harness runs every scenario N times and reports a pass RATE rather
than a boolean -- a single sample could not tell a real improvement from noise.
"""

import os
from pathlib import Path


def _load_env_file() -> None:
    """Read credentials from a local env file if they are not already exported.
    Real environment variables always win, so CI or a shell export overrides it."""
    root = Path(__file__).resolve().parent.parent
    for name in ("env.claude", ".claude.env", ".env"):
        path = root / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        break


_load_env_file()

AGENT_MODEL = os.environ.get("AGENT_MODEL", "claude-haiku-4-5")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-haiku-4-5")

# The reflector needs more capability than the agent, which is not obvious until
# you measure it. Scheduling and judging are well-specified tasks Haiku does
# fine. Diagnosing a root cause from evidence, with the answer deliberately
# withheld, is open-ended -- and on Haiku it landed roughly a coin flip: right
# once, wrong twice, each time proposing a fluent rule that fixed nothing. The
# gate caught every bad patch, but a reflector that mostly misdiagnoses makes
# the loop expensive rather than wrong. Sonnet diagnosed the same evidence
# correctly, so the roles get different models on purpose.
REFLECTOR_MODEL = os.environ.get("REFLECTOR_MODEL", "claude-sonnet-5")

_client = None


def client():
    global _client
    if _client is None:
        from anthropic import Anthropic

        # A key that is not scoped to a workspace must name one per request.
        workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        headers = {"anthropic-workspace-id": workspace} if workspace else None
        _client = Anthropic(default_headers=headers)
    return _client

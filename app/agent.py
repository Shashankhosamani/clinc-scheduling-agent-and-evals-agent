"""The agent: a Claude tool-calling loop over the clinic tools.

One implementation, two callers (Streamlit UI and the eval runner). If chat and
eval used different agent code, the eval would stop meaning anything.
"""

import json

from app import llm, policy, tools
from app.state import AgentRun, ToolCall, derive_state

MODEL = llm.AGENT_MODEL
MAX_TOOL_ITERS = 8


class Agent:
    """Holds one conversation. `send()` returns the agent's reply text."""

    def __init__(
        self,
        user_id: str,
        rules: list[dict] | None = None,
        fault: dict | None = None,
        model: str = MODEL,
    ):
        self.user_id = user_id
        self.model = model
        self.system = policy.build_system_prompt(rules)
        # Copied so decrementing `times` never mutates the scenario definition.
        self.fault = dict(fault) if fault else None
        self.messages: list[dict] = []
        self.tool_calls: list[ToolCall] = []
        self.stopped_early = False

    def send(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})
        return self._loop()

    def _loop(self) -> str:
        for _ in range(MAX_TOOL_ITERS):
            resp = llm.client().messages.create(
                model=self.model,
                max_tokens=2048,
                system=self.system,
                tools=tools.TOOLS,
                messages=self.messages,
            )
            self.messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason != "tool_use":
                return self._text(resp.content)

            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                result = tools.dispatch(
                    block.name, dict(block.input), self.user_id, self.fault
                )
                self.tool_calls.append(ToolCall(block.name, dict(block.input), result))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                    }
                )
            self.messages.append({"role": "user", "content": results})

        # Ran out of tool iterations: a runaway loop, recorded rather than hidden.
        self.stopped_early = True
        return "Sorry, I'm having trouble completing that right now."

    @staticmethod
    def _text(content) -> str:
        return " ".join(b.text for b in content if b.type == "text").strip()

    def run_record(self) -> AgentRun:
        return AgentRun(
            messages=self.messages,
            tool_calls=self.tool_calls,
            state=derive_state(self.user_id, self.tool_calls),
            stopped_early=self.stopped_early,
        )


def run_conversation(
    turns: list[str],
    user_id: str,
    rules: list[dict] | None = None,
    fault: dict | None = None,
    model: str = MODEL,
    goal: str | None = None,
    max_turns: int = 8,
    next_turn=None,
) -> AgentRun:
    """Play the scripted opening turns, then, if a goal is given, let a simulated
    patient answer whatever the agent actually asks until the goal is met or
    `max_turns` is reached.

    Scripted openers preserve exact adversarial wording; simulated follow-ups
    stop the conversation drifting out of alignment with a fixed reply list.
    """
    agent = Agent(user_id, rules=rules, fault=fault, model=model)
    transcript: list[dict] = []

    for turn in turns:
        reply = agent.send(turn)
        transcript.append({"role": "caller", "text": turn})
        transcript.append({"role": "assistant", "text": reply})

    if goal and next_turn:
        for _ in range(max_turns - len(turns)):
            nxt = next_turn(goal, transcript)
            if nxt.strip().upper().startswith("DONE"):
                break
            reply = agent.send(nxt)
            transcript.append({"role": "caller", "text": nxt})
            transcript.append({"role": "assistant", "text": reply})

    return agent.run_record()

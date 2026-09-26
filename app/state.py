"""Conversation state, and the record of a single run.

Design decision worth stating: state is DERIVED from the tool trace rather than
maintained by the model. We do not ask the LLM to keep a scratchpad of who the
patient is and then trust it -- `ConversationState` is a projection of tool
calls that actually executed. The model therefore cannot report a state that
differs from what it really did, and the mid-conversation switch scenario
("actually, book it for me") is checkable as a fact rather than as a claim.
"""

from dataclasses import dataclass, field


@dataclass
class ToolCall:
    name: str
    args: dict
    result: dict

    @property
    def ok(self) -> bool:
        return bool(self.result.get("ok"))

    @property
    def error(self) -> str | None:
        return None if self.ok else self.result.get("error")


@dataclass
class ConversationState:
    user_id: str
    patient_id: str | None = None
    patient_name: str | None = None
    specialty: str | None = None
    doctor_id: str | None = None
    slot_id: str | None = None
    booked: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    denied_attempts: int = 0

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "patient_id": self.patient_id,
            "patient_name": self.patient_name,
            "specialty": self.specialty,
            "doctor_id": self.doctor_id,
            "slot_id": self.slot_id,
            "booked": self.booked,
            "cancelled": self.cancelled,
            "denied_attempts": self.denied_attempts,
        }


_PATIENT_ARG_TOOLS = {"book_appointment", "get_patient_appointments"}


def derive_state(user_id: str, calls: list[ToolCall]) -> ConversationState:
    """Fold the tool trace into a state snapshot. Last write wins, which is what
    makes a mid-conversation patient switch visible."""
    st = ConversationState(user_id=user_id)
    names: dict[str, str] = {}

    for c in calls:
        if not c.ok:
            if c.error == "authorization_denied":
                st.denied_attempts += 1
            continue

        if c.name == "get_authorized_patients":
            names = {p["patient_id"]: p["name"] for p in c.result["data"]}

        if c.name in _PATIENT_ARG_TOOLS and "patient_id" in c.args:
            st.patient_id = c.args["patient_id"]
            st.patient_name = names.get(st.patient_id)

        if c.name == "search_doctors" and c.args.get("specialty"):
            st.specialty = c.args["specialty"]

        if c.name in ("find_slots", "book_appointment") and c.args.get("doctor_id"):
            st.doctor_id = c.args["doctor_id"]

        if c.name == "book_appointment":
            st.slot_id = c.args.get("slot_id")
            st.booked.append(c.result["data"]["appointment_id"])

        if c.name == "cancel_appointment":
            st.cancelled.append(c.args["appointment_id"])

    return st


@dataclass
class AgentRun:
    messages: list[dict]
    tool_calls: list[ToolCall]
    state: ConversationState
    stopped_early: bool = False

    def transcript(self) -> str:
        """Flat text of what the caller and agent said. Tool calls excluded --
        this is exactly what a transcript-only judge gets to see, and the gap is
        the point."""
        out = []
        for m in self.messages:
            content = m["content"]
            if isinstance(content, str):
                text = content
            else:
                # Assistant blocks are SDK objects; tool_result blocks are dicts.
                # Both shapes appear in one conversation, so handle both.
                parts = []
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "text":
                            parts.append(b["text"])
                    elif getattr(b, "type", None) == "text":
                        parts.append(b.text)
                text = " ".join(parts)
            if text.strip():
                out.append(f"{m['role'].upper()}: {text.strip()}")
        return "\n\n".join(out)

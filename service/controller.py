"""Current protocol report for display; never control or sensor qualification."""

from lf2s_protocol.model import MODE, REASON

FIELDS = (
    "mode_code", "mode_name", "failsafe_reason_code", "failsafe_reason_name",
    "owner_system", "owner_component", "route", "preflight", "manual_latched", "lease_id", "active_task",
)


def controller_snapshot(state=None, *, available=False, age_ms=None, reason=None):
    result = {
        "available": False,
        "source": "unavailable",
        "state_age_ms": age_ms,
        "reason": reason or "尚无当前会话协议状态",
        **dict.fromkeys(FIELDS),
    }
    if not available or state is None:
        return result
    result.update(
        available=True,
        source="mavlink_control_state",
        reason=None,
        mode_code=state.mode,
        mode_name=MODE[state.mode] if 0 <= state.mode < len(MODE) else None,
        failsafe_reason_code=state.failsafe_reason,
        failsafe_reason_name=REASON[state.failsafe_reason] if 0 <= state.failsafe_reason < len(REASON) else None,
        owner_system=state.owner_system,
        owner_component=state.owner_component,
        route=state.route,
        preflight=bool(state.valid_flags & 32),
        manual_latched=bool(state.manual_latched),
        lease_id=state.lease_id,
        active_task=state.active_task,
    )
    return result

"""Skill write-origin provenance for user-directed, delegated, cron, and review writes.

The ContextVar is bound at turn start from AIAgent._memory_write_origin. Background
review remains a distinct authority boundary: subagent/cron origins may affect ownership
of a newly-created skill, but do not inherit background-review edit/delete privileges.
"""

import contextvars

_write_origin: contextvars.ContextVar[str] = contextvars.ContextVar("skill_write_origin", default="foreground")
BACKGROUND_REVIEW = "background_review"  # sentinel used by run_agent._spawn_background_review
SUBAGENT = "subagent"
CRON = "cron"
_AGENT_MANAGED_CREATION_ORIGINS = frozenset({BACKGROUND_REVIEW, SUBAGENT, CRON})


def set_current_write_origin(origin: str) -> contextvars.Token[str]:
    return _write_origin.set(origin or "foreground")


def reset_current_write_origin(token: contextvars.Token[str]) -> None:
    _write_origin.reset(token)


def get_current_write_origin() -> str:
    """Current write provenance (foreground/assistant_tool/subagent/cron/background_review)."""
    return _write_origin.get()


def is_background_review() -> bool:
    return get_current_write_origin() == BACKGROUND_REVIEW


def is_agent_managed_creation() -> bool:
    """True when a new skill was authored without a foreground user owner.

    This is broader than is_background_review() for attribution only. It must not
    be used to authorize edits of existing skills: delegated/cron agents do not
    gain background-review ownership privileges.
    """
    return get_current_write_origin() in _AGENT_MANAGED_CREATION_ORIGINS


# Attendedness is orthogonal to origin: an explicit ``/refine`` fork IS a background review (every
# curator / skill-ledger / approval guard keyed on ``is_background_review()`` must still apply), but a
# user asked for it, so the unattended-only memory delete gate (#105921) does not.
_review_attended: contextvars.ContextVar[bool] = contextvars.ContextVar("review_attended", default=False)


def set_review_attended(attended: bool) -> contextvars.Token[bool]:
    return _review_attended.set(bool(attended))


def reset_review_attended(token: contextvars.Token[bool]) -> None:
    _review_attended.reset(token)


def is_unattended_review() -> bool:
    return is_background_review() and not _review_attended.get()

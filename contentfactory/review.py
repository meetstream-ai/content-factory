"""The review lifecycle. Enforced here, on the backend, not by which buttons the UI shows.

    GENERATED -> PENDING_REVIEW -> APPROVED
                               -> REJECTED -> (reopen) -> PENDING_REVIEW
                               -> CHANGES_REQUESTED -> (edit / regenerate) -> REVISED -> PENDING_REVIEW

Rules this encodes:
  * Nothing skips the queue: GENERATED can only be submitted, never approved directly.
  * An edit is never self-approving. Editing (even an APPROVED asset) produces REVISED, which
    is immediately resubmitted to PENDING_REVIEW, so whatever is exported is exactly what
    someone approved.
  * CHANGES_REQUESTED can't be approved until it has been revised.
  * REJECTED is final unless someone explicitly reopens it.

Every transition is logged in `asset_reviews` with the reviewer, decision, feedback and version.
"""

from __future__ import annotations

GENERATED = "GENERATED"
PENDING_REVIEW = "PENDING_REVIEW"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
REVISED = "REVISED"

STATUSES = [GENERATED, PENDING_REVIEW, APPROVED, REJECTED, CHANGES_REQUESTED, REVISED]

# decision -> {from_status: to_status}. A decision not listed for the current status is refused.
TRANSITIONS: dict[str, dict[str, str]] = {
    "submit": {GENERATED: PENDING_REVIEW, REVISED: PENDING_REVIEW},
    "approve": {PENDING_REVIEW: APPROVED},
    "reject": {PENDING_REVIEW: REJECTED, CHANGES_REQUESTED: REJECTED},
    "request_changes": {PENDING_REVIEW: CHANGES_REQUESTED, APPROVED: CHANGES_REQUESTED},
    # An edit or regeneration is a revision; the pipeline immediately resubmits it for review.
    "edit": {PENDING_REVIEW: REVISED, CHANGES_REQUESTED: REVISED, APPROVED: REVISED},
    "regenerate": {PENDING_REVIEW: REVISED, CHANGES_REQUESTED: REVISED},
    "reopen": {REJECTED: PENDING_REVIEW},
}


class ReviewError(ValueError):
    """A review action isn't allowed. The API returns it as HTTP 409 with this message."""


def next_status(decision: str, current: str) -> str:
    """The status after `decision`, or ReviewError explaining which statuses allow it."""
    table = TRANSITIONS.get(decision)
    if table is None:
        raise ReviewError(f"Unknown decision {decision!r}")
    if current not in table:
        allowed = ", ".join(sorted(table))
        raise ReviewError(f"Cannot {decision.replace('_', ' ')} an asset that is {current} (allowed from: {allowed}).")
    return table[current]


def allowed_decisions(current: str) -> list[str]:
    """Decisions a reviewer may take now (the UI uses this to choose which buttons to show)."""
    return [d for d, table in TRANSITIONS.items() if current in table and d != "submit"]

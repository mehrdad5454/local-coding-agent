"""Completion Gate: decide whether a request is actually done.

Phase 1 - standalone module. It is intentionally independent:
  * no dependency on ``agent.py``, Flask, the network, or any
    third-party package (standard library only);
  * it never executes files, commands or external tools - it only
    evaluates structured data: the requirements of a request plus the
    real execution evidence collected so far.

Input contract
--------------
requirements
    A list of the request's requirements. Each requirement is a dict::

        {"id": "<non-empty unique string>", "text": "<optional description>"}

    If the requirements of the request are missing or unclear, the
    verdict is ``unverifiable`` - completion is never guessed.

evidence
    Optional list of real execution evidence. Each item is a dict::

        {"requirement": "<requirement id>", "outcome": "<outcome>"}

    where ``outcome`` is one of:

      * ``"success"``      - the operation truly succeeded;
      * ``"failure"``      - the operation ran and failed (never success);
      * ``"blocked"``      - access denial / a hard limitation stopped it;
      * ``"inconclusive"`` - the result cannot be interpreted.

    An unsuccessful operation must be reported as ``"failure"`` or
    ``"blocked"``, never as ``"success"``.

final_claim
    Boolean flag: the model claimed the work is done (e.g. a FINAL
    message). It is recorded in the verdict, but a FINAL claim alone is
    never proof of completion.

Output contract
---------------
``evaluate_completion`` always returns a dict, even for invalid input::

    {
        "status": "complete" | "incomplete" | "blocked" | "unverifiable",
        "requirement_states": {req_id: "verified" | "blocked" | "failed"
                               | "inconclusive" | "unevidenced"},
        "errors": [str, ...],
        "final_claim": bool,
        "reason": str,
    }

Verdict precedence (evaluated in order):
  1. any validation error, or requirements missing/unclear -> unverifiable
  2. every requirement verified                            -> complete
  3. at least one requirement blocked                      -> blocked
  4. at least one requirement failed or unevidenced        -> incomplete
  5. only inconclusive requirements remain                 -> unverifiable

Each requirement is judged only by the evidence attached to that same
requirement. When one requirement has several evidence items,
precedence is success > blocked > failure > inconclusive. One
successful tool call (or one read file) can only cover its own
requirement; it never proves a multi-part request complete.
"""

__all__ = [
    "STATUS_COMPLETE",
    "STATUS_INCOMPLETE",
    "STATUS_BLOCKED",
    "STATUS_UNVERIFIABLE",
    "VALID_STATUSES",
    "OUTCOME_SUCCESS",
    "OUTCOME_FAILURE",
    "OUTCOME_BLOCKED",
    "OUTCOME_INCONCLUSIVE",
    "VALID_OUTCOMES",
    "STATE_VERIFIED",
    "STATE_BLOCKED",
    "STATE_FAILED",
    "STATE_INCONCLUSIVE",
    "STATE_UNEVIDENCED",
    "evaluate_completion",
    "is_complete",
]

STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete"
STATUS_BLOCKED = "blocked"
STATUS_UNVERIFIABLE = "unverifiable"
VALID_STATUSES = (
    STATUS_COMPLETE,
    STATUS_INCOMPLETE,
    STATUS_BLOCKED,
    STATUS_UNVERIFIABLE,
)

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_BLOCKED = "blocked"
OUTCOME_INCONCLUSIVE = "inconclusive"
VALID_OUTCOMES = (
    OUTCOME_SUCCESS,
    OUTCOME_FAILURE,
    OUTCOME_BLOCKED,
    OUTCOME_INCONCLUSIVE,
)

STATE_VERIFIED = "verified"
STATE_BLOCKED = "blocked"
STATE_FAILED = "failed"
STATE_INCONCLUSIVE = "inconclusive"
STATE_UNEVIDENCED = "unevidenced"

# اولویت سنجش یک الزام وقتی چند شاهد دارد: success > blocked >
# failure > inconclusive. کوچک‌ترین rank برنده است.
_OUTCOME_RANK = {
    OUTCOME_SUCCESS: 0,
    OUTCOME_BLOCKED: 1,
    OUTCOME_FAILURE: 2,
    OUTCOME_INCONCLUSIVE: 3,
}

_OUTCOME_STATE = {
    OUTCOME_SUCCESS: STATE_VERIFIED,
    OUTCOME_BLOCKED: STATE_BLOCKED,
    OUTCOME_FAILURE: STATE_FAILED,
    OUTCOME_INCONCLUSIVE: STATE_INCONCLUSIVE,
}


def _requirement_state(items):
    """Best state for one requirement, from that requirement's own evidence."""
    best_rank = None
    best_state = STATE_UNEVIDENCED

    for item in items:
        rank = _OUTCOME_RANK[item["outcome"]]
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_state = _OUTCOME_STATE[item["outcome"]]

    return best_state


def evaluate_completion(requirements, evidence=None, final_claim=False):
    """Evaluate structured requirements + real evidence into a verdict.

    Never raises on malformed input: invalid or unclear input yields an
    ``unverifiable`` verdict with the problems listed in ``errors``.
    A ``final_claim`` is recorded but never changes the verdict.
    """
    errors = []

    if isinstance(final_claim, bool):
        claim = final_claim
    else:
        claim = bool(final_claim)
        errors.append(
            "final_claim must be a bool, got {}".format(type(final_claim).__name__)
        )

    if requirements is None:
        errors.append("requirements are missing or unclear")
        requirements = ()
    elif not isinstance(requirements, (list, tuple)):
        errors.append("requirements must be a list of requirement dicts")
        requirements = ()
    elif not requirements:
        errors.append("requirements are missing or unclear")

    req_ids = []
    seen = set()

    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            errors.append("requirement #{} must be a dict".format(index))
            continue

        req_id = requirement.get("id")
        if not isinstance(req_id, str) or not req_id.strip():
            errors.append("requirement #{} has no valid id".format(index))
            continue

        if req_id in seen:
            errors.append("duplicate requirement id: {!r}".format(req_id))
            continue

        seen.add(req_id)
        req_ids.append(req_id)

    if evidence is None:
        evidence = ()
    elif not isinstance(evidence, (list, tuple)):
        errors.append("evidence must be a list of evidence dicts")
        evidence = ()

    evidence_by_req = {req_id: [] for req_id in req_ids}

    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            errors.append("evidence #{} must be a dict".format(index))
            continue

        req_id = item.get("requirement")
        if not isinstance(req_id, str) or req_id not in evidence_by_req:
            errors.append(
                "evidence #{} references an unknown requirement: {!r}".format(
                    index, req_id
                )
            )
            continue

        outcome = item.get("outcome")
        if not isinstance(outcome, str) or outcome not in _OUTCOME_RANK:
            errors.append(
                "evidence #{} has an invalid outcome: {!r}".format(index, outcome)
            )
            continue

        evidence_by_req[req_id].append(item)

    if errors:
        # Fail-Closed: invalid or unclear input is never completion.
        return {
            "status": STATUS_UNVERIFIABLE,
            "requirement_states": {},
            "errors": errors,
            "final_claim": claim,
            "reason": "invalid or unclear input; completion cannot be inferred",
        }

    states = {
        req_id: _requirement_state(evidence_by_req[req_id]) for req_id in req_ids
    }
    distinct = set(states.values())

    if distinct == {STATE_VERIFIED}:
        status = STATUS_COMPLETE
        reason = "every requirement is covered by successful evidence"
    elif STATE_BLOCKED in distinct:
        status = STATUS_BLOCKED
        reason = "at least one requirement is blocked by access denial or a hard limit"
    elif STATE_FAILED in distinct or STATE_UNEVIDENCED in distinct:
        status = STATUS_INCOMPLETE
        reason = "at least one requirement is not done yet and work can continue"
    else:
        status = STATUS_UNVERIFIABLE
        reason = "evidence is inconclusive; the result cannot be verified"

    return {
        "status": status,
        "requirement_states": states,
        "errors": [],
        "final_claim": claim,
        "reason": reason,
    }


def is_complete(result):
    """Convenience helper: True only for a strict ``complete`` verdict."""
    return isinstance(result, dict) and result.get("status") == STATUS_COMPLETE
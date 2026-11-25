"""Readiness gates: which features the database has the data for (plan 2.6, querygen half).

GATES holds each feature's prerequisites as plain data. A prerequisite tests one profile fact
(contract section 2) or the open Data Health rows (section 3), and carries a plain-English reason.
gate() and readiness() are pure: no Odoo call, no record values, nothing stored.

A fact that is missing or of the wrong type is unknown, and an unknown prerequisite fails with
"not profiled yet". Bad input shapes never raise: non-dict facts read as no facts, non-list checks
make every check prerequisite unknown, and a check row that is not a dict is ignored.
"""
from numbers import Real
from typing import Any

UNKNOWN = "not profiled yet"

# One test per prerequisite, plus "reason" (a format template filled from the prerequisite's own keys):
#   {"fact": key-or-tuple, "min": n}       a number fact at least n (a tuple walks nested facts).
#   {"fact": key, "is": True}              a bool fact that must be True; False reads "(off)".
#   {"ratio": check_key, "of": fact, "below": x, "flagged": phrase}
#                                          open rows of check_key, summed, over the fact "of", strictly below x.
#   {"check": key?, "severity": s?}        no open health row with that key and/or severity.
GATES: dict[str, list[dict[str, Any]]] = {
    "forecast": [
        {"fact": ("history_days", "stock.move"), "min": 90, "reason": "{min} days of stock history"},
    ],
    "expiry": [
        {"fact": "expiry_in_use", "is": True, "reason": "expiry dates on lots"},
    ],
    "replenishment": [
        {"ratio": "product_no_supplier", "of": "storable_products", "below": 0.5,
         "flagged": "without a supplier", "reason": "a supplier on more than half of storable products"},
        {"severity": "blocking", "reason": "no blocking health issues"},
    ],
    "transfers": [
        {"fact": "warehouse_count", "min": 2, "reason": "{min} or more warehouses"},
    ],
    "pos_demand": [
        {"fact": "pos_in_use", "is": True, "reason": "point of sale orders"},
    ],
    "valuation": [
        {"check": "reconcile_stock_value", "severity": "warning",
         "reason": "stock value reconciled to valuation layers"},
    ],
}


def _path(key: str | tuple) -> tuple:
    return key if isinstance(key, tuple) else (key,)


def _lookup(facts: dict, path: tuple) -> Any:
    node: Any = facts
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _number(value: Any) -> float | None:
    """The value as a number. Bools, strings and nulls are not numbers."""
    return value if isinstance(value, Real) and not isinstance(value, bool) else None


def _open_rows(checks: list | tuple, prereq: dict) -> list[dict]:
    """The health rows matching the prereq's check key and/or severity. Rows exist only where there are findings."""
    rows = []
    for row in checks:
        if not isinstance(row, dict):
            continue
        if prereq.get("check") is not None and row.get("check_key") != prereq["check"]:
            continue
        if prereq.get("severity") is not None and row.get("severity") != prereq["severity"]:
            continue
        rows.append(row)
    return rows


def _unmet(prereq: dict, facts: dict, checks: list | tuple | None) -> str | None:
    """Why this prerequisite is not met, in plain English. None when it is met."""
    reason = prereq["reason"].format(**prereq)
    unknown = f"{reason} ({UNKNOWN})"
    if "ratio" in prereq:
        total = _number(_lookup(facts, _path(prereq["of"])))
        if checks is None or total is None:
            return unknown
        flagged = [_number(row.get("count")) for row in _open_rows(checks, {"check": prereq["ratio"]})]
        if None in flagged:
            return unknown
        count = sum(flagged)
        if total <= 0:
            return f"{reason} (nothing to compare)"
        if count / total < prereq["below"]:
            return None
        return f"{reason} ({count} of {total} {prereq['flagged']})"
    if "fact" in prereq:
        have = _lookup(facts, _path(prereq["fact"]))
        if "min" in prereq:
            number = _number(have)
            if number is None:
                return unknown
            return None if number >= prereq["min"] else f"{reason} (have {have})"
        if have is None or type(have) is not type(prereq["is"]):
            return unknown
        return None if have == prereq["is"] else f"{reason} (off)"
    if checks is None:
        return unknown
    rows = _open_rows(checks, prereq)
    if not rows:
        return None
    keys = ", ".join(sorted({str(row.get("check_key")) for row in rows}))
    return f"{reason} (open: {keys})"


def gate(feature: str, facts: Any, checks: Any) -> dict | None:
    """None when the feature is ready. Otherwise {"insufficient_data": True, "feature", "missing": [reason, ...]}."""
    if not isinstance(feature, str) or feature not in GATES:
        return {"insufficient_data": True, "feature": feature, "missing": ["unknown feature"]}
    facts = facts if isinstance(facts, dict) else {}
    checks = checks if isinstance(checks, (list, tuple)) else None
    missing = [reason for reason in (_unmet(p, facts, checks) for p in GATES[feature]) if reason]
    if not missing:
        return None
    return {"insufficient_data": True, "feature": feature, "missing": missing}


def readiness(facts: Any, checks: Any) -> dict[str, dict | None]:
    """gate() for every feature in GATES, keyed by feature name."""
    return {feature: gate(feature, facts, checks) for feature in GATES}

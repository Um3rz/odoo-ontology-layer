"""Plan 3.11 (querygen half): score the Onto question bank against the chat endpoint.

    python evals/onto/run_bank.py --dry-run
    python evals/onto/run_bank.py --rpm 20 --client pkg.module:factory

--dry-run validates question_bank.yaml (shape, ids, paraphrase links, metric keys checked against
app/ontology/templates) and calls nothing. A live run calls the chat endpoint that 3.5 adds, through
--client: a factory returning an object with ask(question, persona) -> ChatAnswer. No route is
hard-coded here. Truth SQL runs read-only on the seed database whose URL is in the environment variable
named by --seed-env. Nothing is written to any database.

Scores follow G3: metric pick, numbers within tolerance, refusals, access leaks, paraphrase consistency,
p95 latency. A paraphrase is scored against its base's truth and metric, not against the base's answer.
Questions in `questions:` (the 1.10 seed mapping) keep their numbers in tests/onto/ground_truth, so their
numeric check is not run here.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import yaml

ROOT = Path(__file__).resolve().parents[2]
BANK = Path(__file__).with_name("question_bank.yaml")
TEMPLATES = ROOT / "app" / "ontology" / "templates"
CATEGORIES = {"onto_seed", "in_scope", "governed", "access", "paraphrase", "out_of_scope", "exploration", "brd30"}
SETUPS = {"approve_overstock"}


@dataclass
class ChatAnswer:
    """What the chat endpoint returned for one question, reduced to what the scorer reads."""

    metric: str | None = None  # ontology metric key the answer was built on
    refused: bool = False
    total: float | None = None  # the one figure, for scalar truths
    rows: dict[str, float] = field(default_factory=dict)  # key -> figure, for row truths


class ChatClient(Protocol):
    def ask(self, question: str, persona: str) -> ChatAnswer: ...


class Throttle:
    """At least 60 / rpm seconds between calls to the model provider."""

    def __init__(self, rpm: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if rpm <= 0:
            raise ValueError("rpm must be positive")
        self.gap, self.clock, self.sleep, self._next = 60.0 / rpm, clock, sleep, 0.0

    def wait(self) -> None:
        now = self.clock()
        if now < self._next:
            self.sleep(self._next - now)
        self._next = self.clock() + self.gap


def load(path: Path = BANK) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def entries(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The seed mapping (`questions`, plan 1.10) and the chat bank (`bank`), in one shape."""
    defaults = doc.get("defaults") or {}
    out: list[dict[str, Any]] = []
    for q in doc.get("questions") or []:
        out.append({
            "id": q["id"], "question": q["question"], "source": "questions",
            "category": q.get("category", defaults.get("category")),
            "persona": q.get("persona", defaults.get("persona")),
            "expected_metric": (q.get("expected") or {}).get("metric"),
            "expected_refusal": q.get("expected_refusal", defaults.get("expected_refusal", False)),
            "truth_ref": q.get("expected_number_source"),
        })
    for q in doc.get("bank") or []:
        out.append({"expected_refusal": False, "expected_metric": None, "source": "bank", **q})
    return out


def ontology_metric_keys(root: Path = TEMPLATES) -> set[str]:
    keys: set[str] = set()
    for path in root.glob("*.yaml"):
        for metric in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("metrics") or []:
            keys.add(metric["key"])
    return keys


def truth_of(e: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> str | None:
    """A paraphrase has no truth of its own: it uses its base's."""
    if e.get("paraphrase_of"):
        return (by_id.get(e["paraphrase_of"]) or {}).get("truth")
    return e.get("truth")


def validate(doc: dict[str, Any], metric_keys: set[str]) -> list[str]:
    errors: list[str] = []
    items = entries(doc)
    by_id = {e["id"]: e for e in items}
    if len(by_id) != len(items):
        errors.append("duplicate question ids")
    truths = doc.get("truths") or {}
    personas = doc.get("personas") or {}
    branches = set(doc.get("branch_codes") or [])
    for name, spec in truths.items():
        if spec.get("kind") not in ("rows", "scalar") or not str(spec.get("sql", "")).strip():
            errors.append(f"truth {name}: needs kind (rows|scalar) and sql")
        if spec.get("mode", "full") not in ("full", "top", "positive_keys"):
            errors.append(f"truth {name}: unknown mode {spec.get('mode')!r}")
    for name, persona in personas.items():
        if not set(persona.get("allowed_branches") or []) <= branches:
            errors.append(f"persona {name}: allowed_branches must be listed in branch_codes")
    for e in items:
        i, cat, metric = e["id"], e["category"], e["expected_metric"]
        if cat not in CATEGORIES:
            errors.append(f"{i}: unknown category {cat!r}")
        if e["persona"] not in personas:
            errors.append(f"{i}: unknown persona {e['persona']!r}")
        if metric is not None and metric not in metric_keys:
            errors.append(f"{i}: {metric!r} is not an ontology metric key")
        for alt in e.get("alt_metrics") or []:
            if alt not in metric_keys:
                errors.append(f"{i}: alt metric {alt!r} is not an ontology metric key")
        if e["source"] == "questions":
            if not e.get("truth_ref"):
                errors.append(f"{i}: missing expected_number_source")
            continue
        truth = e.get("truth")
        if truth is not None and truth not in truths:
            errors.append(f"{i}: truth {truth!r} is not in truths")
        if e.get("setup") and e["setup"] not in SETUPS:
            errors.append(f"{i}: unknown setup {e['setup']!r}")
        if e.get("paraphrase_of") or cat == "paraphrase":
            base = by_id.get(e.get("paraphrase_of") or "")
            if cat != "paraphrase" or base is None or base["source"] != "bank" or base.get("paraphrase_of"):
                errors.append(f"{i}: a paraphrase needs category paraphrase and paraphrase_of a bank question")
            elif truth is not None or metric != base["expected_metric"] or e["persona"] != base["persona"]:
                errors.append(f"{i}: a paraphrase has no truth, and must share its base's metric and persona")
            elif truth_of(base, by_id) is None and not base["expected_refusal"]:
                errors.append(f"{i}: its base {base['id']} has no truth to compare with")
            continue
        if e["expected_refusal"] and truth is not None:
            errors.append(f"{i}: a refusal carries no truth")
        if cat in ("out_of_scope", "governed") and not e["expected_refusal"]:
            errors.append(f"{i}: {cat} must expect a refusal")
        if cat == "exploration" and (e["expected_refusal"] or truth is not None or metric is not None):
            errors.append(f"{i}: exploration never refuses and has no truth or metric")
        if cat == "access" and e["persona"] == "admin":
            errors.append(f"{i}: access items use a restricted persona")
        if not e["expected_refusal"] and cat != "exploration" and metric is None and not e.get("gap"):
            errors.append(f"{i}: no expected metric, so give the gap reason")
        if metric is not None and e.get("gap"):
            errors.append(f"{i}: a gap is only for a missing metric")
    return errors


def counts(doc: dict[str, Any]) -> dict[str, Any]:
    items = entries(doc)
    by_id = {e["id"]: e for e in items}
    return {
        "total": len(items),
        "by_category": dict(sorted(Counter(e["category"] for e in items).items())),
        "expected_refusal": sum(1 for e in items if e["expected_refusal"]),
        "metric_scored": sum(1 for e in items if e["expected_metric"]),
        "metric_gap": sum(1 for e in items if e.get("gap")),
        "numeric_scored_truth_sql": sum(1 for e in items if truth_of(e, by_id)),
        "numeric_in_seed_suite": sum(1 for e in items if e.get("truth_ref")),
        "paraphrases": sum(1 for e in items if e.get("paraphrase_of")),
        "setup_skipped": sum(1 for e in items if e.get("setup")),
    }


def match(answer: ChatAnswer, spec: dict[str, Any], want: list[tuple[str, float]]) -> bool:
    """Demo scorer semantics: scalar in tolerance; rows compared by key (full, top N, or the positive keys)."""
    tol = float(spec.get("tolerance", 0.005))
    if spec["kind"] == "scalar":
        return answer.total is not None and abs(answer.total - want[0][1]) <= tol
    wanted, got = dict(want), answer.rows
    mode = spec.get("mode", "full")
    if mode == "positive_keys":
        return {k for k, v in got.items() if v > 0} == set(wanted)
    if mode == "top":
        top = sorted(got, key=lambda k: -got[k])[: len(wanted)]
        return set(wanted) <= set(top) and all(k in got and abs(got[k] - v) <= tol for k, v in wanted.items())
    for k, v in wanted.items():
        if k in got and abs(got[k] - v) > tol:
            return False
        if k not in got and v != 0:
            return False
    return all(abs(v) <= tol for k, v in got.items() if k not in wanted)


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in results if "skipped" not in r and "error" not in r]

    def rate(values):
        values = [v for v in values if v is not None]
        return round(sum(values) / len(values), 4) if values else None

    ms = sorted(r["ms"] for r in scored)
    summary = {
        "answered": len(scored),
        "skipped": sum(1 for r in results if "skipped" in r),
        "errors": sum(1 for r in results if "error" in r),
        "metric_pick_accuracy": rate(r["metric_ok"] for r in scored),
        "numbers_match_truth": rate(r["numeric_ok"] for r in scored),
        "refusal_rate_expected": rate(r["refused"] for r in scored if r["expected_refusal"]),
        "false_refusal_rate": rate(r["refused"] for r in scored if not r["expected_refusal"]),
        "out_of_scope_refused": rate(r["refused"] for r in scored if r["category"] == "out_of_scope"),
        "paraphrase_consistent": rate(r["paraphrase_ok"] for r in scored),
        "access_leaks": sum(1 for r in scored if r["leak"]),
        "latency_p95_ms": ms[min(len(ms) - 1, math.ceil(0.95 * len(ms)) - 1)] if ms else None,
    }
    targets = {  # G3: metric >= 90%, numbers 100%, out-of-scope refused >= 95%, paraphrases >= 95%, leaks 0
        "metric_pick_accuracy>=0.90": summary["metric_pick_accuracy"] is None or summary["metric_pick_accuracy"] >= 0.90,
        "numbers_match_truth==1.0": summary["numbers_match_truth"] is None or summary["numbers_match_truth"] == 1.0,
        "out_of_scope_refused>=0.95": summary["out_of_scope_refused"] is None or summary["out_of_scope_refused"] >= 0.95,
        "paraphrase_consistent>=0.95": summary["paraphrase_consistent"] is None or summary["paraphrase_consistent"] >= 0.95,
        "access_leaks==0": summary["access_leaks"] == 0,
    }
    return {**summary, "targets_met": targets}


def run_bank(doc: dict[str, Any], client: ChatClient, throttle: Throttle,
             execute: Callable[[str], list[tuple]]) -> dict[str, Any]:
    items = entries(doc)
    by_id = {e["id"]: e for e in items}
    truths, personas = doc.get("truths") or {}, doc.get("personas") or {}
    branches = set(doc.get("branch_codes") or [])
    cache: dict[str, list[tuple[str, float]]] = {}

    def want(name: str) -> list[tuple[str, float]]:
        if name not in cache:
            spec, rows = truths[name], execute(truths[name]["sql"])
            if spec["kind"] == "scalar":
                cache[name] = [("", float(rows[0][0]) if rows else 0.0)]
            else:
                cache[name] = [(str(r[0]), float(r[1])) for r in rows]
        return cache[name]

    results: list[dict[str, Any]] = []
    for e in items:
        rec: dict[str, Any] = {"id": e["id"], "category": e["category"], "expected_refusal": e["expected_refusal"]}
        if e.get("setup"):
            results.append({**rec, "skipped": f"needs setup {e['setup']}, not wired in this runner"})
            continue
        throttle.wait()
        start = time.monotonic()
        try:
            answer = client.ask(e["question"], e["persona"])
        except Exception as exc:  # one failed call is counted, not allowed to stop the run
            results.append({**rec, "error": type(exc).__name__})
            continue
        rec["ms"] = round((time.monotonic() - start) * 1000, 1)
        rec["refused"] = answer.refused
        rec["metric_ok"] = None if e["expected_metric"] is None else (
            answer.metric in {e["expected_metric"], *(e.get("alt_metrics") or [])})
        name = truth_of(e, by_id)
        rec["numeric_ok"] = match(answer, truths[name], want(name)) if name else None
        rec["refusal_ok"] = answer.refused == e["expected_refusal"]
        allowed = set((personas.get(e["persona"]) or {}).get("allowed_branches") or [])
        rec["leak"] = bool(e["expected_refusal"] and (answer.total is not None or answer.rows)) or any(
            k in branches - allowed for k in answer.rows)
        if e.get("paraphrase_of"):
            rec["paraphrase_ok"] = (rec["metric_ok"] is not False and rec["numeric_ok"] is not False
                                    and rec["refusal_ok"] and not rec["leak"])
        results.append(rec)
    return {"summary": summarize(results), "results": results}


def sql_executor(url: str) -> Callable[[str], list[tuple]]:
    from sqlalchemy import create_engine, text

    engine = create_engine(url)

    def execute(sql: str) -> list[tuple]:
        with engine.connect().execution_options(postgresql_readonly=True) as conn:
            return [tuple(row) for row in conn.execute(text(sql)).fetchall()]

    return execute


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bank", type=Path, default=BANK)
    ap.add_argument("--dry-run", action="store_true", help="validate the YAML only; calls nothing")
    ap.add_argument("--rpm", type=float, default=20.0, help="requests per minute to the chat endpoint")
    ap.add_argument("--client", help="module:factory returning a ChatClient (the 3.5 chat route)")
    ap.add_argument("--seed-env", default="EVAL_SEED_DATABASE_URL", help="env var with the seed database URL")
    ap.add_argument("--out", type=Path, help="also write the JSON report here")
    args = ap.parse_args(argv)

    doc = load(args.bank)
    errors = validate(doc, ontology_metric_keys())
    if errors:
        print(json.dumps({"valid": False, "errors": errors}, indent=2))
        return 1
    if args.dry_run:
        print(json.dumps({"valid": True, "counts": counts(doc)}, indent=2))
        return 0
    if not args.client:
        ap.error("a live run needs --client module:factory (the chat client); --dry-run validates only")
    url = os.environ.get(args.seed_env)
    if not url:
        ap.error(f"set {args.seed_env} to the seed database URL (SQLAlchemy form) for the truth SQL")
    module, _, attr = args.client.partition(":")
    client = getattr(importlib.import_module(module), attr)()
    report = run_bank(doc, client, Throttle(args.rpm), sql_executor(url))
    report["counts"] = counts(doc)
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

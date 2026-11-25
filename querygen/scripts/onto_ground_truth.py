"""Run the Onto ground-truth suite (plan 1.10) against the seeded odoo_onto database and render the report.

The database URL is built in this process from odoo.conf and handed to pytest through the environment. It is never
printed, logged or written to a file: all output is scrubbed of the password and the URL before it is shown or saved.

    python scripts/onto_ground_truth.py [--conf PATH] [--out DIR] [--budget SECONDS]

Writes DIR/ground_truth_results.json (the suite's raw results) and DIR/ground_truth_report.md.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
SUITE = "tests/onto/ground_truth"
DATABASE = "odoo_onto"
DEFAULT_CONF = Path.home() / "Documents" / "odoo16" / "odoo.conf"
# The suite opens no engine. app.db only asserts that a URL is set, so the dev database is never reached this way.
PLACEHOLDER_URL = "postgresql+psycopg2://ground_truth@127.0.0.1:1/unused"
DEFAULT_BUDGET_S = 600.0
STATUSES = ("pass", "fail", "divergence", "unexpected_pass", "refused", "gap")
TEMPLATES = ("org", "core", "inventory", "purchase", "pos", "sales", "account", "crm")
NOT_RUN = [
    "The eval bank web-app/src/demo/evals/bank.ts does not exist in the web-app worktree. The cases follow plan 1.10 "
    "(the metric x dimension matrix), and the question bank is mapped to them, not to bank.ts.",
    "The seed_fnb database was not run. Only odoo_onto is in scope for 1.10.",
    "No CI or nightly job runs this suite. The runner is manual.",
    "No runner for the question bank. evals/onto/question_bank.yaml is checked against the cases and gaps only; "
    "no question is answered by the compiler.",
    "No answer is checked end to end through the querygen HTTP routes. The compile step is the same function the "
    "route calls (_final_sql), and no querygen request is executed.",
    "Near-expiry quantity runs as a refusal only: the spec has no params field, so the metric cannot be computed.",
]


def read_connection(conf: Path) -> dict:
    raw = conf.read_text(encoding="utf-8")
    if not raw.lstrip().startswith("["):
        raw = "[options]\n" + raw  # Odoo's conf file has no section header
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(raw)
    options = parser["options"]
    host = options.get("db_host", "").strip()
    port = options.get("db_port", "").strip()
    return {
        "host": "localhost" if host in ("", "False") else host,
        "port": "5432" if port in ("", "False") else port,
        "user": options["db_user"],
        "password": options.get("db_password", ""),
    }


def build_url(conn: dict) -> str:
    user, password = quote(conn["user"], safe=""), quote(conn["password"], safe="")
    return f"postgresql://{user}:{password}@{conn['host']}:{conn['port']}/{DATABASE}"


def scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if not secret:
            continue
        if "://" in secret:  # the full URL: replace every occurrence
            text = text.replace(secret, "<redacted>")
        else:  # a bare password: only as a whole token, so odoo_onto or a short password keeps the rest of the text
            text = re.sub(rf"(?<!\w){re.escape(secret)}(?!\w)", "<redacted>", text)
    return text


def run_suite(url: str, results: Path, budget: float, select: str | None) -> tuple[int, str, float]:
    env = dict(os.environ)
    env["GROUND_TRUTH_DATABASE_URL"] = url
    env["GROUND_TRUTH_RESULTS"] = str(results)
    env["GROUND_TRUTH_BUDGET_S"] = str(budget)
    env["DATABASE_URL"] = PLACEHOLDER_URL
    cmd = [sys.executable, "-m", "pytest", SUITE, "-q", "-p", "no:cacheprovider", "-rfExX", "--tb=short"]
    if select:
        cmd += ["-k", select]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=budget + 300)
        code, output = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        code, output = 124, "the run exceeded its time limit and was stopped"
    return code, output, time.monotonic() - started


# --- report ------------------------------------------------------------------------------------------
def _cell(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")[:300]


def _table(headers: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return out + [""]


def _period(case: dict) -> str:
    period = case.get("period")
    return f"{period[0]} to {period[1]}" if period else "none"


def _binding_path(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def render(payload: dict | None, code: int, summary: str, wall_s: float) -> str:
    lines = ["# Onto 1.10 ground truth: querygen against the seed database", ""]
    if payload is None:
        lines += ["No results were written: the suite did not reach its teardown.",
                  f"pytest exit code {code}: {summary}", ""]
        return "\n".join(lines)
    records = payload["cases"]
    cases = [r for r in records if not r["id"].startswith("view.")]
    views = [r for r in records if r["id"].startswith("view.")]
    counts = Counter(r["status"] for r in cases)
    lines += [
        f"- Database: `{payload['database']}` (Odoo 16 seed), read-only session, statement timeout 120 s.",
        f"- Finished (UTC): {payload['finished_utc']}. Suite time {payload['seconds']} s of a "
        f"{payload['budget_s']:.0f} s budget; wall time {wall_s:.1f} s.",
        f"- pytest exit code {code}: `{summary}`.",
        "- Tolerance by unit: " + ", ".join(f"{k} {v}" for k, v in payload["tolerance"].items()) + ".",
        f"- Cases: {len(cases)}. " + ", ".join(f"{s} {counts.get(s, 0)}" for s in STATUSES)
        + f". Object views: {len(views)} ({sum(1 for v in views if v['status'] == 'pass')} pass).",
        "",
        "## Per template", "",
    ]
    rows = []
    for template in TEMPLATES:
        mine = [r for r in cases if r["template"] == template]
        metrics = {r["metric"] for r in mine if r["metric"]}
        rows.append([template, len(metrics), len(mine)] + [sum(1 for r in mine if r["status"] == s) for s in STATUSES]
                    + [sum(1 for v in views if v["template"] == template and v["status"] == "pass"),
                       sum(1 for v in views if v["template"] == template)])
    lines += _table(["Template", "Metrics with cases", "Cases"] + list(STATUSES) + ["Views pass", "Views"], rows)

    lines += ["## Per metric and dimension", "",
              "Rows are the number of result rows from the compiled SQL and from the hand SQL. Totals are the "
              "single no-dimension value, compiled and hand.", ""]
    rows = [[r["template"], r["object"], r["metric"], r["id"], ", ".join(r["dims"]) or "none", _period(r),
             r["grain"] or "none", r["status"], f"{r['compiled_rows']} / {r['hand_rows']}",
             (r["total"] or {}).get("compiled"), (r["total"] or {}).get("hand"), r["seconds"]] for r in cases]
    lines += _table(["Template", "Object", "Metric", "Case", "Dims", "Period", "Grain", "Status", "Rows c / h",
                     "Total compiled", "Total hand", "Seconds"], rows)

    lines += ["## Object views", "",
              "A compiled object view must return one row per base record, with a unique _onto_id.", ""]
    lines += _table(["Object", "Template", "View rows", "Base rows", "Status", "Detail"],
                    [[v["object"], v["template"], v["compiled_rows"], v["hand_rows"], v["status"], v["detail"]]
                     for v in views])

    bindings: dict[str, Counter] = {}
    for record in cases:
        for value in record.get("bindings", {}).values():
            bindings.setdefault(_binding_path(value), Counter())[record["status"]] += 1
    lines += ["## Per Odoo binding", "",
              "Each Odoo field path that a compiled query reads, with the statuses of the cases that read it.", ""]
    lines += _table(["Odoo binding", "Cases", "Statuses"],
                    [[path, sum(c.values()), ", ".join(f"{s} {n}" for s, n in sorted(c.items()))]
                     for path, c in sorted(bindings.items())])

    lines += ["## Gaps", "", "Metrics that cannot be checked on odoo_onto, with the probe that keeps the limit honest.", ""]
    lines += _table(["Metric", "Object", "Kind", "Probe case", "Reason"],
                    [[g["metric"], g["object"], g["kind"], g["probe"], g["reason"]] for g in payload["gaps"]])

    findings = payload["findings"]
    bugs = [f for f in findings if f["kind"] == "bug"]
    lines += ["## Findings", "",
              f"Compiler or template bugs: {len(bugs)} found, {sum(1 for f in bugs if f['status'] == 'fixed')} fixed "
              "in this change. Other findings are open decisions, not changed here.", ""]
    lines += _table(["ID", "Kind", "Status", "Metric", "Summary"],
                    [[f["id"], f["kind"], f["status"], f["metric"], f["summary"]] for f in findings])

    problems = [r for r in cases if r["status"] in ("fail", "unexpected_pass", "divergence")]
    lines += ["## Failures and divergences", ""]
    if not problems:
        lines += ["None.", ""]
    for record in problems:
        lines += [f"- `{record['id']}` ({record['status']}): {record['detail'][:600]}"]
    lines += [""]

    slowest = sorted(records, key=lambda r: r.get("seconds") or 0, reverse=True)[:5]
    lines += ["## Timing", "",
              f"Suite {payload['seconds']} s, budget {payload['budget_s']:.0f} s, wall {wall_s:.1f} s. Slowest cases:", ""]
    lines += _table(["Case", "Seconds"], [[r["id"], r.get("seconds")] for r in slowest])

    lines += ["## Not run", ""] + [f"- {item}" for item in NOT_RUN] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Onto ground-truth suite and render its report.")
    parser.add_argument("--conf", type=Path, default=DEFAULT_CONF, help="Odoo config with the db_* settings")
    parser.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "onto_ground_truth")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_S, help="seconds for the whole run")
    parser.add_argument("--select", default=None, help="a pytest -k expression, to run part of the suite")
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    results = args.out / "ground_truth_results.json"
    report = args.out / "ground_truth_report.md"
    results.unlink(missing_ok=True)

    conn = read_connection(args.conf)
    url = build_url(conn)
    secrets = [conn["password"], quote(conn["password"], safe=""), url]
    code, output, wall = run_suite(url, results, args.budget, args.select)
    lines = [line for line in scrub(output, secrets).splitlines() if line.strip()]
    summary = lines[-1] if lines else "no pytest output"

    payload = json.loads(results.read_text(encoding="utf-8")) if results.exists() else None
    text = scrub(render(payload, code, summary, wall), secrets)
    report.write_text(text, encoding="utf-8")

    print(f"database {DATABASE}: pytest exit {code}; {summary}")
    for line in lines:
        if line.startswith(("FAILED", "ERROR")):
            print(line[:400])
    if payload is not None:
        statuses = Counter(r["status"] for r in payload["cases"] if not r["id"].startswith("view."))
        print("cases: " + ", ".join(f"{s} {statuses.get(s, 0)}" for s in STATUSES))
    print(f"report: {report}")
    return code


if __name__ == "__main__":
    sys.exit(main())

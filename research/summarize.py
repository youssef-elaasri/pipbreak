"""Summarize mining results: per-agent totals and every finding on an added line, for triage.

    python research/summarize.py [--markdown out.md]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def load() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(RESULTS.glob("*.json"))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    records = load()

    statuses = Counter(r["status"] for r in records)
    checked = [r for r in records if r["status"] == "ok"]
    by_agent = defaultdict(lambda: Counter())
    for r in checked:
        a = by_agent[r["agent"]]
        a["prs"] += 1
        a["lines"] += r["added_lines"]
        a["prs_with_findings"] += bool(r["findings"])
        for f in r["findings"]:
            a[f["severity"]] += 1
        a["install_failed"] += bool(r["install"]["failed"] or r["install"]["skipped"])

    out = [f"Statuses: {dict(statuses)}", "",
           "| Agent | PRs checked | Added lines | PRs with findings | Errors | Warnings | Partial installs |",
           "|---|---|---|---|---|---|---|"]
    for agent, a in sorted(by_agent.items()):
        out.append(f"| {agent} | {a['prs']} | {a['lines']} | {a['prs_with_findings']} | {a['error']} | "
                   f"{a['warning']} | {a['install_failed']} |")
    out += ["", "## Findings on added lines", ""]
    for r in checked:
        for f in r["findings"]:
            out.append(f"- **{r['agent']}** [{r['repo']}#{r['number']}]({r['url']}) `{f['path']}:{f['line']}` "
                       f"{f['severity']} {f['code']}: {f['message']}  \n  `{f['source'][:160]}`")
    problems = [r for r in records if r["status"] not in ("ok", "no-python-changes")]
    if problems:
        out += ["", "## Not checked", ""]
        out += [f"- {r['repo']}#{r['number']}: {r['status']} {r.get('error', '')[:200]}" for r in problems]
    text = "\n".join(out)
    print(text)
    if args.markdown:
        args.markdown.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

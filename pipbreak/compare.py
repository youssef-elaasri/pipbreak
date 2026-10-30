"""What does an update break? Compare two environments - before and after a dependency bump.

    python -m pipbreak.compare --base venv-base/bin/python --head venv-head/bin/python \
        --markdown report.md

Finds the distributions whose version changed, runs `pipbreak --deps --uses <changed>` in both
environments, and reports only the findings the update introduced: calls that installed packages
make into the updated ones and that no longer exist. Exit code 1 when the update breaks something.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .checker import canonical

MARKER = "<!-- pipbreak -->"
PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)
VERSIONS = re.compile(r"==[\w.+!-]+")


def run(python: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [PACKAGE_ROOT, env.get("PYTHONPATH")]))
    return subprocess.run([python, *args], capture_output=True, text=True, encoding="utf-8", env=env)


def installed_versions(python: str) -> dict[str, tuple[str, str]]:
    """{canonical name: (display name, version)} for everything installed in that environment."""
    result = run(python, "-c", "import importlib.metadata as m, json; print(json.dumps("
                               "[[d.metadata['Name'], d.version] for d in m.distributions()]))")
    if result.returncode:
        raise RuntimeError(f"could not list packages with {python}: {result.stderr.strip()}")
    versions = {}
    for name, version in json.loads(result.stdout):
        if name:
            versions.setdefault(canonical(name), (name, version))  # first on sys.path wins
    return versions


def changed_distributions(base: dict, head: dict) -> list[tuple[str, str | None, str | None]]:
    """(name, old version, new version) for each distribution added, removed or re-versioned."""
    changes = []
    for key in sorted(base.keys() | head.keys()):
        old, new = base.get(key), head.get(key)
        if (old and old[1]) != (new and new[1]):
            changes.append(((new or old)[0], old and old[1], new and new[1]))
    return changes


def scan(python: str, uses: list[str]) -> list[dict]:
    args = ["-m", "pipbreak", "--deps", "--format", "json"]
    for name in uses:
        args += ["--uses", name]
    result = run(python, *args)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"pipbreak failed with {python}: {result.stderr.strip()[-2000:]}")
    return json.loads(result.stdout)


def key(finding: dict) -> tuple:
    # Line numbers and versions move when the dependent package itself is re-versioned.
    return finding["path"], finding["code"], VERSIONS.sub("", finding["message"])


def introduced(base: list[dict], head: list[dict]) -> list[dict]:
    before = {key(f) for f in base}
    return [f for f in head if key(f) not in before]


def split_message(message: str) -> tuple[str, str]:
    """'Flask==2.2.5: <problem>' -> ('Flask 2.2.5', '<problem>')."""
    package, sep, problem = message.partition(": ")
    if not sep:
        return "", message
    return package.replace("==", " "), problem


def markdown(changes: list[tuple], new: list[dict]) -> str:
    errors = [f for f in new if f["severity"] == "error"]
    warnings = [f for f in new if f["severity"] != "error"]
    lines = [MARKER]
    if errors:
        lines.append(f"### pipbreak: this update breaks {len(errors)} call(s) in installed packages")
    elif warnings:
        lines.append("### pipbreak: no breaking calls, but this update deprecates "
                     f"{len(warnings)} API(s) installed packages use")
    else:
        lines.append("### pipbreak: no installed package uses an API this update removed")
    lines.append("")
    updated = [f"{name} {old or '(new)'} → {new or '(removed)'}" for name, old, new in changes]
    lines.append("Changed: " + (", ".join(updated) if updated else "nothing"))

    def table(findings):
        rows = ["", "| Package | Where | Problem |", "|---|---|---|"]
        for f in findings:
            package, problem = split_message(f["message"])
            rows.append(f"| {package} | `{f['path']}:{f['line']}` | {problem.replace('|', '¦')} |")
        return rows

    if errors:
        lines += table(errors)
        lines += ["", "These packages accept the new version in their metadata but call APIs it no "
                      "longer has, so pip installs them without complaint and they fail at runtime. "
                      "Upgrade them too, or hold this update back."]
    if warnings:
        lines += ["", f"<details><summary>{len(warnings)} deprecation warning(s)</summary>"]
        lines += table(warnings)
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipbreak.compare", description=__doc__.split("\n")[0])
    parser.add_argument("--base", required=True, help="interpreter of the environment before the update")
    parser.add_argument("--head", required=True, help="interpreter of the environment after the update")
    parser.add_argument("--markdown", help="also write the report as Markdown to this file")
    parser.add_argument("--strict", action="store_true", help="exit non-zero on new deprecations too")
    args = parser.parse_args(argv)

    changes = changed_distributions(installed_versions(args.base), installed_versions(args.head))
    updated = [name for name, _, new in changes if new]
    new = introduced(scan(args.base, updated), scan(args.head, updated)) if updated else []

    for name, old, version in changes:
        print(f"changed: {name} {old or '(new)'} -> {version or '(removed)'}")
    for f in new:
        print(f"{f['path']}:{f['line']}:{f['col']}: {f['severity']} {f['code']} {f['message']}")
    errors = sum(f["severity"] == "error" for f in new)
    print(f"\n{errors} new error(s), {len(new) - errors} new warning(s) from this update.")
    if args.markdown:
        Path(args.markdown).write_text(markdown(changes, new), encoding="utf-8")
    return 1 if errors or (args.strict and new) else 0


if __name__ == "__main__":
    sys.exit(main())

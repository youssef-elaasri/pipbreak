"""Does pipbreak find code that a major dependency bump breaks - and does CI catch it?

For each Dependabot/Renovate PR that bumps a library across a major version, install the repo's
dependencies twice (before and after the bump), run pipbreak over the whole repo against each, and
keep the findings that are new after the bump and come from the bumped library. Then compare with
the PR's CI result.

    python research/upgrades.py collect --per-lib 6      # host: pick PRs  -> upgrades.json
    upgrades.py fetch                                    # container, online:  clone + 2 installs
    upgrades.py check                                    # container, offline: 2 runs + diff -> results-upgrades/

Same safety model as mine.py: nothing from a repo is executed, wheels only, checker kept off sys.path.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from mine import HERE, declared_dependencies, gh_json, local_packages

SAMPLE = Path(os.environ.get("PIPBREAK_SAMPLE", HERE / "upgrades.json"))
WORK = Path(os.environ.get("PIPBREAK_WORK", HERE / "work"))
RESULTS = Path(os.environ.get("PIPBREAK_RESULTS", HERE / "results-upgrades"))

# (distribution, import name, old major as Dependabot writes it, new major for Renovate titles)
LIBRARIES = [
    ("pydantic", "pydantic", "1.", "2"),
    ("sqlalchemy", "sqlalchemy", "1.", "2"),
    ("numpy", "numpy", "1.", "2"),
    ("marshmallow", "marshmallow", "3.", "4"),
    ("httpx", "httpx", "0.27", "0.28"),
    ("openai", "openai", "0.", "1"),
    ("werkzeug", "werkzeug", "2.", "3"),
    ("pandas", "pandas", "1.", "2"),
]
MIN_STARS = 10
MAX_REPO_KB = 300_000
DEPENDENCY_FILES = re.compile(r"(^|/)(pyproject\.toml|setup\.py|setup\.cfg|requirements[^/]*\.(txt|in))$")
BUMP_TITLE = re.compile(r"[Bb]ump (?P<dist>[\w.-]+) from (?P<old>[\w.]+) to (?P<new>[\w.]+)")
RENOVATE_TITLE = re.compile(r"[Uu]pdate dependency (?P<dist>[\w.-]+) to v?(?P<new>[\w.]+)")


def slug(pr: dict) -> str:
    return f"up__{pr['repo'].replace('/', '__')}__{pr['number']}"


# -- host: collect ------------------------------------------------------------------------------

def ci_summary(rollup: list[dict]) -> str:
    conclusions = {(c.get("conclusion") or c.get("state") or "").upper() for c in rollup}
    if not rollup:
        return "none"
    if conclusions & {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
        return "failure"
    if conclusions <= {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        return "success"
    return "pending"


def collect(per_lib: int):
    repos: dict[str, dict | None] = {}

    def repo_info(repo: str):
        if repo not in repos:
            try:
                repos[repo] = gh_json("api", f"repos/{repo}")
            except RuntimeError:
                repos[repo] = None
        return repos[repo]

    sample = []
    for dist, import_name, old_major, new_major in LIBRARIES:
        found = []
        for query in ([f'"Bump {dist} from {old_major}"', "--author=app/dependabot"],
                      [f'"Update dependency {dist} to v{new_major}"', "--author=app/renovate"]):
            time.sleep(10)  # the search API's burst limit is strict
            found += gh_json("search", "prs", *query, "--sort=created", "--limit=100",
                             "--json=repository,number,title,url")
        picked = 0
        for pr in found:
            repo = pr["repository"]["nameWithOwner"]
            info = repo_info(repo)
            if (not info or info.get("archived") or info.get("stargazers_count", 0) < MIN_STARS
                    or info.get("size", 0) > MAX_REPO_KB or any(s["repo"] == repo for s in sample)):
                continue
            m = BUMP_TITLE.search(pr["title"]) or RENOVATE_TITLE.search(pr["title"])
            if not m or m["dist"].lower() != dist:
                continue
            view = gh_json("pr", "view", str(pr["number"]), "-R", repo,
                           "--json=state,mergedAt,headRefOid,statusCheckRollup,files,createdAt")
            dep_files = [f["path"] for f in view["files"] if DEPENDENCY_FILES.search(f["path"])]
            if not dep_files:
                continue
            sample.append({
                "lib": dist, "import": import_name, "repo": repo, "number": pr["number"], "url": pr["url"],
                "title": pr["title"], "old": m.groupdict().get("old"), "new": m["new"],
                "state": view["state"], "merged": bool(view["mergedAt"]), "created": view["createdAt"],
                "ci": ci_summary(view["statusCheckRollup"]), "head": view["headRefOid"],
                "dependency_files": dep_files, "stars": info["stargazers_count"],
            })
            picked += 1
            if picked >= per_lib:
                break
        print(f"{dist}: {len(found)} bump PRs found, {picked} picked", flush=True)
    SAMPLE.write_text(json.dumps(sample, indent=2), encoding="utf-8")
    print(f"wrote {len(sample)} PRs to {SAMPLE}")


# -- container, online: fetch -------------------------------------------------------------------

def git(path: Path, *args: str, timeout: int = 900):
    subprocess.run(["git", "-C", str(path), "-c", "core.hooksPath=/dev/null", *args],
                   check=True, timeout=timeout, capture_output=True)


def checkout(pr: dict) -> tuple[Path, Path]:
    """The PR head (after the bump) and its parent (before), which share all code but the pins."""
    base = WORK / slug(pr)
    new, old = base / "new", base / "old"
    if not (new / ".git").exists():
        new.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(new)], check=True)
        git(new, "fetch", "-q", "--depth=2", f"https://github.com/{pr['repo']}.git", f"pull/{pr['number']}/head")
        git(new, "checkout", "-q", "FETCH_HEAD")
    if not old.exists():
        git(new, "worktree", "add", "-q", "--detach", str(old), "HEAD^")
    return new, old


def dependencies(tree: Path, pr: dict) -> list[str]:
    """Root dependencies plus those declared next to each dependency file the bump touched."""
    deps, _ = declared_dependencies(tree)
    for path in pr["dependency_files"]:
        directory = (tree / path).parent
        if directory != tree:
            deps += declared_dependencies(directory)[0]
    return list(dict.fromkeys(deps))


def installed_version(target: Path, dist: str) -> str | None:
    normalized = dist.lower().replace("-", "_")
    for info in target.glob("*.dist-info"):
        name, _, version = info.name[: -len(".dist-info")].rpartition("-")
        if name.lower().replace("-", "_") == normalized:
            return version
    return None


def install(deps: list[str], target: Path) -> list[str]:
    target.mkdir(parents=True, exist_ok=True)
    pip = [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--no-cache-dir",
           "--only-binary=:all:", "--target", str(target)]
    failed = []
    if deps and subprocess.run([*pip, *deps], capture_output=True, text=True, timeout=1800).returncode:
        # One package without a wheel sinks the batch, so install one at a time - but under the
        # exact pins as constraints, or a later package drags in the newest version of the
        # library being compared (e.g. alembic pulling SQLAlchemy 2 into the "before" env).
        constraints = target.parent / f"{target.name}-constraints.txt"
        constraints.write_text("\n".join(d for d in deps if "==" in d) + "\n", encoding="utf-8")
        for d in deps:
            cmd = [*pip, "--upgrade", "-c", str(constraints), d]
            if subprocess.run(cmd, capture_output=True, text=True, timeout=900).returncode:
                failed.append(d)
    return failed


def fetch(pr: dict) -> dict:
    status_file = WORK / slug(pr) / "fetch.json"
    if status_file.exists():
        return json.loads(status_file.read_text(encoding="utf-8"))
    status: dict = {"status": "ok"}
    try:
        new, old = checkout(pr)
        for side, tree in (("old", old), ("new", new)):
            deps = dependencies(tree, pr)
            target = WORK / slug(pr) / f"deps-{side}"
            status[side] = {"failed": install(deps, target), "declared": len(deps),
                            "version": installed_version(target, pr["lib"])}
        if not status["old"]["version"] or status["old"]["version"] == status["new"]["version"]:
            status["status"] = "bump-not-reproduced"
    except Exception as exc:
        status = {"status": "fetch-error", "error": str(exc)[-800:]}
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(status), encoding="utf-8")
    return status


# -- container, offline: check ------------------------------------------------------------------

VERSION = re.compile(r"==[\w.+!-]+|Python \d+\.\d+")


def run_pipbreak(tree: Path, deps: Path, local: bool = True) -> list[dict]:
    cmd = [sys.executable, "-I", str(HERE / "run_check.py"), str(deps), "--format", "json"]
    for name in sorted(local_packages(tree)) if local else ():
        cmd += ["--local", name]
    result = subprocess.run([*cmd, str(tree)], capture_output=True, text=True, encoding="utf-8",
                            timeout=3600, cwd=HERE)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"pipbreak failed: {result.stderr.strip()[-800:]}")


def introduced_findings(pr: dict, tree: Path, local: bool = True) -> tuple[list[dict], int, int]:
    """Check `tree` against the old and the new dependencies; keep what's new and from the library."""
    deps = WORK / slug(pr)
    before = run_pipbreak(tree, deps / "deps-old", local)
    after = run_pipbreak(tree, deps / "deps-new", local)

    def key(f):
        return f["path"], f["line"], f["code"], VERSION.sub("", f["message"])

    seen_before = {key(f) for f in before}
    names = (pr["lib"].lower(), pr["import"].lower())
    introduced = []
    for f in after:
        if key(f) in seen_before or not any(n in f["message"].lower() for n in names):
            continue
        path = Path(f["path"])
        rel = path.resolve().relative_to(tree.parent.resolve()).as_posix()
        source = path.read_text(encoding="utf-8", errors="replace").splitlines()[f["line"] - 1].strip()
        introduced.append(dict(f, path=rel, source=source))
    return introduced, len(before), len(after)


def dependents(pr: dict) -> list[Path]:
    """Installed packages (other than the library) whose code imports the bumped library.

    Their code is fixed - the same version sits in both environments - but whether it still works
    depends on the library. A package that pins nothing (Flask 2.2: Werkzeug>=2.2.2) breaks at
    runtime and pip never notices.
    """
    deps = WORK / slug(pr) / "deps-new"
    uses = re.compile(rf"^\s*(import|from)\s+{re.escape(pr['import'])}\b", re.M)
    found = []
    for package in sorted(deps.iterdir()):
        if (not package.is_dir() or package.name == pr["import"] or "." in package.name
                or package.name.startswith("_") or package.name == "bin"):
            continue
        if any(uses.search(f.read_text(encoding="utf-8", errors="replace")) for f in package.rglob("*.py")):
            found.append(package)
    return found


def check(pr: dict) -> dict:
    record = dict(pr)
    status_file = WORK / slug(pr) / "fetch.json"
    if not status_file.exists():
        return dict(record, status="not-fetched")
    record.update(json.loads(status_file.read_text(encoding="utf-8")))
    if record["status"] != "ok":
        return record
    try:
        introduced, before, after = introduced_findings(pr, WORK / slug(pr) / "new")
        in_dependencies = []
        for package in dependents(pr):
            in_dependencies += introduced_findings(pr, package, local=False)[0]
    except Exception as exc:
        return dict(record, status="check-error", error=str(exc)[-800:])
    record["findings_before"] = before
    record["findings_after"] = after
    record["introduced"] = introduced
    record["introduced_in_dependencies"] = in_dependencies
    return record


def run_stage(stage: str, only: str | None, limit: int | None):
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    if only:
        sample = [pr for pr in sample if f"{pr['repo']}#{pr['number']}" == only]
    if stage == "check":
        RESULTS.mkdir(parents=True, exist_ok=True)
    for pr in sample[:limit]:
        name = f"{pr['lib']:12} {pr['repo']}#{pr['number']}"
        if stage == "fetch":
            status = fetch(pr)
            versions = f"{status.get('old', {}).get('version')} -> {status.get('new', {}).get('version')}"
            print(f"{name}: {status['status']} ({versions})", flush=True)
        else:
            record = check(pr)
            (RESULTS / f"{slug(pr)}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            print(f"{name}: {record['status']}, CI {record['ci']}, "
                  f"{len(record.get('introduced', []))} introduced finding(s), "
                  f"{len(record.get('introduced_in_dependencies', []))} in dependencies", flush=True)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect").add_argument("--per-lib", type=int, default=6)
    for stage in ("fetch", "check"):
        p = sub.add_parser(stage)
        p.add_argument("--only", help="owner/repo#number")
        p.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.cmd == "collect":
        collect(args.per_lib)
    else:
        run_stage(args.cmd, args.only, args.limit)


if __name__ == "__main__":
    main()

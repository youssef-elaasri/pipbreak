"""Mine public pull requests opened by AI coding agents and run apicheck on the lines they added.

Three stages, so untrusted code only ever touches a locked-down container (see run-docker.ps1):

    python research/mine.py collect --per-agent 15   # host: pick PRs          -> sample.json
    python research/mine.py prepare                  # host: fetch their diffs -> sample.json
    mine.py fetch                                    # container, online:  clone + install deps
    mine.py check                                    # container, offline: run apicheck -> results/

`collect` and `prepare` only call the GitHub API (they need your gh login). `fetch` and `check`
need no credentials. Nothing from a PR is ever executed: dependencies are installed as wheels only
(no build scripts) and apicheck runs with the checkout kept off sys.path (see run_check.py).
Imported dependencies do run their own import-time code - that's what the container is for.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
SAMPLE = Path(os.environ.get("APICHECK_SAMPLE", HERE / "sample.json"))
WORK = Path(os.environ.get("APICHECK_WORK", HERE / "work"))        # checkouts + installed deps
RESULTS = Path(os.environ.get("APICHECK_RESULTS", HERE / "results"))
GH = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"

AGENTS = {
    "copilot": ["--author=app/copilot-swe-agent"],
    "devin": ["--author=app/devin-ai-integration"],
    "cursor": ["--author=app/cursor"],
    "claude-code": ["Generated with Claude Code in:body"],
}
MIN_STARS = 20
PER_REPO = 2
MAX_DEPENDENCIES = 150  # monorepos with hundreds of deps take forever and rarely matter here


def gh(*args: str, timeout: int = 120) -> str:
    result = subprocess.run([GH, *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {result.stderr.strip()}")
    return result.stdout


def gh_json(*args: str):
    return json.loads(gh(*args))


def slug(pr: dict) -> str:
    return f"{pr['repo'].replace('/', '__')}__{pr['number']}"


def load_sample() -> list[dict]:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def save_sample(sample: list[dict]):
    SAMPLE.write_text(json.dumps(sample, indent=2), encoding="utf-8")


# -- host: collect + prepare --------------------------------------------------------------------

def collect(per_agent: int):
    stars: dict[str, int | None] = {}

    def repo_stars(repo: str):
        if repo not in stars:
            try:
                info = gh_json("api", f"repos/{repo}")
                stars[repo] = None if info.get("archived") else info.get("stargazers_count", 0)
            except RuntimeError:
                stars[repo] = None
        return stars[repo]

    sample = []
    for agent, query in AGENTS.items():
        prs = gh_json("search", "prs", *query, "--language=python", "--created=>=2026-06-01",
                      "--sort=created", "--limit=400",
                      "--json=repository,number,state,author,createdAt,title,url")
        picked, per_repo = [], {}
        for pr in prs:
            repo = pr["repository"]["nameWithOwner"]
            if per_repo.get(repo, 0) >= PER_REPO or (repo_stars(repo) or 0) < MIN_STARS:
                continue
            per_repo[repo] = per_repo.get(repo, 0) + 1
            picked.append({"agent": agent, "repo": repo, "number": pr["number"], "state": pr["state"],
                           "author": pr["author"]["login"], "created": pr["createdAt"],
                           "title": pr["title"], "url": pr["url"], "stars": stars[repo]})
            if len(picked) >= per_agent:
                break
        print(f"{agent}: {len(prs)} PRs found, {len(picked)} picked from {len(per_repo)} repos")
        sample += picked
    save_sample(sample)
    print(f"wrote {len(sample)} PRs to {SAMPLE}")


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(patch: str) -> list[int]:
    """Line numbers (in the new file) that a unified-diff patch adds."""
    lines, current = [], 0
    for row in patch.splitlines():
        m = HUNK.match(row)
        if m:
            current = int(m.group(1))
        elif row.startswith("+"):
            lines.append(current)
            current += 1
        elif not row.startswith("-") and not row.startswith("\\"):
            current += 1
    return lines


def prepare():
    """Record each PR's changed Python files and added lines, so the container needs no API access."""
    sample = load_sample()
    for pr in sample:
        if "changed" in pr:
            continue
        pages = json.loads(gh("api", "--paginate", "--slurp", f"repos/{pr['repo']}/pulls/{pr['number']}/files"))
        changed = {}
        for f in (f for page in pages for f in page):
            if f["filename"].endswith(".py") and f["status"] != "removed" and f.get("patch"):
                if lines := added_lines(f["patch"]):
                    changed[f["filename"]] = lines
        pr["changed"] = changed
        print(f"{pr['repo']}#{pr['number']}: {len(changed)} Python file(s), "
              f"{sum(map(len, changed.values()))} added line(s)", flush=True)
    save_sample(sample)


# -- container, online: fetch -------------------------------------------------------------------

def requirement(spec: str) -> str | None:
    """A pip-installable requirement, or None for local paths, URLs and editable installs."""
    spec = spec.split("#", 1)[0].strip()
    if not spec or spec.startswith(("-", ".", "/", "git+", "http")) or "@" in spec.split(";")[0]:
        return None
    return spec


def declared_dependencies(root: Path) -> tuple[list[str], str]:
    """Dependencies read from metadata files - never by running setup.py."""
    deps, sources = [], []
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            data = {}
        if project_deps := data.get("project", {}).get("dependencies"):
            deps += project_deps
            sources.append("pyproject")
        if poetry := data.get("tool", {}).get("poetry", {}).get("dependencies", {}):
            # Poetry's ^/~ constraints aren't pip syntax; fall back to the latest of each name.
            deps += [name for name in poetry if name.lower() != "python"]
            sources.append("poetry")

    def read(path: Path, seen: set):
        if path in seen or not path.is_file():
            return
        seen.add(path)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith(("-r ", "--requirement ")):
                included = (path.parent / line.split(None, 1)[1]).resolve()
                if included.is_relative_to(root.resolve()):
                    read(included, seen)
            elif spec := requirement(line):
                deps.append(spec)

    for req in sorted(root.glob("requirements*.txt")):
        read(req, set())
        sources.append(req.name)
    setup = root / "setup.py"
    if setup.exists() and not deps:
        try:
            for node in ast.walk(ast.parse(setup.read_text(encoding="utf-8"))):
                if isinstance(node, ast.keyword) and node.arg == "install_requires":
                    deps += ast.literal_eval(node.value)
                    sources.append("setup.py")
        except (SyntaxError, ValueError, UnicodeDecodeError):
            pass
    cleaned = []
    for d in deps:
        spec = requirement(d) if isinstance(d, str) else None
        if spec and spec not in cleaned:
            cleaned.append(spec)
    return cleaned, "+".join(sources) or "none"


def checkout(pr: dict) -> Path:
    dest = WORK / slug(pr) / "src"
    if (dest / ".git").exists():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    git = ["git", "-C", str(dest), "-c", "core.hooksPath=/dev/null", "-c", "core.longpaths=true"]
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    subprocess.run([*git, "fetch", "-q", "--depth=1", f"https://github.com/{pr['repo']}.git",
                    f"pull/{pr['number']}/head"], check=True, timeout=900)
    subprocess.run([*git, "checkout", "-q", "FETCH_HEAD"], check=True, timeout=300)
    return dest


def install(pr: dict, root: Path) -> dict:
    deps, source = declared_dependencies(root)
    target = WORK / slug(pr) / "deps"
    target.mkdir(parents=True, exist_ok=True)
    info = {"source": source, "declared": deps, "failed": [], "skipped": False}
    if len(deps) > MAX_DEPENDENCIES:
        info["skipped"] = True
        return info
    pip = [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--no-cache-dir",
           "--only-binary=:all:", "--target", str(target)]
    if deps and subprocess.run([*pip, *deps], capture_output=True, text=True, timeout=1800).returncode:
        # One package without a wheel shouldn't sink the rest.
        for d in deps:
            if subprocess.run([*pip, d], capture_output=True, text=True, timeout=900).returncode:
                info["failed"].append(d)
    return info


def fetch(pr: dict) -> dict:
    status_file = WORK / slug(pr) / "fetch.json"
    if status_file.exists():
        return json.loads(status_file.read_text(encoding="utf-8"))
    if not pr.get("changed"):
        status = {"status": "no-python-changes"}
    else:
        try:
            root = checkout(pr)
            status = {"status": "ok", "install": install(pr, root)}
        except Exception as exc:
            status = {"status": "fetch-error", "error": str(exc)[-800:]}
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps(status), encoding="utf-8")
    return status


# -- container, offline: check ------------------------------------------------------------------

def local_packages(root: Path) -> set[str]:
    names = set()
    for base in (root, root / "src", root / "lib"):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.suffix == ".py":
                names.add(child.stem)
            elif child.is_dir() and not child.name.startswith("."):
                names.add(child.name)  # packages, namespace packages, and script folders
    # Monorepos: every top-level package anywhere in the tree (a package whose parent isn't one).
    for init in root.rglob("__init__.py"):
        package = init.parent
        if not (package.parent / "__init__.py").exists() and ".git" not in package.parts:
            names.add(package.name)
    return names


def run_apicheck(pr: dict, root: Path, files: list[str]) -> list[dict]:
    cmd = [sys.executable, "-I", str(HERE / "run_check.py"), str(WORK / slug(pr) / "deps"), "--format", "json"]
    for name in sorted(local_packages(root)):
        cmd += ["--local", name]
    cmd += [str(root / f) for f in files]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=1800, cwd=HERE)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"apicheck failed: {result.stderr.strip()[-800:]}")


def check(pr: dict) -> dict:
    record = {k: v for k, v in pr.items() if k != "changed"}
    changed = {f: set(lines) for f, lines in pr.get("changed", {}).items()}
    record["python_files"] = len(changed)
    record["added_lines"] = sum(map(len, changed.values()))
    status_file = WORK / slug(pr) / "fetch.json"
    if not status_file.exists():
        return dict(record, status="not-fetched")
    fetched = json.loads(status_file.read_text(encoding="utf-8"))
    record.update(fetched)
    if fetched["status"] != "ok":
        return record
    root = WORK / slug(pr) / "src"
    present = [f for f in changed if (root / f).is_file()]
    try:
        findings = run_apicheck(pr, root, present)
    except Exception as exc:
        return dict(record, status="check-error", error=str(exc)[-800:])
    on_added = []
    for f in findings:
        rel = Path(f["path"]).resolve().relative_to(root.resolve()).as_posix()
        if f["line"] in changed.get(rel, ()):
            lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            on_added.append(dict(f, path=rel, source=lines[f["line"] - 1].strip()))
    record["findings_all_lines"] = len(findings)
    record["findings"] = on_added
    return record


# -- driver -------------------------------------------------------------------------------------

def each_pr(only: str | None, limit: int | None):
    sample = load_sample()
    if only:
        sample = [pr for pr in sample if f"{pr['repo']}#{pr['number']}" == only]
    return sample[:limit]


def run_stage(stage: str, only: str | None, limit: int | None):
    if stage == "check":
        RESULTS.mkdir(parents=True, exist_ok=True)
    for pr in each_pr(only, limit):
        name = f"{pr['agent']:12} {pr['repo']}#{pr['number']}"
        if stage == "fetch":
            status = fetch(pr)
            print(f"{name}: {status['status']}", flush=True)
        else:
            record = check(pr)
            (RESULTS / f"{slug(pr)}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            print(f"{name}: {record['status']}, {record['added_lines']} added lines, "
                  f"{len(record.get('findings', []))} finding(s)", flush=True)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect").add_argument("--per-agent", type=int, default=15)
    sub.add_parser("prepare")
    for stage in ("fetch", "check"):
        p = sub.add_parser(stage)
        p.add_argument("--only", help="owner/repo#number")
        p.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.cmd == "collect":
        collect(args.per_agent)
    elif args.cmd == "prepare":
        prepare()
    else:
        run_stage(args.cmd, args.only, args.limit)


if __name__ == "__main__":
    main()

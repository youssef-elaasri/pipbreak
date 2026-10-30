"""Benchmark: does apicheck catch documented "an update broke a package that didn't pin it" cases?

Each case pins a dependent package and moves one library from an old to a new version. A smoke
script that exercises the dependent must pass before the update; whether it fails after is the
ground truth, independent of apicheck. Then `apicheck.compare` runs on the two environments:
caught / missed for real breaks, quiet / flagged-no-break for updates that didn't break the smoke.

    known_breaks.py fetch     # container, online:  one venv per case and side (wheels only)
    known_breaks.py check     # container, offline: smoke scripts + compare -> results-known/

Same safety model as the other studies: wheels only, the check stage has no network.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = Path(os.environ.get("APICHECK_WORK", HERE / "work")) / "known"
RESULTS = Path(os.environ.get("APICHECK_RESULTS", HERE / "results-known"))
APICHECK_ROOT = HERE.parent if (HERE.parent / "apicheck").is_dir() else Path("/app")

FLASK_1 = ["Flask==1.1.2", "Werkzeug==1.0.1", "itsdangerous==1.1.0", "click==7.1.2"]

# name: (packages on both sides, library before, library after, smoke script)
CASES = {
    "jinja2-2.11-markupsafe-2.1": (
        ["Jinja2==2.11.3"], "MarkupSafe==2.0.1", "MarkupSafe==2.1.5",
        "import jinja2"),
    "flask-1.1-jinja2-3.1": (
        [*FLASK_1, "MarkupSafe==2.0.1"], "Jinja2==2.11.3", "Jinja2==3.1.4",
        "import flask"),
    "flask-1.1-itsdangerous-2.1": (
        ["Flask==1.1.2", "Werkzeug==1.0.1", "click==7.1.2", "Jinja2==2.11.3", "MarkupSafe==2.0.1"],
        "itsdangerous==1.1.0", "itsdangerous==2.1.2",
        "import flask"),
    "flask-2.0-werkzeug-2.1": (
        ["Flask==2.0.3", "Jinja2==3.0.3", "itsdangerous==2.0.1", "click==8.0.4"],
        "Werkzeug==2.0.3", "Werkzeug==2.1.2",
        "import flask\napp = flask.Flask('a')\napp.route('/')(lambda: 'x')\n"
        "assert app.test_client().get('/').status_code == 200"),
    "flask-2.2-werkzeug-3.1": (  # 3.0 only deprecated werkzeug.__version__; 3.1 removed it
        ["Flask==2.2.5"], "Werkzeug==2.3.8", "Werkzeug==3.1.3",
        "import flask\napp = flask.Flask('a')\napp.route('/')(lambda: 'x')\n"
        "assert app.test_client().get('/').status_code == 200"),
    "flask-login-0.6.2-werkzeug-3.0": (
        ["Flask==2.3.3", "Flask-Login==0.6.2"], "Werkzeug==2.3.8", "Werkzeug==3.0.1",
        "import flask_login"),
    "flask-sqlalchemy-2.5-sqlalchemy-2.0": (
        ["Flask==2.2.5", "Werkzeug==2.3.8", "Flask-SQLAlchemy==2.5.1"],
        "SQLAlchemy==1.4.52", "SQLAlchemy==2.0.30",
        "from flask_sqlalchemy import SQLAlchemy\nSQLAlchemy()"),
    "pandas-1.5-sqlalchemy-2.0": (
        ["pandas==1.5.3", "numpy==1.26.4"], "SQLAlchemy==1.4.52", "SQLAlchemy==2.0.30",
        "import pandas as pd, sqlalchemy as sa\n"
        "pd.read_sql('select 1 as x', sa.create_engine('sqlite://'))"),
    "openai-1.40-httpx-0.28": (
        ["openai==1.40.0"], "httpx==0.27.2", "httpx==0.28.1",
        "from openai import OpenAI\nOpenAI(api_key='x')"),
    "pandas-1.5-numpy-2.0": (
        ["pandas==1.5.3"], "numpy==1.26.4", "numpy==2.0.2",
        "import pandas"),
    "webargs-8.4-marshmallow-4": (
        ["webargs==8.4.0"], "marshmallow==3.21.3", "marshmallow==4.0.0",
        "from webargs import fields\nfrom webargs.core import Parser\n"
        "Parser().parse({'a': fields.Str(load_default='x')}, {}, location='json')"),
    "marshmallow-sqlalchemy-1.0-marshmallow-4": (
        ["marshmallow-sqlalchemy==1.0.0", "SQLAlchemy==2.0.30"], "marshmallow==3.21.3", "marshmallow==4.0.0",
        "import marshmallow_sqlalchemy"),
}


def python(case: str, side: str) -> Path:
    return WORK / case / side / "bin" / "python"


def fetch(case: str) -> dict:
    status_file = WORK / case / "fetch.json"
    if status_file.exists():
        return json.loads(status_file.read_text())
    both, old, new = CASES[case][:3]
    status = {}
    for side, lib in (("base", old), ("head", new)):
        venv = WORK / case / side
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        result = subprocess.run(
            [str(python(case, side)), "-m", "pip", "install", "-q", "--disable-pip-version-check",
             "--no-cache-dir", "--only-binary=:all:", *both, lib],
            capture_output=True, text=True, timeout=1800)
        status[side] = "ok" if result.returncode == 0 else result.stderr.strip()[-600:]
    status_file.write_text(json.dumps(status))
    return status


def smoke(case: str, side: str) -> str | None:
    """None when the smoke script passes, else the last line of its error."""
    result = subprocess.run([str(python(case, side)), "-I", "-c", CASES[case][3]],
                            capture_output=True, text=True, timeout=600, cwd="/tmp")
    if result.returncode == 0:
        return None
    lines = result.stderr.strip().splitlines()
    return lines[-1] if lines else f"exit {result.returncode}"


def check(case: str) -> dict:
    record = {"case": case, "pins": CASES[case][0], "old": CASES[case][1], "new": CASES[case][2]}
    status_file = WORK / case / "fetch.json"
    fetched = json.loads(status_file.read_text()) if status_file.exists() else {}
    if fetched != {"base": "ok", "head": "ok"}:
        return dict(record, status="install-failed", install=fetched)
    record["smoke_base"], record["smoke_head"] = smoke(case, "base"), smoke(case, "head")
    if record["smoke_base"]:
        return dict(record, status="base-broken")  # the "before" side must work
    env = dict(os.environ, PYTHONPATH=str(APICHECK_ROOT))
    out = WORK / case / "report.md"
    result = subprocess.run([sys.executable, "-m", "apicheck.compare", "--base", str(python(case, "base")),
                             "--head", str(python(case, "head")), "--markdown", str(out)],
                            capture_output=True, text=True, env=env, timeout=1800)
    if result.returncode not in (0, 1):
        return dict(record, status="check-error", error=result.stderr.strip()[-800:])
    flagged = result.returncode == 1
    if record["smoke_head"]:
        record["status"] = "caught" if flagged else "missed"
    else:  # the update didn't break the smoke script: flagging it is a false alarm (or a deeper break)
        record["status"] = "flagged-no-break" if flagged else "quiet"
    record["report"] = result.stdout
    return record


def main():
    stage = sys.argv[1]
    only = sys.argv[2] if len(sys.argv) > 2 else None
    RESULTS.mkdir(parents=True, exist_ok=True) if stage == "check" else None
    for case in CASES:
        if only and case != only:
            continue
        if stage == "fetch":
            print(f"{case:42} {fetch(case)}", flush=True)
        else:
            record = check(case)
            (RESULTS / f"{case}.json").write_text(json.dumps(record, indent=2))
            print(f"{case:42} {record['status']:15} head: {record.get('smoke_head')}", flush=True)


if __name__ == "__main__":
    main()

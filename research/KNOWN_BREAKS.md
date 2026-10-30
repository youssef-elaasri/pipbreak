# Benchmark: known "the update broke a package that didn't pin it" cases

October 2026. `research/known_breaks.py`, run in the Docker sandbox
(`run-docker.ps1 -Study known -Python 3.10`).

## Method

Each case pins a dependent package and moves one library from an old to a new version, like a
Dependabot PR would. Ground truth comes from a **smoke script** that exercises the dependent
(import it, build an app, call the function): it must pass before the update, and whether it fails
after decides if the update is a break - independently of apicheck. Then `apicheck.compare` runs
on the two environments, exactly as the GitHub Action does.

## Results

| Case | Smoke after the update | apicheck |
|---|---|---|
| Jinja2 2.11 + MarkupSafe 2.0 -> 2.1 | `cannot import name 'soft_unicode'` | **caught, exact** - `markupsafe.soft_unicode` in 3 files |
| Flask 1.1 + Jinja2 2.11 -> 3.1 | `cannot import name 'escape' from 'jinja2'` | **caught, exact** - `jinja2.escape`, `jinja2.Markup` |
| Flask 1.1 + itsdangerous 1.1 -> 2.1 | `cannot import name 'json' from 'itsdangerous'` | **caught, exact** - `itsdangerous.json` |
| Flask 2.2 + Werkzeug 2.3 -> 3.1 | `module 'werkzeug' has no attribute '__version__'` | **caught, exact** - `flask/testing.py:116`, `flask/cli.py:250` |
| Flask-Login 0.6.2 + Werkzeug 2.3 -> 3.0 | `cannot import name 'url_decode'` | **caught, exact** - `url_decode`, `url_encode` |
| Flask-SQLAlchemy 2.5 + SQLAlchemy 1.4 -> 2.0 | `module 'sqlalchemy' has no attribute '__all__'` | **caught, other symptom** - `get_bind()` called with 3 positional args, 2 allowed; `__all__` is read from a loop variable |
| openai 1.40 + httpx 0.27 -> 0.28 | `Client.__init__() got an unexpected keyword argument 'proxies'` | **caught, other call site** - `httpx.Client(proxies=)` in the openai CLI; the client's own path passes it through `**kwargs` |
| pandas 1.5 + numpy 1.26 -> 2.0 | `numpy.dtype size changed` (binary) | **caught, other symptom** - ~15 removed numpy APIs (`np.NaN`, `np.float_`, `find_common_type`...) that would break pandas even without the binary problem |
| pandas 1.5 + SQLAlchemy 1.4 -> 2.0 | `'OptionEngine' object has no attribute 'execute'` | **missed** - `self.con.execute()` on a user-supplied object; its type isn't knowable statically |
| Flask 2.0 + Werkzeug 2.0 -> 2.1 | works | quiet |
| webargs 8.4 + marshmallow 3 -> 4 | works | quiet |
| marshmallow-sqlalchemy 1.0 + marshmallow 3 -> 4 | works | quiet |

**Real breaks: 8 of 9 flagged** (5 at the exact failing line, 3 through a different but real
incompatibility in the same package). **Updates that didn't break: 3 of 3 quiet** - no false alarms.
For the GitHub Action, which turns this into pass/fail on the PR, the verdict was right in 11 of 12.

## What it can't see

- **Dynamically typed objects** - methods called on values passed in from outside (`con.execute`).
- **Arguments forwarded through `**kwargs`** into the changed library.
- **Binary (ABI) breaks** - C extensions compiled against an older library. It caught pandas/numpy 2
  only because pandas also used removed Python APIs.
- **Behaviour changes** with the same API - nothing static can see those.

## Bug this found in apicheck

On Python < 3.11, `importlib.metadata.packages_distributions()` only reads `top_level.txt`, which
flit-built wheels (Jinja2 3.1, Werkzeug 3) don't have. `--deps` couldn't attribute `jinja2` to a
distribution and dropped the findings: 2 real breaks were missed. apicheck now infers top-level
modules from `RECORD`, as Python 3.11+ does.

## Caveats

12 cases, chosen by me from well-known breakages - so biased toward the import-and-attribute kind
apicheck is built for. The smoke scripts exercise one path each; "quiet" means that path works, not
that nothing anywhere changed.

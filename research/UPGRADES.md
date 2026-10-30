# Does apicheck catch what a major dependency bump breaks?

October 2026. `research/upgrades.py`, run in the Docker sandbox (`run-docker.ps1 -Study upgrades -Python 3.11`).

## Method

40 Dependabot/Renovate PRs that bump pydantic, SQLAlchemy, numpy, marshmallow, httpx, openai,
Werkzeug or pandas (repos with 10+ stars). For each one: check out the PR head and its parent,
install each side's pinned dependencies (wheels only), and run apicheck twice on the same code -
once against the old environment, once against the new. A finding counts as *introduced* when it
only appears against the new environment and names the bumped library. Then compare with the PR's CI.

Two scopes:

- **repo** - the project's own code;
- **dependencies** - every *installed* package (other than the library) whose code imports the
  bumped library. Same version on both sides, so anything new is caused by the bump.

## Sample attrition

| | PRs |
|---|---|
| collected | 40 |
| bump not reproduced (monorepo / lock-file-only deps, packages without wheels, the library never installed) | 22 |
| fetch timed out | 1 |
| installed, but not a major bump (search noise: pydantic 2.14.1 -> 2.14.2, openai 1.109.0 -> .1, ...) | 6 |
| installed, but the new side lost the library (fabston: Werkzeug missing after the bump) | 1 |
| **real major bumps, both sides installed and checked** | **10** |

The collection step is the weak point: the search query matches minor bumps, and the installer only
understands `requirements*.txt`, `pyproject.toml` and literal `setup.py`.

## Results on the 10 real major bumps

| PR | bump | CI | introduced (repo) | introduced (dependencies) | what CI failed on |
|---|---|---|---|---|---|
| IBM/ada#17 | Werkzeug 2.2 -> 3.1 | fail | 0 | **2 errors** - Flask 2.2.5: `werkzeug.__version__` | the same `werkzeug.__version__` AttributeError in `flask/testing.py:116` |
| Septem151/gifsync#61 | Werkzeug 2.2 -> 3.1 | fail | 0 | **10 errors** - Flask: `url_quote`, `url_parse`, `__version__`; Flask-Login: `url_encode`, `url_decode`; Flask-WTF: `url_encode` | a deprecated GitHub Action, before tests ran; the app itself cannot even be imported (`from werkzeug.urls import url_quote` in `flask/app.py`) |
| Azure/containerization-assist#803 | Werkzeug 2.3 -> 3.1 (a test fixture app) | **pass** | 0 | **2 errors** - Flask: `werkzeug.__version__` | nothing - CI never runs the fixture app, so the break ships |
| closeio/sync-engine#1207 | SQLAlchemy 1.4 -> 2.1 | fail | 56 deprecations (`declarative_base` moved to `sqlalchemy.orm`, `noload`, `as_declarative`) | 9 in Alembic (`util.zip_longest`, `util.text_type`, `RemovedIn20Warning` - mostly `alembic.testing`; `Variant` deprecated) | mypy errors from SQLAlchemy 2's new typing |
| Merck/matcher#17 | Werkzeug 2.0 -> 3.1 | fail | 0 | 0 | pip `ResolutionImpossible` |
| PlanExeOrg/PlanExe#874 | marshmallow 3 -> 4 | fail | 0 | 0 | pip `ResolutionImpossible` |
| Azure-Samples/ai-rag-chat-evaluator#171 | marshmallow 3 -> 4 | fail | 0 | 0 | pip `ResolutionImpossible` (azure-ai-evaluation needs marshmallow < 4) |
| KarelZe/tclf#289 | numpy 1.26 -> 2.4 | pass | 0 | 1 - scipy: `np.array(val, stype=...)`, a latent typo (see below) | - |
| jhd3197/ServerKit#170, wachawo/text-to-speech#36 | marshmallow 3 -> 4 | pass | 0 | 0 | - |

The numpy bump was the false-positive test: scipy, scikit-learn and joblib all support numpy 1 and
2 side by side. The first run flagged 13 "errors" there - all in version fallbacks
(`except ImportError: from numpy import RankWarning`, `if np_version >= parse_version("1.25"): ...
else: ...`) - plus `np.typing` as "deprecated" because its module docstring deprecates one member.
After fixing those, one finding remains and it is real: `scipy/sparse/_dok.py:184` passes
`stype=` to `np.array`, which raises TypeError whenever that path runs. It only shows up with
numpy 2 because numpy 1's `np.array` exposes no signature to check against.

Every remaining dependency finding is a symbol that really is gone (or deprecated) in the new version.

## What this says

1. **Project code rarely breaks on its own; the dependency graph does.** None of the 10 repos had a
   hard API break in their own code. The hard breaks were all in *other installed packages* that use
   the bumped library and don't cap it - Flask 2.2 declares `Werkzeug>=2.2.2`, so pip happily
   installs Werkzeug 3 and `pip check` stays green.
2. **apicheck pinpoints those breaks statically, before anything runs.** In the one PR where CI got
   as far as running tests (IBM/ada), apicheck reported the exact line that failed. In gifsync it
   found the breaks hiding behind an unrelated CI failure, and in containerization-assist it found a
   break that CI passed.
3. **Most CI failures on bump PRs aren't API breaks** - of 6, three were resolver conflicts, one CI
   config, one mypy.
   pip already reports those; apicheck has nothing to add there.
4. **Deprecations in project code** (closeio) are a useful migration to-do list, not a blocker.

The sample is small (3 PRs with hard breaks, all Werkzeug 3 + old Flask), so this is a signal, not
proof. It points at a product shape: *"before you merge this bump, here's what in your installed
dependencies will break"* - `apicheck --deps`, or a Dependabot companion check.

## Follow-up: `apicheck --deps` on whole environments

The dependency scan is now a CLI mode. Run with no `--uses` filter over the *entire* installed
environment of four PRs (900-1,800 files, 35-110 s each), after skipping vendored copies, shipped
test suites and platform-dependent `six.moves`:

| environment | errors before the bump | errors after | new after the bump |
|---|---|---|---|
| gifsync (Werkzeug 3) | 0 | 8 | the 8 Flask / Flask-Login / Flask-WTF breaks |
| IBM/ada (Werkzeug 3) | 0 | 2 | the 2 Flask breaks CI hit |
| closeio (SQLAlchemy 2.1) | 2 | 9 | 7 in `alembic.testing` (alembic 1.7.5 vs SQLAlchemy 2) |
| tclf (numpy 2) | 1 | 2 | scipy `np.array(stype=)` |

The rest: dead code in cffi (`pycparser.yacctab`, in a function commented "never called") and
`np.zeroslike` at `scipy/sparse/_coo.py:1516` - a real typo in scipy 1.17.1, present with any numpy.
Warnings are genuine deprecations (pytest-mock's `yield_fixture`, Flask-SQLAlchemy's `Query.get`).
`--uses <lib>` narrows the same check to seconds (32 files for IBM/ada).

Comparing before and after the bump removes the pre-existing findings entirely - that's how a
Dependabot check should run it.

## Bugs found in apicheck along the way

- Live proxies (Flask's `request`) raised during introspection and crashed the run - now caught.
- A deprecated *parameter* (`Session.flush(objects=)`) flagged the whole function - only top-level
  `.. deprecated::` counts now.
- Packages that print on import (pygame) corrupted `--format json` - introspection output goes to stderr.
- A non-UTF-8 source file crashed the whole run - files are read with their PEP 263 encoding, and
  undecodable ones become an API000 finding.
- Version fallbacks were flagged: `except ImportError/AttributeError:` bodies and both branches of
  a library-version comparison now count as guarded.
- A module was reported deprecated when its docstring deprecated one of its members - module
  docstrings no longer count.

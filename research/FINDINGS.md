# Mining AI-agent PRs: first run (2026-10-29)

## Sample

54 PRs opened since 2026-06-01 by GitHub Copilot's coding agent, Devin, Cursor and Claude Code, in
public repos with at least 20 stars (at most 2 per repo). 33 of them change Python: **12,409 added
lines** across PostHog, Prefect, pydantic-ai, Sentry, airbyte, litellm, E2B and others. Each was
checked in a Docker sandbox against Python 3.13.18 and the project's declared dependencies.

## Result: 0 real bugs

pipbreak reported 94 findings on lines the agents added. Triaged by hand, **none is a bug**:

| Findings | What they are | Verdict |
|---|---|---|
| 50 × API001 "package not installed" | Test/dev dependencies we didn't install (`pytest` ×23, `PIL`, `botocore`, `inline_snapshot`, `torch`), the project's own package in a monorepo (`e2b`, `prime_cli`, `posthoganalytics`), or deps declared in a sub-project's own `pyproject.toml` (`airbyte_cdk`) | Our environment, not the agent |
| 44 × API101 "deprecated" | `typing.List`, `typing.Callable`, `typing.Mapping`... These are documented as deprecated aliases but have no removal date and are used everywhere | Noise: pipbreak shouldn't report these |

No hallucinated module, attribute, keyword argument or removed API appeared on any added line.

## What this does and doesn't show

**Caveats on coverage**
- Dependencies were installed only partially: dev/test extras and sub-project dependencies were
  skipped, so code importing them was checked less deeply.
- pipbreak's inference doesn't follow unannotated return values, so most method calls on objects
  from third-party libraries weren't checked.
- 33 PRs is a small sample, and these are PRs from agents that usually run tests and CI before
  opening a PR, in repos that review their code.

**The likely reading:** by the time an agent opens a PR, the "API that doesn't exist" class of bug
has mostly been caught already, because the agent ran the code. If that holds up at a larger sample,
a PR-level checker for AI code is a weak product; the value would be earlier (at generation time,
inside the agent loop) or in code that isn't executed before review (scripts, notebooks, config,
rarely-run paths).

## Fixes this run surfaced

1. pipbreak: don't report the soft-deprecated `typing` / `typing_extensions` aliases.
2. Miner: install `[project.optional-dependencies]` and `[dependency-groups]` (dev/test) too.
3. Miner: read dependency files nearest to each changed file, not only at the repo root.
4. Miner: treat every package directory in the repo as local (monorepos).

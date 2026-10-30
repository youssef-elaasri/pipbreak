# apicheck

Catch hallucinated and deprecated API calls **against the exact package versions you have installed**.

AI assistants write code for "pydantic", not for *your* pydantic. `apicheck` parses your code and checks
every imported name, attribute chain, method and call signature against the real modules in your environment.

```text
$ apicheck examples/
examples/app.py:9:1: error API002 'pydantic.BaseSettings' is not available in pydantic==2.9.2: `BaseSettings` has been moved to the `pydantic-settings` package. ...
examples/app.py:9:1: warning API101 'pydantic.validator' is deprecated in pydantic==2.9.2: Pydantic V1 style `@validator` validators are deprecated. ...
examples/app.py:31:8: error API002 'os.path' has no attribute 'exist' in Python 3.13. Did you mean 'exists'?
examples/app.py:33:27: error API003 shutil.copy() has no parameter 'overwrite' in Python 3.13.
examples/app.py:37:39: error API003 random.choices() has no parameter 'unique' in Python 3.13.
examples/app.py:45:30: error API002 'collections' has no attribute 'Mapping' in Python 3.13. Did you mean 'collections.abc.Mapping'?
examples/app.py:50:12: error API002 'Path' object has no attribute 'exist' in Python 3.13. Did you mean 'exists'?
examples/app.py:52:27: error API003 path.read_text() has no parameter 'encodings' in Python 3.13. Did you mean 'encoding'?
examples/app.py:56:12: warning API101 'User.dict' is deprecated in pydantic==2.9.2: The `dict` method is deprecated; use `model_dump` instead.
examples/app.py:60:12: warning API101 'User.parse_obj' is deprecated in pydantic==2.9.2: The `parse_obj` method is deprecated; use `model_validate` instead.
examples/app.py:71:12: warning API005 'Job' object has no attribute 'retry_count': it is never assigned in the class or its bases.
```

Run the same file against pydantic 1.10 and the answer changes: `BaseSettings`, `.dict()` and
`.parse_obj()` are fine, but `field_validator` doesn't exist yet (`Did you mean 'validator'?`).

## Usage

```bash
pip install -e .                       # zero dependencies
apicheck src/                          # check against the current interpreter
apicheck --python .venv/bin/python src/  # check against another environment
apicheck --format github src/          # inline annotations on GitHub PRs
apicheck --format json src/            # machine-readable
```

Exit code is 1 when there are errors (`--strict` makes warnings fail too).

### Do my installed packages still work together? (`--deps`)

pip only checks declared version ranges, and those are often open-ended: Flask 2.2 asks for
`Werkzeug>=2.2.2`, so upgrading to Werkzeug 3 installs cleanly - and then fails at runtime.
`--deps` checks the code of every installed package against the *other* installed packages:

```text
$ pip install -U werkzeug
$ apicheck --deps --uses werkzeug
flask/testing.py:116:44: error API002 Flask==2.2.5: 'werkzeug' has no attribute '__version__' in Werkzeug==3.1.9.
flask/cli.py:250:21: error API002 Flask==2.2.5: 'werkzeug' has no attribute '__version__' in Werkzeug==3.1.9.

Checked 32 file(s) against Python 3.11 at /usr/bin/python: 2 error(s), 0 warning(s).
```

- `--uses LIB` (repeatable; import or distribution name) narrows the check to packages that import
  LIB - the one you just upgraded. That takes seconds; a whole environment takes a minute or more.
- Only calls from one distribution into another count. A package's own internals, the stdlib,
  missing optional extras, vendored copies and shipped test suites are skipped.
- Combine with `--python .venv/bin/python` to check another environment, e.g. on a Dependabot PR.

### On Dependabot / Renovate PRs

The action installs your dependencies twice - from the PR's base commit and from the PR - and
reports only what the update breaks, as a job summary and a PR comment. It fails the check when an
installed package calls an API the update removed.

```yaml
# .github/workflows/apicheck.yml
name: apicheck
on: pull_request
permissions:
  contents: read
  pull-requests: write   # for the comment; Dependabot PRs get a read-only token without this
jobs:
  dependency-update:
    if: github.actor == 'dependabot[bot]' || github.actor == 'renovate[bot]'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"   # the Python you deploy on
      - uses: youssef-elaasri/apicheck@main
        # with:
        #   install: pip install -r requirements/prod.txt   # default: requirements.txt, else `pip install .`
        #   working-directory: backend
        #   fail-on-break: false
```

Use `pull_request`, not `pull_request_target`: installing the PR's dependencies runs their build
code, which must not see a write token for your repository.

Locally, the same comparison between any two environments:

```bash
python -m apicheck.compare --base .venv-old/bin/python --head .venv/bin/python --markdown report.md
```

### GitHub Actions (your own code)

```yaml
- run: pip install -r requirements.txt   # your pinned versions
- run: pip install apicheck
- run: apicheck --format github src/
```

## Checks

| Code   | Severity | Meaning |
|--------|----------|---------|
| API001 | error    | Package or module doesn't exist / isn't installed |
| API002 | error    | Name or attribute doesn't exist in the installed version (with "did you mean") |
| API003 | error    | Call uses a keyword argument the function doesn't accept |
| API004 | error    | Call passes more positional arguments than the function accepts |
| API005 | warning  | Attribute is never assigned anywhere in the object's class (instances with a `__dict__`) |
| API101 | warning  | API is deprecated (PEP 702 `@deprecated` or a `.. deprecated::` docstring note) |

## What it knows about your variables

A light, flow-insensitive inference tracks what each name holds, per function scope:

| Code | Inferred as | Checks |
|------|-------------|--------|
| `p = Path(name)`, `Path(name).read_text()` | a `Path`, exactly | missing attributes, method signatures, deprecations |
| `def f(user: User)`, `x: Client \| None` | a `User` or subclass | method signatures, deprecations |
| `client.get(url)` with `-> Response` | a `Response` or subclass | method signatures, deprecations |
| `self` in a method | the class or a subclass | method signatures, deprecations |
| `class Job: ...` in your file | modelled from its source and its real base classes | all of the above |

Missing attributes are only reported for objects we saw constructed: an annotated parameter may be a
subclass with more attributes. For classes whose instances have no `__dict__` (`__slots__`, builtins)
that's an **error**. Otherwise attributes are read from the assignments in the class source, so it's a
**warning** (API005): code elsewhere could still set the attribute.

## Avoiding false positives

- Imports and calls under feature probes are skipped: `if hasattr(...)`, `sys.platform`,
  `sys.version_info`, `os.name`, `TYPE_CHECKING`, `try/except ImportError|AttributeError`, and
  flags assigned from these (`HAVE_FORK = hasattr(os, "fork")`).
- Your own modules, relative imports, and names you reassign are never checked. A name bound to
  different things in one scope (or used as a loop/comprehension/lambda variable) isn't inferred.
- Classes with a custom `__getattr__`/`__getattribute__`, a custom metaclass (ORMs, pydantic, enums),
  class decorators, or code that writes `self.__dict__`/`setattr(self, ...)` are never reported as
  missing attributes - only deprecations and signatures are checked.
- `super()`, proxies, and functions taking `**kwargs` / `*args` aren't checked for those arguments.
- Silence a line with `# apicheck: ignore`.

Checked against pydantic 2.9 and 21 stdlib packages (429 files), the only errors left are real for the
environment: Unix-only APIs (`os.fork`, `socket.AF_UNIX`) in modules that only run on Unix.

## Limitations (MVP)

- **Imports third-party modules** to inspect them, so their import-time code runs. Use it in an env you trust.
- Inference is per scope and doesn't follow `isinstance` narrowing, container contents (`users[0].x`),
  or return types without annotations.
- Python only.

## Development

```bash
python -m unittest discover -s tests
```

"""Run apicheck on an untrusted checkout without putting the checkout on sys.path.

Invoked as `python -I run_check.py <deps_dir> <apicheck args...>`. `-I` keeps the current directory,
the script directory and PYTHONPATH off sys.path, so a planted `json.py` in the checkout can't be
imported. Installed dependencies go *after* the stdlib so they can't shadow it either.
"""
import sys
from pathlib import Path

deps = sys.argv[1]
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the apicheck package
sys.path.append(deps)

from apicheck.cli import main  # noqa: E402

sys.exit(main(sys.argv[2:]))

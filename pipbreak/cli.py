from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import warnings
from dataclasses import asdict
from pathlib import Path

from .checker import check_installed, check_paths


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="pipbreak",
        description="Flag calls to APIs that don't exist in your installed package versions.")
    parser.add_argument("paths", nargs="*", default=["."], help="files or directories to check")
    parser.add_argument("--python", help="interpreter of the environment to check against "
                                         "(e.g. .venv/bin/python); defaults to the current one")
    parser.add_argument("--format", choices=["text", "json", "github"], default="text")
    parser.add_argument("--local", action="append", default=[],
                        help="top-level module name that belongs to your project (repeatable)")
    parser.add_argument("--strict", action="store_true", help="exit non-zero on warnings too")
    parser.add_argument("--deps", action="store_true",
                        help="instead of your code, check the installed packages against each other: "
                             "calls one package makes into another that the installed version lacks")
    parser.add_argument("--uses", action="append", default=[], metavar="LIB",
                        help="with --deps, only check packages that import LIB, e.g. the one you "
                             "just upgraded (repeatable)")
    args = parser.parse_args(argv)
    if args.uses and not args.deps:
        parser.error("--uses only applies with --deps")
    if args.deps and (args.paths != ["."] or args.local):
        parser.error("--deps checks installed packages; it takes no paths or --local")
    return args


def run_in(python: str, argv: list[str]) -> int:
    """Re-run ourselves under the target interpreter, so we see *its* installed packages."""
    forwarded, skip = [], False
    for arg in argv:
        if skip:
            skip = False
        elif arg == "--python":
            skip = True
        elif not arg.startswith("--python="):
            forwarded.append(arg)
    env = dict(os.environ)
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH")]))
    return subprocess.call([python, "-m", "pipbreak", *forwarded], env=env)


def progress(done: int, total: int, package: str | None = None):
    """One status line, rewritten in place (spaces, not ANSI codes: old Windows consoles show those raw)."""
    width = shutil.get_terminal_size().columns - 1
    text = f"checking {package} ({done + 1}/{total})" if package else ""
    sys.stderr.write("\r" + text[:width].ljust(width) + ("" if package else "\r"))
    sys.stderr.flush()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)
    if args.python:
        return run_in(args.python, argv)

    # Introspection imports the checked code's dependencies, and some print on import (pygame's
    # banner) - keep that off stdout, or it corrupts --format json. Their warnings (cryptography's
    # deprecations on attribute access, SyntaxWarnings from parsing old code) are noise here:
    # deprecations we report come from the APIs' own markers.
    with contextlib.redirect_stdout(sys.stderr), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if args.deps:
            # A whole environment takes a minute or more; without this it looks frozen.
            show = sys.stderr.isatty()
            findings, file_count = check_installed(set(args.uses), progress if show else None)
            if show:
                progress(0, 0)  # clear the status line
        else:
            findings, file_count = check_paths([Path(p) for p in args.paths], set(args.local))
    errors = sum(f.severity == "error" for f in findings)
    warning_count = len(findings) - errors

    if args.format == "json":
        print(json.dumps([asdict(f) for f in findings], indent=2))
    elif args.format == "github":
        for f in findings:
            print(f"::{f.severity} file={f.path},line={f.line},col={f.col},title={f.code}::{f.message}")
    else:
        for f in findings:
            print(f"{f.path}:{f.line}:{f.col}: {f.severity} {f.code} {f.message}")
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        print(f"\nChecked {file_count} file(s) against Python {version} at {sys.executable}: "
              f"{errors} error(s), {warning_count} warning(s).")

    return 1 if errors or (args.strict and warning_count) else 0

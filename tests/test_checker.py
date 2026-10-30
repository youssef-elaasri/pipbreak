import contextlib
import io
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from pipbreak import check_source, compare
from pipbreak.checker import check_installed, check_paths, packages_distributions
from pipbreak.cli import main


def codes(source, **kwargs):
    return [f.code for f in check_source(textwrap.dedent(source), **kwargs)]


def messages(source):
    return [f.message for f in check_source(textwrap.dedent(source))]


class MissingApis(unittest.TestCase):
    def test_missing_package(self):
        self.assertEqual(codes("import definitely_not_a_real_pkg\n"), ["API001"])

    def test_missing_submodule(self):
        self.assertEqual(codes("import os.nope\n"), ["API001"])

    def test_missing_from_import(self):
        self.assertEqual(codes("from json import parse\n"), ["API002"])

    def test_missing_attribute_with_suggestion(self):
        [msg] = messages("import os.path\nos.path.exist('x')\n")
        self.assertIn("'os.path' has no attribute 'exist'", msg)
        self.assertIn("Did you mean 'exists'?", msg)

    def test_moved_into_submodule(self):
        [msg] = messages("import collections\ncollections.Mapping\n")
        self.assertIn("Did you mean 'collections.abc.Mapping'?", msg)

    def test_alias_is_followed(self):
        self.assertEqual(codes("import json as j\nj.parse('{}')\n"), ["API002"])

    def test_missing_class_attribute(self):
        self.assertEqual(codes("from pathlib import Path\nPath.cwdir()\n"), ["API002"])


class Calls(unittest.TestCase):
    def test_unknown_keyword(self):
        [msg] = messages("import shutil\nshutil.copy('a', 'b', overwrite=True)\n")
        self.assertIn("shutil.copy() has no parameter 'overwrite'", msg)

    def test_keyword_suggestion(self):
        [msg] = messages("import random\nrandom.choices([1], weight=[1])\n")
        self.assertIn("Did you mean 'weights'?", msg)

    def test_too_many_positional(self):
        self.assertEqual(codes("import os.path\nos.path.exists('a', 'b')\n"), ["API004"])

    def test_bare_name_call(self):
        self.assertEqual(codes("from shutil import copy\ncopy('a', 'b', force=True)\n"), ["API003"])

    def test_class_constructor(self):
        self.assertEqual(codes("from fractions import Fraction\nFraction(1, 2, exact=True)\n"), ["API003"])

    def test_kwargs_functions_are_not_flagged(self):
        self.assertEqual(codes("import subprocess\nsubprocess.run(['x'], whatever=1)\n"), [])

    def test_star_args_are_not_flagged(self):
        self.assertEqual(codes("import os.path\nargs = ['a', 'b']\nos.path.exists(*args)\n"), [])


class Instances(unittest.TestCase):
    def test_constructed_instance(self):
        [msg] = messages("from pathlib import Path\np = Path('.')\np.exist()\n")
        self.assertIn("'Path' object has no attribute 'exist'", msg)
        self.assertIn("Did you mean 'exists'?", msg)

    def test_chained_constructor(self):
        self.assertEqual(codes("from pathlib import Path\nPath('.').made_up_method()\n"), ["API002"])

    def test_bound_method_keywords(self):
        [msg] = messages("from pathlib import Path\nPath('x').read_text(encodings='utf-8')\n")
        self.assertIn("has no parameter 'encodings'", msg)
        self.assertIn("Did you mean 'encoding'?", msg)

    def test_bound_method_positional_count_excludes_self(self):
        self.assertEqual(codes("from pathlib import Path\nPath('x').read_text('utf-8', 'strict')\n"), [])
        self.assertEqual(codes("from pathlib import Path\nPath('x').exists(1, 2)\n"), ["API004"])

    def test_inside_function_scope(self):
        source = """
            from pathlib import Path
            def load(name):
                path = Path(name)
                return path.read_txt()
        """
        self.assertEqual(codes(source), ["API002"])

    def test_class_with_dict_is_a_warning(self):
        [finding] = check_source("import argparse\nparser = argparse.ArgumentParser()\nparser.add_arg('x')\n")
        self.assertEqual((finding.code, finding.severity), ("API005", "warning"))
        self.assertIn("Did you mean 'add_argument'?", finding.message)

    def test_builtins(self):
        self.assertEqual(codes("open('x', encodings='utf-8')\n"), ["API003"])
        self.assertEqual(codes("d = dict()\nd.has_key('a')\n"), ["API002"])

    def test_local_class_instance(self):
        source = """
            class Job:
                def __init__(self, name):
                    self.name = name
                def run(self): pass
            job = Job("x")
            job.run()
            job.name
            job.nmae
        """
        findings = check_source(textwrap.dedent(source))
        self.assertEqual([(f.code, f.line) for f in findings], [("API005", 9)])
        self.assertIn("Did you mean 'name'?", findings[0].message)

    def test_local_class_attribute(self):
        source = """
            class Config:
                DEBUG = False
            Config.DEBUG
            Config.VERBOSE
        """
        self.assertEqual(codes(source), ["API002"])

    def test_local_subclass_uses_real_base(self):
        source = """
            import argparse
            class Parser(argparse.ArgumentParser):
                def extra(self): pass
            p = Parser()
            p.parse_args(nmspace=None)
            p.extra()
        """
        self.assertEqual(codes(source), ["API003"])


class NoFalsePositives(unittest.TestCase):
    def test_annotated_values_are_not_checked_for_missing_attributes(self):
        # The caller may pass a subclass with more attributes.
        source = """
            import argparse
            def f(parser: argparse.ArgumentParser):
                parser.custom_thing
        """
        self.assertEqual(codes(source), [])

    def test_classmethod_hooks_filled_by_subclasses(self):
        source = """
            class Mixin:
                @classmethod
                def parse(cls, value):
                    return cls.value_parser(value)
        """
        self.assertEqual(codes(source), [])

    def test_typing_aliases_are_not_deprecations(self):
        self.assertEqual(codes("from typing import List, Callable, Mapping\nimport typing\ntyping.Dict\n"), [])

    def test_class_that_sets_attributes_dynamically(self):
        source = """
            class Bag:
                def __init__(self, **kw):
                    self.__dict__.update(kw)
            Bag(a=1).a
        """
        self.assertEqual(codes(source), [])

    def test_self_calls_into_subclass_hooks(self):
        source = """
            class Base:
                def run(self):
                    return self.handle()
        """
        self.assertEqual(codes(source), [])

    def test_name_with_conflicting_assignments(self):
        source = """
            from pathlib import Path
            p = Path('.')
            p = make_something()
            p.made_up()
        """
        self.assertEqual(codes(source), [])

    def test_comprehension_and_lambda_variables_shadow(self):
        source = """
            from pathlib import Path
            p = Path('.')
            names = [p.made_up for p in things]
            f = lambda p: p.made_up
        """
        self.assertEqual(codes(source), [])

    def test_super_and_proxies(self):
        source = """
            class A(dict):
                def __init__(self, a, b):
                    super().__init__(a, b, c=1)
        """
        self.assertEqual(codes(source), [])

    def test_metaclass_driven_local_class(self):
        source = """
            import enum
            class Color(enum.Enum):
                RED = 1
            Color.RED
            Color.BLUE
        """
        self.assertEqual(codes(source), [])

    def test_local_class_with_unknown_base(self):
        source = """
            from somewhere_unknown_pkg import Base  # pipbreak: ignore
            class Thing(Base):
                pass
            Thing().anything
            Thing.anything
        """
        self.assertEqual(codes(source), [])

    def test_shadowed_builtin(self):
        self.assertEqual(codes("def open(path, **kw): pass\nopen('x', encodings='utf-8')\n"), [])

    def test_star_import_disables_builtins(self):
        self.assertEqual(codes("from os.path import *\nopen('x', encodings='utf-8')\n"), [])

    def test_instance_attribute_assignment(self):
        self.assertEqual(codes("from pathlib import Path\np = Path('.')\np.made_up = 1\n"), [])


    def test_valid_code(self):
        source = """
            import json, os.path
            from collections.abc import Mapping
            from pathlib import Path
            data = json.loads('{}', parse_float=float)
            os.path.join('a', 'b')
            Path('.').resolve(strict=False)
        """
        self.assertEqual(codes(source), [])

    def test_optional_import_in_try(self):
        source = """
            try:
                import definitely_not_a_real_pkg
                from json import parse
            except ImportError:
                definitely_not_a_real_pkg = None
        """
        self.assertEqual(codes(source), [])

    def test_feature_probes_guard_code(self):
        source = """
            import os, sys, socket
            from typing import TYPE_CHECKING
            if hasattr(os, "made_up_fork"):
                os.made_up_fork()
            if sys.platform == "made_up_os":
                socket.AF_MADE_UP
            if sys.version_info >= (9, 0):
                os.path.exists("a", future_flag=True)
            if TYPE_CHECKING:
                import definitely_not_a_real_pkg
            fd = hasattr(os, "made_up") and os.made_up()
            try:
                os.made_up_call()
            except AttributeError:
                pass
        """
        self.assertEqual(codes(source), [])

    def test_feature_flag_variables(self):
        source = """
            import os, sys
            IS_MADE_UP = sys.platform == "made_up_os"
            CAN_DO_IT = IS_MADE_UP and True
            class Server:
                have_made_up = hasattr(os, "made_up")
                def run(self):
                    if self.have_made_up:
                        os.made_up()
            if CAN_DO_IT:
                os.made_up_call()
        """
        self.assertEqual(codes(source), [])

    def test_platform_specific_module(self):
        source = """
            import sys, os
            if sys.platform == "made_up_os":
                raise ImportError("not supported here")
            os.made_up_call()
        """
        self.assertEqual(codes(source), [])

    def test_unguarded_code_after_a_guard_is_still_checked(self):
        source = """
            import os, sys
            if sys.platform == "made_up_os":
                os.made_up_call()
            os.path.exist("x")
        """
        self.assertEqual(codes(source), ["API002"])

    def test_rebound_name_is_not_tracked(self):
        self.assertEqual(codes("import json\njson = make_thing()\njson.parse\n"), [])

    def test_ignore_comment(self):
        self.assertEqual(codes("import json\njson.parse('{}')  # pipbreak: ignore\n"), [])

    def test_local_modules_are_skipped(self):
        self.assertEqual(codes("import myproject.thing\n", local={"myproject"}), [])

    def test_relative_imports_are_skipped(self):
        self.assertEqual(codes("from . import thing\nfrom .x import y\n"), [])

    def test_attribute_assignment_is_not_checked(self):
        self.assertEqual(codes("import types\ntypes.brand_new = 1\n"), [])


class Deprecations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        Path(cls.tmp.name, "oldlib.py").write_text(textwrap.dedent('''
            try:
                from warnings import deprecated
            except ImportError:  # Python < 3.13: the PEP 702 marker is all that matters here
                def deprecated(message):
                    def mark(obj):
                        obj.__deprecated__ = message
                        return obj
                    return mark

            @deprecated("use new_func instead")
            def old_func(): pass

            def sphinx_old():
                """Do a thing.

                .. deprecated:: 2.0
                """

            def fine(): pass

            def param_deprecated(objects=None):
                """Flush things.

                :param objects:
                    .. deprecated:: 2.0
                        Pass nothing instead.
                """

            def call_style(a, maxiter=None):
                """Solve.

                .. deprecated:: 1.18.0
                    Use of argument(s) ``{'maxiter'}`` by position is deprecated.
                """

            def with_option(sql):
                """Make a statement.

                .. deprecated:: 1.4
                    The "autocommit" execution option is deprecated.
                """

            class Client:
                @deprecated("use send() instead")
                def post(self): pass
                def send(self, data, *, timeout=None): pass

            def make() -> Client:
                return Client()

            class _Proxy:
                def __getattribute__(self, name):
                    raise RuntimeError("Working outside of request context.")
                def __call__(self):
                    pass

            proxy = _Proxy()
        '''))
        sys.path.insert(0, cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(cls.tmp.name)
        cls.tmp.cleanup()

    def test_pep702_marker(self):
        findings = check_source("import oldlib\noldlib.old_func()\n")
        self.assertEqual([(f.code, f.severity) for f in findings], [("API101", "warning")])
        self.assertIn("use new_func instead", findings[0].message)

    def test_sphinx_note_and_clean_function(self):
        self.assertEqual(codes("from oldlib import sphinx_old, fine\n"), ["API101"])

    def test_deprecated_method_via_annotation(self):
        source = """
            import oldlib
            def f(client: oldlib.Client):
                client.post()
                client.send(1, timout=3)
                client.not_checked_because_annotated
        """
        self.assertEqual(codes(source), ["API101", "API003"])

    def test_deprecated_parameter_is_not_a_deprecated_function(self):
        self.assertEqual(codes("from oldlib import param_deprecated\n"), [])

    def test_deprecated_calling_style_is_not_a_deprecated_function(self):
        self.assertEqual(codes("from oldlib import call_style, with_option\n"), [])

    def test_sphinx_note_text_is_reported(self):
        [finding] = check_source("from oldlib import sphinx_old\n")
        self.assertIn("2.0", finding.message)

    def test_objects_that_raise_when_inspected(self):
        source = """
            import oldlib
            from oldlib import proxy
            proxy.args.get("x")
            oldlib.proxy.method(1, 2)
            proxy()
        """
        self.assertEqual(codes(source), [])

    def test_return_annotation(self):
        self.assertEqual(codes("import oldlib\noldlib.make().post()\n"), ["API101"])

    def test_deprecated_method_inherited_by_local_class(self):
        source = """
            import oldlib
            class MyClient(oldlib.Client):
                def go(self):
                    self.post()
        """
        self.assertEqual(codes(source), ["API101"])


class VersionFallbacks(unittest.TestCase):
    def test_except_import_error_fallback(self):
        source = "try:\n    from os import newname\nexcept ImportError:\n    from os import oldname\n"
        self.assertEqual(codes(source), [])

    def test_library_version_branch(self):
        source = ("import os\nif np_version >= parse_version('2'):\n    os.path.newname()\n"
                  "else:\n    os.path.oldname()\n")
        self.assertEqual(codes(source), [])

    def test_unrelated_except_still_checked(self):
        source = "try:\n    pass\nexcept ValueError:\n    from os import oldname\n"
        self.assertEqual(codes(source), ["API002"])


def install(site: Path, dist: str, version: str, files: dict[str, str]):
    """Lay out a distribution the way pip does: its files plus a .dist-info with RECORD."""
    for name, source in files.items():
        Path(site, name).parent.mkdir(parents=True, exist_ok=True)
        Path(site, name).write_text(textwrap.dedent(source))
    info = Path(site, f"{dist}-{version}.dist-info")
    info.mkdir()
    Path(info, "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n")
    Path(info, "RECORD").write_text("".join(f"{name},,\n" for name in files))


class InstalledPackages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        site = Path(cls.tmp.name)
        install(site, "Lib-New", "3.0", {"libnew/__init__.py": "def keep(): pass\n"})
        install(site, "consumer", "1.0", {
            "consumer/__init__.py": """
                import os
                import libnew
                import optional_extra
                from consumer import helpers
                libnew.keep()
                libnew.gone()
                os.path.nope()
                helpers.nothere()
            """,
            "consumer/helpers.py": "",
        })
        install(site, "bystander", "1.0", {"bystander.py": "import os\nos.path.nope()\n"})
        install(site, "vendorer", "1.0", {
            "vendorer/__init__.py": "",
            "vendorer/_vendor/libnew/__init__.py": "import libnew\nlibnew.vendored_only()\n",
            "vendorer/tests/test_it.py": "import libnew\nlibnew.only_in_old_versions()\n",
        })
        sys.path.insert(0, str(site))

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(cls.tmp.name)
        for name in ("libnew", "consumer", "consumer.helpers", "optional_extra", "bystander", "vendorer"):
            sys.modules.pop(name, None)
        cls.tmp.cleanup()

    def test_only_calls_into_other_packages(self):
        findings, count = check_installed({"libnew"})
        # consumer's two files; bystander doesn't import libnew; vendorer's vendored copy and tests are skipped
        self.assertEqual(count, 2)
        self.assertEqual([(f.path, f.code, f.target) for f in findings],
                         [("consumer/__init__.py", "API002", "libnew")])
        self.assertTrue(findings[0].message.startswith("consumer==1.0: 'libnew' has no attribute 'gone'"))

    def test_modules_found_without_top_level_txt(self):
        # flit-built wheels (Jinja2 3.1, Werkzeug 3) ship no top_level.txt; Python < 3.11 can't map them.
        dists = packages_distributions()
        self.assertEqual(dists["libnew"], ["Lib-New"])
        self.assertEqual(dists["bystander"], ["bystander"])

    def test_distribution_name_selects_its_modules(self):
        self.assertEqual(len(check_installed({"lib-new"})[0]), 1)


class Compare(unittest.TestCase):
    BREAK = {"path": "flask/testing.py", "line": 116, "col": 44, "severity": "error", "code": "API002",
             "message": "Flask==2.2.5: 'werkzeug' has no attribute '__version__' in Werkzeug==3.1.9."}

    def test_changed_distributions(self):
        base = {"werkzeug": ("Werkzeug", "2.3.8"), "flask": ("Flask", "2.2.5"), "gone": ("Gone", "1")}
        head = {"werkzeug": ("Werkzeug", "3.1.9"), "flask": ("Flask", "2.2.5"), "fresh": ("Fresh", "2")}
        self.assertEqual(compare.changed_distributions(base, head),
                         [("Fresh", None, "2"), ("Gone", "1", None), ("Werkzeug", "2.3.8", "3.1.9")])

    def test_only_new_findings_count(self):
        old = dict(self.BREAK, line=110, message=self.BREAK["message"].replace("2.2.5", "2.2.4"))
        self.assertEqual(compare.introduced([old], [self.BREAK]), [])  # same problem, moved and re-versioned
        self.assertEqual(compare.introduced([], [self.BREAK]), [self.BREAK])

    def test_markdown_report(self):
        report = compare.markdown([("Werkzeug", "2.3.8", "3.1.9")], [self.BREAK])
        self.assertTrue(report.startswith(compare.MARKER))
        self.assertIn("breaks 1 call(s)", report)
        self.assertIn("Werkzeug 2.3.8 → 3.1.9", report)
        self.assertIn("| Flask 2.2.5 | `flask/testing.py:116` | 'werkzeug' has no attribute '__version__'", report)
        self.assertIn("no installed package uses an API", compare.markdown([], []))


class Action(unittest.TestCase):
    def test_action_yml_is_valid_yaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        action = yaml.safe_load(Path(__file__).parent.parent.joinpath("action.yml").read_text(encoding="utf-8"))
        self.assertEqual(action["runs"]["using"], "composite")
        self.assertTrue(all(isinstance(v["description"], str) for v in action["inputs"].values()))


class Cli(unittest.TestCase):
    def test_json_survives_a_package_that_prints_on_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            site, project = Path(tmp, "site"), Path(tmp, "project")
            site.mkdir(), project.mkdir()
            Path(site, "chatty.py").write_text("print('Hello from chatty')\ndef hi(): pass\n")
            Path(project, "app.py").write_text("import chatty\nchatty.hi()\nchatty.bye()\n")
            sys.path.insert(0, str(site))
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    main(["--format", "json", str(project)])
            finally:
                sys.path.remove(str(site))
                sys.modules.pop("chatty", None)
        self.assertEqual([f["code"] for f in json.loads(out.getvalue())], ["API002"])

    def test_file_encodings(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "latin.py").write_bytes(b"# -*- coding: latin-1 -*-\nimport os\nos.path.exist('\xa4')\n")
            Path(tmp, "broken.py").write_bytes(b"x = '\xa4'\n")
            findings, count = check_paths([Path(tmp)])
        self.assertEqual(count, 2)
        self.assertEqual(sorted((Path(f.path).name, f.code) for f in findings),
                         [("broken.py", "API000"), ("latin.py", "API002")])


if __name__ == "__main__":
    unittest.main()

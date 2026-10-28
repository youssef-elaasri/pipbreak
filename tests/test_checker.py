import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from apicheck import check_source


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
        self.assertEqual(codes("from pathlib import Path\nPath('x').read_text('utf-8', 'strict', None)\n"), [])
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
            from somewhere_unknown_pkg import Base  # apicheck: ignore
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
        self.assertEqual(codes("import json\njson.parse('{}')  # apicheck: ignore\n"), [])

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
            from warnings import deprecated

            @deprecated("use new_func instead")
            def old_func(): pass

            def sphinx_old():
                """Do a thing.

                .. deprecated:: 2.0
                """

            def fine(): pass

            class Client:
                @deprecated("use send() instead")
                def post(self): pass
                def send(self, data, *, timeout=None): pass

            def make() -> Client:
                return Client()
        '''))
        sys.path.insert(0, cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(cls.tmp.name)
        cls.tmp.cleanup()

    @unittest.skipIf(sys.version_info < (3, 13), "warnings.deprecated is new in 3.13")
    def test_pep702_marker(self):
        findings = check_source("import oldlib\noldlib.old_func()\n")
        self.assertEqual([(f.code, f.severity) for f in findings], [("API101", "warning")])
        self.assertIn("use new_func instead", findings[0].message)

    @unittest.skipIf(sys.version_info < (3, 13), "warnings.deprecated is new in 3.13")
    def test_sphinx_note_and_clean_function(self):
        self.assertEqual(codes("from oldlib import sphinx_old, fine\n"), ["API101"])

    @unittest.skipIf(sys.version_info < (3, 13), "warnings.deprecated is new in 3.13")
    def test_deprecated_method_via_annotation(self):
        source = """
            import oldlib
            def f(client: oldlib.Client):
                client.post()
                client.send(1, timout=3)
                client.not_checked_because_annotated
        """
        self.assertEqual(codes(source), ["API101", "API003"])

    @unittest.skipIf(sys.version_info < (3, 13), "warnings.deprecated is new in 3.13")
    def test_return_annotation(self):
        self.assertEqual(codes("import oldlib\noldlib.make().post()\n"), ["API101"])

    @unittest.skipIf(sys.version_info < (3, 13), "warnings.deprecated is new in 3.13")
    def test_deprecated_method_inherited_by_local_class(self):
        source = """
            import oldlib
            class MyClient(oldlib.Client):
                def go(self):
                    self.post()
        """
        self.assertEqual(codes(source), ["API101"])


if __name__ == "__main__":
    unittest.main()

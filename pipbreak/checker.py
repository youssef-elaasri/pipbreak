"""Flag uses of APIs that don't exist (or are deprecated) in the *installed* package versions.

The source is parsed statically, but every imported name, attribute chain and call signature
is checked against the real modules importable from the running interpreter - so the answer
depends on exactly what is installed, e.g. pydantic 1.x vs 2.x.

Beyond module-level names, a light flow-insensitive inference tracks what variables hold:
`p = Path(x)` makes `p` a Path, `def f(u: User)` makes `u` a User, and classes defined in the
checked file are modelled from their source, so `p.exist()` and `user.dict()` get checked too.
"""
from __future__ import annotations

import ast
import builtins
import difflib
import importlib
import importlib.metadata
import inspect
import re
import sys
import textwrap
import tokenize
import typing
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path

IGNORE_MARKER = "# pipbreak: ignore"
GUARD_ERRORS = {"ImportError", "ModuleNotFoundError", "AttributeError", "NameError", "TypeError",
                "Exception", "BaseException"}
FALLBACK_ERRORS = {"ImportError", "ModuleNotFoundError", "AttributeError"}
SAFE_CLASS_DECORATORS = {"dataclass", "total_ordering", "final", "unique", "runtime_checkable"}
PLAIN_METACLASSES = {"type", "ABCMeta"}

MISSING = object()  # attribute does not exist
UNKNOWN = object()  # module exists but could not be imported; don't guess
UNTYPED = object()  # a name in scope whose value we can't infer


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    col: int
    severity: str  # "error" or "warning"
    code: str
    message: str
    target: str = ""  # module whose API this is about; empty for the checked code's own classes


@dataclass(frozen=True)
class Removed:
    """Accessing the attribute raised something other than AttributeError (e.g. a "moved to" error)."""

    reason: str


@dataclass(eq=False)
class LocalClass:
    """A class defined in the file being checked, modelled from its source."""

    node: ast.ClassDef
    name: str
    bases: list = field(default_factory=list)   # real classes or LocalClass
    attrs: set = field(default_factory=set)     # class-body names plus attributes assigned anywhere in it
    closed: bool = True  # False when attributes may come from somewhere we can't see


@dataclass(frozen=True)
class Instance:
    """An object of class `cls` (a real class or a LocalClass).

    `exact` means we saw it constructed, so it can't be a subclass with extra attributes - only
    then do we report attributes as missing. Annotated or returned values only get deprecation
    and signature checks.
    """

    cls: object
    exact: bool


@dataclass(frozen=True)
class BoundMethod:
    func: object
    drop_first: bool  # signature still includes self


class Environment:
    """Resolves modules and attributes against whatever is installed in this interpreter."""

    def __init__(self):
        self._modules: dict[str, object] = {}
        self._dists: dict[str, list[str]] | None = None

    def module(self, name: str):
        """Return the module, None if it doesn't exist, or UNKNOWN if it exists but failed to import."""
        if name not in self._modules:
            try:
                self._modules[name] = importlib.import_module(name)
            except ModuleNotFoundError as exc:
                missing = exc.name or ""
                is_ours = name == missing or name.startswith(missing + ".")
                self._modules[name] = None if is_ours else UNKNOWN
            except BaseException:  # the module itself crashed on import; not our call to make
                self._modules[name] = UNKNOWN
        return self._modules[name]

    def member(self, obj, name: str):
        """Look up `obj.name`, falling back to importing a submodule when obj is a package."""
        try:
            return getattr(obj, name)
        except AttributeError:
            pass
        except Exception as exc:
            return Removed(str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__)
        if inspect.ismodule(obj):
            sub = self.module(f"{obj.__name__}.{name}")
            if sub is not None and sub is not UNKNOWN:
                return sub
        return MISSING

    def label(self, module_name: str) -> str:
        """Human-readable version of whatever provides module_name, e.g. 'pydantic==2.9.2'."""
        top = module_name.split(".")[0]
        if top in sys.stdlib_module_names or top == "builtins":
            return f"Python {sys.version_info.major}.{sys.version_info.minor}"
        if self._dists is None:
            self._dists = packages_distributions()
        for dist in self._dists.get(top, []):
            try:
                return f"{dist}=={importlib.metadata.version(dist)}"
            except importlib.metadata.PackageNotFoundError:
                continue
        return top


def packages_distributions() -> dict[str, list[str]]:
    """Top-level module -> distributions providing it.

    importlib.metadata's version only reads top_level.txt before Python 3.11, and wheels built
    with flit (Jinja2 3.1, Werkzeug 3) don't have one - so infer from RECORD, as 3.11 does.
    """
    found: dict[str, list[str]] = defaultdict(list)
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        tops = set((dist.read_text("top_level.txt") or "").split())
        for path in dist.files or ():
            if len(path.parts) == 1 and path.suffix == ".py":
                tops.add(path.stem)
            elif len(path.parts) > 1 and "." not in path.parts[0] and path.parts[0] != "__pycache__":
                tops.add(path.parts[0])
        for top in tops:
            if name not in found[top]:
                found[top].append(name)
    return dict(found)


def owner_module(obj) -> str:
    if inspect.ismodule(obj):
        return obj.__name__
    return getattr(obj, "__module__", None) or "builtins"


def deprecation_note(obj) -> str | None:
    """PEP 702 `@deprecated` marker or a Sphinx `.. deprecated::` note on the object itself."""
    try:
        return _deprecation_note(obj)
    except Exception:  # live proxies (flask.request & co.) raise when inspected out of context
        return None


def _deprecation_note(obj) -> str | None:
    try:
        own = vars(obj)
    except TypeError:
        own = {}
    note = own.get("__deprecated__")
    if isinstance(note, str):
        return note or "deprecated"
    doc = own.get("__doc__") if inspect.isclass(obj) else getattr(obj, "__doc__", None)
    if not isinstance(doc, str):
        return None
    lines = inspect.cleandoc(doc).splitlines()
    if not lines:
        return None
    # typing.List & co. are "deprecated aliases" with no removal planned; everyone still uses them.
    if lines[0].startswith("Deprecated alias") and owner_module(obj) in ("typing", "typing_extensions"):
        return None
    if lines[0].lower().startswith("deprecated"):
        return lines[0]
    # A module docstring documents its members too (numpy.typing: "NBitBase is deprecated").
    if inspect.ismodule(obj):
        return None
    # Only a top-level directive; an indented one belongs to a parameter, not the object.
    for i, line in enumerate(lines):
        if line.startswith(".. deprecated::"):
            body = [line.removeprefix(".. deprecated::").strip()]
            body += [rest.strip() for rest in lines[i + 1:i + 4] if rest.startswith((" ", "\t"))]
            note = " ".join(filter(None, body))
            # "Use of argument(s) {'maxiter'} by position is deprecated", "The "autocommit" execution
            # option is deprecated" - a way of calling it, not the object itself.
            if re.search(r"\b(argument|parameter|keyword|option)", note, re.I) or quotes_a_parameter(obj, note):
                return None
            return note
    return None


def quotes_a_parameter(obj, note: str) -> bool:
    """attrs.define: "*hash* is deprecated in favor of *unsafe_hash*"; sqlalchemy.orm.join: "a string
    relationship name for the "onclause" is deprecated" - the note names one of its parameters."""
    try:
        params = inspect.signature(obj).parameters
    except (TypeError, ValueError):
        return False
    quoted = set(re.findall(r"""[*`"']+(\w+)[*`"']+""", note))
    return bool(quoted & set(params))


def suggest(name: str, candidates) -> str:
    public = [c for c in candidates if not c.startswith("_")]
    match = difflib.get_close_matches(name, public, n=1, cutoff=0.6)
    return f" Did you mean '{match[0]}'?" if match else ""


def moved_to(owner, name: str, what: str) -> str:
    """Names often move into a submodule (collections.Mapping -> collections.abc.Mapping)."""
    if not inspect.ismodule(owner) or name.startswith("_"):
        return ""
    for attr in dir(owner):
        if attr.startswith("_"):
            continue
        try:
            sub = getattr(owner, attr, None)
            if inspect.ismodule(sub) and sub.__name__ == f"{owner.__name__}.{attr}" and hasattr(sub, name):
                return f" Did you mean '{what}.{attr}.{name}'?"
        except Exception:  # lazy attributes import on access (six.moves.tkinter without Tk)
            continue
    return ""


def decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return decorator_name(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


# -- what instances of a real class can have --------------------------------------------------

def mro_without_object(cls) -> list:
    return [c for c in getattr(cls, "__mro__", ()) if c is not object]


BUILTIN_PROXIES = {super, type, type(sys)}


def custom_hook(cls, hook: str) -> bool:
    """Whether the class overrides attribute lookup. Many builtin types (dict, list) expose the
    generic lookup in their own __dict__, which doesn't count."""
    for c in mro_without_object(cls):
        if hook in vars(c) and (c.__module__ != "builtins" or c in BUILTIN_PROXIES):
            return True
    return False


def is_proxy(cls) -> bool:
    """__getattribute__ overrides every lookup (super, proxies), so the class tells us nothing."""
    return custom_hook(cls, "__getattribute__")


def on_self(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id in ("self", "cls")


def is_self_dict(node: ast.AST) -> bool:
    """`self.__dict__` or `vars(self)`."""
    if isinstance(node, ast.Attribute):
        return node.attr == "__dict__" and on_self(node.value)
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "vars"
            and bool(node.args) and on_self(node.args[0]))


def assigned_attrs(tree: ast.AST) -> set[str] | None:
    """Attribute names a class's source assigns, or None if it sets its own attributes dynamically."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "setattr" and node.args and on_self(node.args[0]):
                return None
            if isinstance(func, ast.Attribute) and func.attr == "__setattr__":
                return None
            # self.__dict__.update(...) / vars(self).setdefault(...)
            if isinstance(func, ast.Attribute) and func.attr in ("update", "setdefault") and is_self_dict(func.value):
                return None
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) and is_self_dict(node.value):
            return None
        if isinstance(node, ast.Attribute):
            if node.attr == "__dict__" and on_self(node.value) and isinstance(node.ctx, ast.Store):
                return None
            if isinstance(node.ctx, ast.Store):
                names.add(node.attr)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


_instance_attrs_cache: dict = {}


def instance_attrs(cls) -> tuple[set[str], bool] | None:
    """Attribute names an instance of cls can have, and whether that list is certain.

    Certain when instances have no __dict__ (__slots__ or C types). Otherwise it's read from the
    attribute assignments in the class source. None when we can't tell.
    """
    try:
        return _instance_attrs_cache[cls]
    except (KeyError, TypeError):
        pass
    result = _instance_attrs(cls)
    try:
        _instance_attrs_cache[cls] = result
    except TypeError:
        pass
    return result


def _instance_attrs(cls):
    mro = mro_without_object(cls)
    if custom_hook(cls, "__getattr__") or custom_hook(cls, "__getattribute__"):
        return None
    names = set(dir(cls))
    if getattr(cls, "__dictoffset__", 1) == 0:
        return names, True
    for c in mro:
        if c.__module__ == "builtins":
            continue
        try:
            found = assigned_attrs(ast.parse(textwrap.dedent(inspect.getsource(c))))
        except (OSError, TypeError, SyntaxError):
            return None
        if found is None:
            return None
        names |= found
    for attr in ("__dataclass_fields__", "__attrs_attrs__", "_fields", "model_fields", "__fields__"):
        declared = getattr(cls, attr, None)
        if isinstance(declared, dict):
            names |= set(declared)
        elif isinstance(declared, (tuple, list)):
            names |= {d if isinstance(d, str) else getattr(d, "name", "") for d in declared}
    return names, False


def bind(cls, attr: str, value):
    """What `instance.attr` gives: a bound method, a property's annotated type, or unknown."""
    raw = next((vars(c)[attr] for c in getattr(cls, "__mro__", ()) if attr in vars(c)), MISSING)
    if isinstance(raw, (staticmethod, classmethod)):
        return BoundMethod(value, drop_first=False)
    if isinstance(raw, property):
        return return_type(raw.fget) if raw.fget else None
    if inspect.isfunction(raw) or inspect.ismethoddescriptor(raw):
        return BoundMethod(value, drop_first=True)
    return None


_return_cache: dict = {}


def return_type(func):
    """Instance of the function's annotated return class, if it has a plain one."""
    try:
        if func in _return_cache:
            return _return_cache[func]
    except TypeError:
        return None
    try:
        ret = typing.get_type_hints(func).get("return")
    except Exception:
        ret = None
    result = Instance(ret, exact=False) if inspect.isclass(ret) and ret is not type(None) else None
    _return_cache[func] = result
    return result


def plain_metaclass(cls) -> bool:
    """Custom metaclasses (ORMs, pydantic, enums) add attributes we can't see in the source."""
    return all(type(c).__name__ in PLAIN_METACLASSES for c in getattr(cls, "__mro__", ()))


# -- guards: code that only runs where an API is known to exist -------------------------------

def is_feature_probe(test: ast.AST, flags: set[str] = frozenset()) -> bool:
    """Conditions that mean "this code only runs where the API exists": hasattr, platform, version...

    `flags` are names previously assigned from such a condition, e.g. `HAVE_FORK = hasattr(os, "fork")`.
    """
    for n in ast.walk(test):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("hasattr", "getattr"):
            return True
        if isinstance(n, ast.Name) and n.id in flags | {"TYPE_CHECKING"}:
            return True
        if isinstance(n, ast.Attribute) and n.attr in flags:
            return True
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            if n.attr == "TYPE_CHECKING":
                return True
            if n.value.id == "sys" and n.attr in ("platform", "version_info", "implementation"):
                return True
            if (n.value.id, n.attr) == ("os", "name") or n.value.id == "platform":
                return True
        # Library version checks: `np_version >= parse_version("1.25")`, `_vers >= (2,)`,
        # `werkzeug_major < 2`...
        if isinstance(n, ast.Compare) and any(is_version_operand(o) for o in (n.left, *n.comparators)):
            return True
    return False


VERSION_WORDS = {"version", "vers", "ver", "major", "minor"}


def is_version_operand(node: ast.AST) -> bool:
    if (isinstance(node, ast.Tuple) and node.elts
            and all(isinstance(e, ast.Constant) and type(e.value) is int for e in node.elts)):
        return True  # (2,), (1, 4)
    for v in ast.walk(node):
        name = (getattr(v, "id", None) or getattr(v, "attr", None) or "").lower()
        if "version" in name or VERSION_WORDS & set(name.split("_")):
            return True
    return False


def caught_names(node: ast.Try) -> set[str]:
    caught = set()
    for handler in node.handlers:
        if handler.type is None:
            caught.add("BaseException")
        for t in ast.walk(handler.type) if handler.type else ():
            if isinstance(t, ast.Name):
                caught.add(t.id)
            elif isinstance(t, ast.Attribute):
                caught.add(t.attr)
    return caught


def feature_flags(tree: ast.Module) -> set[str]:
    """Names assigned from a feature probe, followed through chains like `B = A and x`."""
    assigns = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value]
    flags: set[str] = set()
    while True:
        found = set()
        for node in assigns:
            if is_feature_probe(node.value, flags):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        found.add(t.id)
                    elif isinstance(t, ast.Attribute):
                        found.add(t.attr)
        if found <= flags:
            return flags
        flags |= found


def guarded_nodes(tree: ast.Module) -> set[ast.AST]:
    """Nodes that only run when an API is known to exist, so a missing API there is not a bug."""
    flags = feature_flags(tree)
    # A module that bails out early on the wrong platform/version is entirely conditional.
    for stmt in tree.body:
        if (isinstance(stmt, ast.If) and is_feature_probe(stmt.test, flags)
                and any(isinstance(n, ast.Raise) for n in ast.walk(stmt))):
            return set(ast.walk(tree))
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp, ast.While)) and is_feature_probe(node.test, flags):
            guarded.update(ast.walk(node))
        elif isinstance(node, ast.BoolOp) and any(is_feature_probe(v, flags) for v in node.values):
            guarded.update(ast.walk(node))
        elif isinstance(node, ast.Try) and caught_names(node) & GUARD_ERRORS:
            for stmt in node.body:
                guarded.update(ast.walk(stmt))
            # `except ImportError: from numpy import RankWarning` - the fallback for other versions.
            for handler in node.handlers:
                if FALLBACK_ERRORS & {getattr(t, "id", None) or getattr(t, "attr", None)
                                      for t in (ast.walk(handler.type) if handler.type else ())}:
                    guarded.update(ast.walk(handler))
    return guarded


def store_counts(tree: ast.AST) -> Counter:
    """How often each name is bound by something other than an import."""
    counts = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            counts[node.id] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            counts[node.name] += 1
        elif isinstance(node, ast.arg):
            counts[node.arg] += 1
        elif isinstance(node, ast.ExceptHandler) and node.name:
            counts[node.name] += 1
    return counts


def walk_scope(body: list[ast.AST]):
    """ast.walk over one scope's statements (parents first), not descending into nested scopes."""
    stack = list(reversed(body))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(reversed(list(ast.iter_child_nodes(node))))


def same(values: list) -> object:
    first = values[0]
    if first is None or first is UNTYPED:
        return UNTYPED
    for v in values[1:]:
        if not (v is first or (type(v) is Instance and v == first)):
            return UNTYPED
    return first


class FileChecker(ast.NodeVisitor):
    def __init__(self, env: Environment, path: str, source: str, local: set[str]):
        self.env = env
        self.path = path
        self.lines = source.splitlines()
        self.local = local
        self.tree = ast.parse(source, filename=path)
        self.bindings: dict[str, object] = {}
        self.local_classes: dict[str, LocalClass] = {}
        self.scopes: list[dict] = []
        self.class_stack: list[LocalClass | None] = []
        self.findings: list[Finding] = []
        self.guarded = guarded_nodes(self.tree)
        self.stores = store_counts(self.tree)
        self.use_builtins = not any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
                                    for n in ast.walk(self.tree))
        self.quiet = 0  # >0 while inferring scopes: resolve without reporting
        self._memo: dict[int, object] = {}
        self._seen: set[tuple] = set()

    def run(self) -> list[Finding]:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                self.check_import(node, node in self.guarded)
            elif isinstance(node, ast.ImportFrom):
                self.check_import_from(node, node in self.guarded)
        for name in self.stores:
            self.bindings.pop(name, None)
        self.build_local_classes()
        self.visit(self.tree)
        return sorted(self.findings, key=lambda f: (f.line, f.col))

    # -- reporting ------------------------------------------------------------------------

    def report(self, node: ast.AST, severity: str, code: str, message: str, target: str = ""):
        if self.quiet:
            return
        line = self.lines[node.lineno - 1] if node.lineno <= len(self.lines) else ""
        key = (node.lineno, node.col_offset, code, message)
        if IGNORE_MARKER in line or key in self._seen or (code != "API101" and node in self.guarded):
            return
        self._seen.add(key)
        self.findings.append(Finding(self.path, node.lineno, node.col_offset + 1, severity, code, message,
                                     target))

    def report_missing_module(self, node: ast.AST, name: str):
        parent, _, child = name.rpartition(".")
        parent_mod = self.env.module(parent) if parent else None
        if parent_mod is None or parent_mod is UNKNOWN:
            top = name.split(".")[0]
            self.report(node, "error", "API001", f"package '{top}' is not installed in this environment")
        else:
            hint = suggest(child, dir(parent_mod))
            self.report(node, "error", "API001",
                        f"module '{name}' does not exist in {self.env.label(parent)}.{hint}", parent)

    def report_missing_member(self, node: ast.AST, owner, name: str, value, what: str):
        module = owner_module(owner)
        if re.search(r"(^|\.)six\.moves\b", module):  # its contents depend on the platform (winreg)
            return
        where = self.env.label(module)
        if type(value) is Removed:
            self.report(node, "error", "API002", f"'{what}.{name}' is not available in {where}: {value.reason}",
                        module)
        else:
            hint = moved_to(owner, name, what) or suggest(name, dir(owner))
            self.report(node, "error", "API002", f"'{what}' has no attribute '{name}' in {where}.{hint}", module)

    def check_deprecated(self, node: ast.AST, value, display: str):
        note = deprecation_note(value)
        if note:
            module = owner_module(value)
            self.report(node, "warning", "API101",
                        f"'{display}' is deprecated in {self.env.label(module)}: {note}", module)

    # -- imports --------------------------------------------------------------------------

    def is_local(self, module_name: str) -> bool:
        return module_name.split(".")[0] in self.local

    def check_import(self, node: ast.Import, guarded: bool):
        for alias in node.names:
            if self.is_local(alias.name):
                continue
            mod = self.env.module(alias.name)
            if mod is None:
                if not guarded:
                    self.report_missing_module(node, alias.name)
                continue
            if mod is UNKNOWN:
                continue
            if alias.asname:
                self.bindings[alias.asname] = mod
            else:
                top = alias.name.split(".")[0]
                top_mod = self.env.module(top)
                if top_mod is not None and top_mod is not UNKNOWN:
                    self.bindings[top] = top_mod

    def check_import_from(self, node: ast.ImportFrom, guarded: bool):
        if node.level or not node.module or self.is_local(node.module):
            return
        mod = self.env.module(node.module)
        if mod is None:
            if not guarded:
                self.report_missing_module(node, node.module)
            return
        if mod is UNKNOWN:
            return
        for alias in node.names:
            if alias.name == "*":
                continue
            value = self.env.member(mod, alias.name)
            if value is MISSING or type(value) is Removed:
                if not guarded:
                    self.report_missing_member(node, mod, alias.name, value, node.module)
                continue
            self.check_deprecated(node, value, f"{node.module}.{alias.name}")
            self.bindings[alias.asname or alias.name] = value

    # -- classes defined in this file -----------------------------------------------------

    def build_local_classes(self):
        defs = [n for n in ast.walk(self.tree) if isinstance(n, ast.ClassDef)]
        defined = Counter(d.name for d in defs)
        for d in defs:
            # Only classes whose name means exactly one thing in this file.
            if defined[d.name] == 1 and self.stores[d.name] == 1:
                self.local_classes[d.name] = LocalClass(d, d.name)
        self.quiet += 1
        try:
            for local in self.local_classes.values():
                self.model_local_class(local)
        finally:
            self.quiet -= 1

    def model_local_class(self, local: LocalClass):
        d = local.node
        for n in walk_scope(d.body):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local.attrs.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                local.attrs.add(n.id)
        dynamic = assigned_attrs(d)
        local.attrs |= dynamic or set()
        if (dynamic is None or {"__getattr__", "__getattribute__"} & local.attrs or d.keywords
                or any(decorator_name(x) not in SAFE_CLASS_DECORATORS for x in d.decorator_list)):
            local.closed = False
        for base in d.bases:
            resolved = self.resolve(base)
            if type(resolved) is LocalClass or inspect.isclass(resolved):
                local.bases.append(resolved)
                if inspect.isclass(resolved) and not plain_metaclass(resolved):
                    local.closed = False
            else:
                local.closed = False

    def lookup_local(self, local: LocalClass, attr: str, instance: bool, seen=None):
        """Find attr on a local class. Returns (state, value, owner) with state found/unknown/missing."""
        seen = seen if seen is not None else set()
        if id(local) in seen or attr in local.attrs or attr.startswith("__"):
            return "unknown", None, None
        seen.add(id(local))
        certain = local.closed
        for base in local.bases:
            if type(base) is LocalClass:
                state, value, owner = self.lookup_local(base, attr, instance, seen)
                if state != "missing":
                    return state, value, owner
                continue
            value = self.env.member(base, attr)
            if type(value) is Removed:
                return "unknown", None, None
            if value is not MISSING:
                return "found", value, base
            if instance:
                known = instance_attrs(base)
                if known is None or attr in known[0]:
                    certain = False
        if hasattr(object, attr):
            return "unknown", None, None
        return ("missing" if certain else "unknown"), None, None

    def local_candidates(self, local: LocalClass, seen=None) -> set[str]:
        seen = seen if seen is not None else set()
        if id(local) in seen:
            return set()
        seen.add(id(local))
        names = set(local.attrs)
        for base in local.bases:
            names |= self.local_candidates(base, seen) if type(base) is LocalClass else set(dir(base))
        return names

    # -- scopes: what each name holds -----------------------------------------------------

    def scope_types(self, body, args=None, owner=None, decorators=()) -> dict:
        """Infer each name bound in a scope; a name bound to different things becomes UNTYPED."""
        scope: dict = {}
        self.scopes.append(scope)
        self.quiet += 1
        try:
            # A few passes so `p = Path(x); q = p.parent` can see p, without trusting outer scopes.
            for _ in range(3):
                sources = self.scope_sources(body, args, owner, decorators)
                result = {name: same(values) for name, values in sources.items()}
                scope.clear()
                scope.update(result)
        finally:
            self.quiet -= 1
            self.scopes.pop()
        return scope

    def scope_sources(self, body, args, owner, decorators) -> dict[str, list]:
        sources: dict[str, list] = defaultdict(list)
        if args is not None:
            positional = args.posonlyargs + args.args
            for a in positional + args.kwonlyargs:
                sources[a.arg].append(self.annotation_type(a.annotation))
            for a in (args.vararg, args.kwarg):
                if a:
                    sources[a.arg].append(UNTYPED)
            if owner is not None and positional and positional[0].annotation is None:
                # `self` may be a subclass instance that fills in hooks, so it's never exact. `cls`
                # may be a subclass too; we don't model "some subclass of", so leave it unknown.
                kinds = {decorator_name(d) for d in decorators}
                if not kinds & {"staticmethod", "classmethod"}:
                    sources[positional[0].arg] = [Instance(owner, exact=False)]
        handled: set[int] = set()
        for node in walk_scope(body):
            if isinstance(node, ast.Assign):
                value = self.resolve(node.value)
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        handled.add(id(target))
                        sources[target.id].append(value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                handled.add(id(node.target))
                value = self.annotation_type(node.annotation)
                if value is UNTYPED and node.value is not None:
                    value = self.resolve(node.value)
                sources[node.target.id].append(value)
            elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                if id(node) not in handled:
                    sources[node.id].append(UNTYPED)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sources[node.name].append(UNTYPED)
            elif isinstance(node, ast.ClassDef):
                local = self.local_classes.get(node.name)
                sources[node.name].append(local if local is not None and local.node is node else UNTYPED)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    if name != "*":
                        sources[name].append(self.bindings.get(name, UNTYPED))
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                for name in node.names:
                    sources[name].append(UNTYPED)
            elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
                sources[node.name].append(UNTYPED)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                sources[node.rest].append(UNTYPED)
        return sources

    def annotation_type(self, ann):
        if ann is None:
            return UNTYPED
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            try:
                return self.annotation_type(ast.parse(ann.value, mode="eval").body)
            except SyntaxError:
                return UNTYPED
        if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
            sides = [s for s in (ann.left, ann.right) if not (isinstance(s, ast.Constant) and s.value is None)]
            return self.annotation_type(sides[0]) if len(sides) == 1 else UNTYPED
        if isinstance(ann, ast.Subscript) and self.resolve(ann.value) is typing.Optional:
            return self.annotation_type(ann.slice)
        resolved = self.resolve(ann)
        if type(resolved) is LocalClass or inspect.isclass(resolved):
            return Instance(resolved, exact=False)
        return UNTYPED

    def lookup_name(self, name: str):
        for scope in reversed(self.scopes):
            if name in scope:
                value = scope[name]
                return None if value is UNTYPED else value
        if name in self.local_classes:
            return self.local_classes[name]
        if name in self.bindings:
            return self.bindings[name]
        if self.use_builtins and name not in self.stores and hasattr(builtins, name):
            return getattr(builtins, name)
        return None

    # -- resolving expressions ------------------------------------------------------------

    def resolve(self, expr: ast.expr):
        """Resolve an expression to a module, class, LocalClass, Instance, function or None (unknown).

        Reports the first link of an attribute chain that doesn't exist.
        """
        if not self.quiet and id(expr) in self._memo:
            return self._memo[id(expr)]
        try:
            if isinstance(expr, ast.Name):
                value = self.lookup_name(expr.id)
            elif isinstance(expr, ast.Call):
                value = self.call_result(expr)
            elif isinstance(expr, ast.Attribute):
                base = self.resolve(expr.value)
                value = None if base is None else self.resolve_member(expr, base)
            else:
                value = None
        except Exception:
            # Inspecting live objects can run arbitrary code (proxies, lazy objects) that raises.
            value = None
        if not self.quiet:
            self._memo[id(expr)] = value
        return value

    def call_result(self, call: ast.Call):
        func = self.resolve(call.func)
        if type(func) is LocalClass:
            return Instance(func, exact=func.closed)
        if inspect.isclass(func):
            # A metaclass with its own __call__ (Enum("A", ...)) may not return an instance.
            return Instance(func, exact=type(func).__call__ is type.__call__)
        if type(func) is BoundMethod:
            return return_type(func.func)
        if func is not None and not type(func) is Instance and callable(func):
            return return_type(func)
        return None

    def as_reference(self, value):
        if value is None:
            return None
        if inspect.ismodule(value) or inspect.isclass(value) or callable(value):
            return value
        # A module or class attribute holding an object; it may be replaced at runtime.
        return Instance(type(value), exact=False)

    def resolve_member(self, expr: ast.Attribute, base):
        attr = expr.attr
        if inspect.ismodule(base) or inspect.isclass(base):
            value = self.env.member(base, attr)
            if value is MISSING or type(value) is Removed:
                self.report_missing_member(expr, base, attr, value, ast.unparse(expr.value))
                return None
            self.check_deprecated(expr, value, ast.unparse(expr))
            return self.as_reference(value)
        if type(base) is LocalClass:
            state, value, _ = self.lookup_local(base, attr, instance=False)
            if state == "missing":
                hint = suggest(attr, self.local_candidates(base))
                self.report(expr, "error", "API002", f"class '{base.name}' has no attribute '{attr}'.{hint}")
            elif state == "found":
                self.check_deprecated(expr, value, f"{base.name}.{attr}")
                return self.as_reference(value)
            return None
        if type(base) is Instance:
            return self.instance_member(expr, base)
        return None

    def instance_member(self, expr: ast.Attribute, inst: Instance):
        cls, attr = inst.cls, expr.attr
        if type(cls) is LocalClass:
            state, value, owner = self.lookup_local(cls, attr, instance=True)
            if state == "found":
                self.check_deprecated(expr, value, f"{cls.name}.{attr}")
                return bind(owner, attr, value)
            if state == "missing" and inst.exact:
                hint = suggest(attr, self.local_candidates(cls))
                self.report(expr, "warning", "API005",
                            f"'{cls.name}' object has no attribute '{attr}': it is never assigned "
                            f"in the class or its bases.{hint}")
            return None
        if is_proxy(cls):
            return None
        value = self.env.member(cls, attr)
        if type(value) is Removed:
            return None
        if value is not MISSING:
            self.check_deprecated(expr, value, f"{cls.__name__}.{attr}")
            return bind(cls, attr, value)
        known = instance_attrs(cls) if inst.exact and not attr.startswith("__") else None
        if known and attr not in known[0]:
            names, certain = known
            where = self.env.label(owner_module(cls))
            hint = suggest(attr, names)
            if certain:
                self.report(expr, "error", "API002",
                            f"'{cls.__name__}' object has no attribute '{attr}' in {where}.{hint}")
            else:
                self.report(expr, "warning", "API005",
                            f"'{cls.__name__}' object may have no attribute '{attr}' in {where}: "
                            f"it is never assigned in the class.{hint}")
        return None

    # -- visiting -------------------------------------------------------------------------

    def visit_Module(self, node: ast.Module):
        self.scopes = [self.scope_types(node.body)]
        for stmt in node.body:
            self.visit(stmt)

    def visit_ClassDef(self, node: ast.ClassDef):
        for child in node.decorator_list + node.bases + node.keywords:
            self.visit(child)
        local = self.local_classes.get(node.name)
        self.class_stack.append(local if local is not None and local.node is node else None)
        for stmt in node.body:
            self.visit(stmt)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns:
            self.visit(node.returns)
        owner = self.class_stack[-1] if self.class_stack else None
        self.scopes.append(self.scope_types(node.body, node.args, owner, node.decorator_list))
        self.class_stack.append(None)
        for stmt in node.body:
            self.visit(stmt)
        self.class_stack.pop()
        self.scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda):
        self.visit(node.args)
        a = node.args
        params = a.posonlyargs + a.args + a.kwonlyargs + [x for x in (a.vararg, a.kwarg) if x]
        self.scopes.append({p.arg: UNTYPED for p in params})
        self.visit(node.body)
        self.scopes.pop()

    def visit_Attribute(self, node: ast.Attribute):
        if isinstance(node.ctx, ast.Load):
            self.resolve(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        func = self.resolve(node.func)
        if type(func) is BoundMethod:
            self.check_call(node, func.func, drop_first=func.drop_first)
        elif func is not None and type(func) not in (LocalClass, Instance) and callable(func):
            self.check_call(node, func)
        self.generic_visit(node)

    def check_call(self, node: ast.Call, func, drop_first: bool = False):
        try:
            sig = inspect.signature(func)
        except Exception:
            return
        params = list(sig.parameters.values())
        if drop_first:
            if params and params[0].kind in (inspect.Parameter.POSITIONAL_ONLY,
                                             inspect.Parameter.POSITIONAL_OR_KEYWORD):
                params = params[1:]
            elif not params or params[0].kind is not inspect.Parameter.VAR_POSITIONAL:
                return
        kinds = {p.kind for p in params}
        name = ast.unparse(node.func)
        module = owner_module(func)
        where = self.env.label(module)

        if inspect.Parameter.VAR_KEYWORD not in kinds:
            accepted = [p.name for p in params
                        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
            for kw in node.keywords:
                if kw.arg and kw.arg not in accepted:
                    self.report(kw, "error", "API003",
                                f"{name}() has no parameter '{kw.arg}' in {where}.{suggest(kw.arg, accepted)}",
                                module)

        has_star = any(isinstance(a, ast.Starred) for a in node.args)
        if inspect.Parameter.VAR_POSITIONAL not in kinds and not has_star:
            max_positional = sum(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in params)
            if len(node.args) > max_positional:
                self.report(node, "error", "API004",
                            f"{name}() takes at most {max_positional} positional argument(s) "
                            f"in {where}, got {len(node.args)}", module)


def local_names(paths: list[Path]) -> set[str]:
    """Top-level modules/packages that belong to the project itself, so we don't import them."""
    names = set()
    for path in paths:
        base = path if path.is_dir() else path.parent
        for directory in {base.resolve(), Path.cwd().resolve()}:
            if (directory / "__init__.py").exists():
                names.add(directory.name)
            for child in directory.iterdir():
                if child.suffix == ".py":
                    names.add(child.stem)
                elif child.is_dir() and (child / "__init__.py").exists():
                    names.add(child.name)
    return names


def iter_python_files(paths: list[Path]):
    skip = {".venv", "venv", ".git", "__pycache__", "node_modules", ".tox", "build", "dist"}
    for path in paths:
        if path.is_file():
            yield path
        else:
            for file in sorted(path.rglob("*.py")):
                if not skip.intersection(file.parts):
                    yield file


def check_file(env: Environment, file: Path, local: set[str], display: str | None = None) -> list[Finding]:
    display = display or str(file)
    try:
        with tokenize.open(file) as f:  # honours a PEP 263 coding cookie, as Python does
            source = f.read()
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [Finding(display, 1, 1, "error", "API000", f"could not read: {exc}")]
    try:
        return FileChecker(env, display, source, local).run()
    except SyntaxError as exc:
        return [Finding(display, exc.lineno or 1, exc.offset or 1, "error", "API000",
                        f"could not parse: {exc.msg}")]
    except Exception as exc:  # introspecting arbitrary packages; one odd file mustn't sink the run
        return [Finding(display, 1, 1, "error", "API000", f"pipbreak failed on this file: {exc!r}")]


def check_paths(paths: list[Path], extra_local: set[str] = frozenset()) -> tuple[list[Finding], int]:
    env = Environment()
    local = local_names(paths) | set(extra_local)
    findings: list[Finding] = []
    count = 0
    for file in iter_python_files(paths):
        count += 1
        findings.extend(check_file(env, file, local))
    return findings, count


# -- installed packages: do they still agree with each other? ----------------------------------

def installed_distributions() -> dict[str, tuple[importlib.metadata.Distribution, set[str]]]:
    """Each installed distribution (first on sys.path wins, as for imports) and its top-level modules."""
    tops: dict[str, set[str]] = defaultdict(set)
    for top, dists in packages_distributions().items():
        for dist in dists:
            tops[canonical(dist)].add(top)
    found = {}
    for dist in importlib.metadata.distributions():
        name = canonical(dist.metadata["Name"] or "")
        if name and name not in found:
            found[name] = (dist, tops.get(name, set()))
    return found


VENDORED = {"_vendor", "vendor", "_vendored", "vendored", "extern", "_extern"}


def canonical(name: str) -> str:
    return name.lower().replace("-", "_").replace(".", "_")


def check_installed(uses: set[str] = frozenset(), progress=None) -> tuple[list[Finding], int]:
    """Check every installed package's code against the *other* installed packages.

    pip only checks declared version ranges, and those are often open-ended (Flask 2.2 asks for
    Werkzeug>=2.2.2), so a set of packages can install cleanly and still not work together. Only
    calls into another distribution count - a package's own internals and the stdlib are its
    maintainers' business - and missing optional dependencies are ignored. With `uses`, only
    packages that import one of those modules are checked. `progress(done, total, package)` is
    called before each package.
    """
    env = Environment()
    dists = installed_distributions()
    owner = {top: name for name, (_, tops) in dists.items() for top in tops}
    # Accept distribution names too: --uses Werkzeug means the `werkzeug` module.
    uses = {top for u in uses for top in (dists[canonical(u)][1] if canonical(u) in dists else {u})}
    imports = re.compile(r"^\s*(?:import|from)\s+(" + "|".join(map(re.escape, sorted(uses))) + r")\b", re.M)
    findings: list[Finding] = []
    count = 0
    for done, (name, (dist, tops)) in enumerate(sorted(dists.items())):
        if name == "pipbreak" or uses & tops:
            continue
        if progress:
            progress(done, len(dists), f"{dist.metadata['Name']} {dist.version}")
        roots = tops | {f"{t}.py" for t in tops}
        # Vendored copies (setuptools/_vendor/wheel) import themselves by their original names
        # through sys.path tricks, so they'd be checked against the separately installed package.
        # A package's own test suite doesn't run in your application, and is full of platform and
        # version skips we can't see (np.float96 exists only on some platforms).
        files = [(f.as_posix(), Path(dist.locate_file(f))) for f in dist.files or ()
                 if f.suffix == ".py" and f.parts[0] in roots and not VENDORED.intersection(f.parts)
                 and not {"tests", "test"}.intersection(f.parts) and not f.name.startswith("test_")]
        files = [(display, path) for display, path in files if path.is_file()]
        if uses and not any(imports.search(path.read_text(encoding="utf-8", errors="replace"))
                            for _, path in files):
            continue
        label = f"{dist.metadata['Name']}=={dist.version}"
        for display, path in files:
            count += 1
            for f in check_file(env, path, tops, display):
                target = owner.get(f.target.split(".")[0])
                if target and target != name:
                    findings.append(replace(f, message=f"{label}: {f.message}"))
    return findings, count


def check_source(source: str, path: str = "<string>", local: set[str] = frozenset()) -> list[Finding]:
    return FileChecker(Environment(), path, source, set(local)).run()

"""Core reference-leak detection and reporting."""

# Authors: Eric Larson <larson.eric.d@gmail.com>
#
# License: BSD-3-Clause

import gc
import inspect
import sys
import types


def _fullname(obj):
    if inspect.ismodule(obj):
        # Otherwise every module shows up identically as just "module",
        # which is exactly the info we need to tell which one is which.
        return getattr(obj, "__name__", "<unknown module>")
    klass = obj if inspect.isclass(obj) else obj.__class__
    module = klass.__module__
    name = klass.__qualname__
    if module != "builtins":
        name = f"{module}.{name}"
    return name


def _key_suffix(obj, referent):
    """Return the ``[...]``-like Python-syntax suffix to reach referent from obj."""
    if isinstance(obj, (list, tuple)):
        for ii, item in enumerate(obj):
            if item is referent:
                return f"[{ii}]"
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if key is referent:
                return "-key"
            if value is referent:
                return f"[{key!r}]"
    return ""


def gc_collect_once(request=None):
    """Call ``gc.collect()``, deduplicated once per test item if given a request.

    ``gc.collect()`` cost scales with the number of tracked objects in the
    whole process, so when several independent test fixtures each want a
    fresh collect during the same test's teardown, doing it more than once
    is a significant and unnecessary fraction of total test time. When
    ``request`` (a pytest fixture request) is given, only the first call
    for a given test item actually collects; later calls are no-ops.

    Parameters
    ----------
    request : pytest.FixtureRequest | None
        If given, deduplicate the collection per test item.
    """
    if request is None:
        gc.collect()
        return
    node = request.node
    if getattr(node, "_refleak_gc_collected", False):
        return
    node._refleak_gc_collected = True
    gc.collect()


def _safe_repr(obj, *, maxlen=100):
    """Get a repr that cannot raise (e.g., on a deleted VTK/Qt C++ object)."""
    try:
        rep = repr(obj)
    except Exception as exc:
        return f"<repr failed: {type(exc).__name__}: {exc}>"
    return rep[:maxlen].replace("\n", " ")


def _dict_owner(d):
    """Find the object whose __dict__ (or similar) *is* d, if any."""
    for o in gc.get_referrers(d):
        if getattr(o, "__dict__", None) is d:
            return o
    return None


def _cell_owner(cell):
    """Find the (function, freevar name) whose closure holds ``cell``, if any.

    Naming the closing-over function and its variable is what makes a leak
    via an enclosing scope (a classic reference-cycle source) actionable; the
    cell object itself is anonymous.
    """
    for t in gc.get_referrers(cell):
        if not isinstance(t, tuple):
            continue
        for f in gc.get_referrers(t):
            if inspect.isfunction(f) and f.__closure__ is t:
                # A tuple only refers to its elements, so cell must be in t
                # (compare by identity: cells compare by their contents).
                idx = [id(c) for c in t].index(id(cell))
                return f, f.__code__.co_freevars[idx]
    return None, None


def _module_global_name(obj):
    """Find the "module.attr" name of obj if it is itself a module-level global.

    This is what lets a failure message name e.g. a long-lived module-level
    registry (cache, weak-value dict, etc.) directly, which is often the
    actual reason an object outlives a single test/example: a plain
    ``gc.get_referrers`` walk only shows an anonymous ``dict``/``list``.
    """
    for modname, mod in list(sys.modules.items()):
        d = getattr(mod, "__dict__", None)
        if not d:
            continue
        try:
            items = list(d.items())
        except Exception:
            continue
        for key, val in items:
            if val is obj:
                return f"{modname}.{key}"
    return None


def _describe_referrer(r, referent):
    """Build a "name: type = repr"-style description of r, which refers to referent.

    Mirroring a Python variable declaration keeps every referrer kind
    parseable the same way: a name (the best Python-syntax expression for
    reaching ``r``, falling back to its type when nothing better is known),
    its type (omitted when it would just repeat the name), and a repr -- for
    containers (dict/list/tuple) this is always at least a length summary
    rather than their (possibly huge) contents.

    Returns
    -------
    desc : str
        Human-readable, safe description of r.
    next_obj : object | None
        What to keep tracing referrers of (``None`` to stop here). This is
        usually ``r`` itself, but for e.g. an instance's ``__dict__`` it's
        the owning instance (tracing the dict's own referrers is normally
        just uninformative interpreter-internal noise), for a closure cell
        it's the function closing over it (the ``__closure__`` tuple in
        between is likewise noise), and for a module-level global it's
        ``None`` (a named global is already a fully-explained anchor;
        nothing more useful to say).
    """
    if inspect.ismethod(r):
        name = r.__func__.__qualname__
        return f"{name}: method = {_safe_repr(r.__self__)}", r
    if inspect.isfunction(r):
        return f"{r.__qualname__}: function = {_safe_repr(r)}", r
    if inspect.ismodule(r):
        return f"{_fullname(r)}: module = {_safe_repr(r)}", r
    if isinstance(r, dict):
        suffix = _key_suffix(r, referent)
        owner = _dict_owner(r)
        if owner is not None:
            # e.g. "some.module.SomeClass.__dict__['attr']: dict = <len=1>"
            name = f"{_fullname(owner)}.__dict__{suffix}"
            # A module attribute is a fully-explained anchor: what refers to
            # the *module* (importers, sys.modules, parent packages) is never
            # the actionable part, so stop there like for named globals.
            next_obj = None if inspect.ismodule(owner) else owner
            return f"{name}: dict = <len={len(r)}>", next_obj
        global_name = _module_global_name(r)
        if global_name is not None:
            # e.g. "sys.modules['__main__']: dict = <len=2142>"
            return f"{global_name}{suffix}: dict = <len={len(r)}>", None
        return f"dict{suffix}: dict = <len={len(r)}>", r
    if isinstance(r, (list, tuple)):
        suffix = _key_suffix(r, referent)
        kind = "list" if isinstance(r, list) else "tuple"
        global_name = _module_global_name(r)
        if global_name is not None:
            return f"{global_name}{suffix}: {kind} = <len={len(r)}>", None
        return f"{kind}{suffix}: {kind} = <len={len(r)}>", r
    if isinstance(r, types.CellType):  # a closure variable
        # A cell's contents *is* the referent (its only reference), so
        # repeating it here would be redundant; like the other containers,
        # summarize the closure by length instead.
        owner, varname = _cell_owner(r)
        if owner is not None:
            # e.g. "cb.__closure__['widget']: cell = <closure len=1>"
            name = f"{owner.__qualname__}.__closure__[{varname!r}]"
            return f"{name}: cell = <closure len={len(owner.__closure__)}>", owner
        return f"cell = {_safe_repr(r)}", r
    global_name = _module_global_name(r)
    if global_name is not None:
        return f"{global_name}: {_fullname(r)} = {_safe_repr(r)}", None
    # Here the best available "name" is just the type, so writing both (e.g.
    # "generator: generator = ...") would only stutter.
    return f"{_fullname(r)} = {_safe_repr(r)}", r


def _referrer_tree(
    o, depth, *, max_depth, max_lines, count, excluded, recursed, root_id
):
    """Recursively build a tree of (description, children) referrer nodes.

    ``excluded`` objects (e.g. the huge ``gc.get_objects()`` snapshot) are
    never shown or recursed into. ``recursed`` tracks objects already used
    as a recursion root, so a cycle in the referrer graph can't recurse
    forever; unlike ``excluded`` it doesn't prevent an object from being
    *listed* (only from being expanded again). ``count`` is a 1-element list
    used as a mutable counter shared across the whole recursion, so the
    total number of nodes (not just per-level) is capped at ``max_lines``.

    So that a childless node isn't ambiguous, a node whose expansion target
    was skipped gets a marker: ``(cycle back to 0x...)`` when the chain has
    come back around to the traced object itself (``root_id``) -- i.e. a
    reference cycle -- and ``(see above)`` when it was already expanded
    earlier in the tree.
    """
    nodes = list()
    refs = gc.get_referrers(o)
    # While this list is alive (i.e. for the duration of this call, including
    # any recursive calls below), it itself shows up as a "referrer" of any
    # of its own elements if we ask gc.get_referrers() about them -- that's
    # an artifact of doing this traversal at all, not a real anchor.
    excluded.add(id(refs))
    for r in refs:
        if count[0] >= max_lines:
            nodes.append(("... (truncated)", []))
            return nodes
        if inspect.isframe(r) or id(r) in excluded:
            continue
        count[0] += 1
        desc, next_obj = _describe_referrer(r, o)
        children = list()
        if next_obj is not None and id(next_obj) not in excluded:
            if id(next_obj) == root_id:
                desc += f" (cycle back to 0x{root_id:x})"
            elif id(next_obj) in recursed:
                desc += " (see above)"
            elif depth + 1 < max_depth:
                recursed.add(id(next_obj))
                children = _referrer_tree(
                    next_obj,
                    depth + 1,
                    max_depth=max_depth,
                    max_lines=max_lines,
                    count=count,
                    excluded=excluded,
                    recursed=recursed,
                    root_id=root_id,
                )
        nodes.append((desc, children))
        del r
    del refs
    return nodes


def _render_tree(nodes, prefix=""):
    """Render a (description, children) tree using box-drawing characters."""
    lines = list()
    for i, (desc, children) in enumerate(nodes):
        last = i == len(nodes) - 1
        lines.append(prefix + ("└── " if last else "├── ") + desc)
        child_prefix = prefix + ("    " if last else "│   ")
        lines.extend(_render_tree(children, child_prefix))
    return lines


def referrer_chain(obj, *, max_depth=5, max_lines=40, exclude_ids=()):
    """Describe, recursively, what holds references to obj.

    Referrers are walked up to ``max_depth`` hops and rendered as a tree, so
    that a leaked object's actual anchor (e.g. a module-level registry
    several containers away) is visible directly in the failure message,
    instead of just the immediate (often uninformative, e.g. a bare
    ``list``) referrer.

    Parameters
    ----------
    obj : object
        The object to trace referrers of.
    max_depth : int
        Maximum number of referrer hops to walk.
    max_lines : int
        Maximum number of lines (nodes) to render in total.
    exclude_ids : Iterable[int]
        ``id()``\\ s of objects to treat as if they don't exist (e.g. any
        containers the caller itself is using to hold state during the
        traversal).

    Returns
    -------
    lines : list[str]
        Rendered tree lines, one referrer per line. A line ending in
        ``(cycle back to 0x...)`` reached ``obj`` itself again (a reference
        cycle); one ending in ``(see above)`` reached something already
        expanded earlier in the tree.
    has_referrers : bool
        Whether any (non-excluded, non-frame) referrers were found at all.
    """
    excluded = set(exclude_ids)
    recursed = {id(obj)}
    nodes = _referrer_tree(
        obj,
        0,
        max_depth=max_depth,
        max_lines=max_lines,
        count=[0],
        excluded=excluded,
        recursed=recursed,
        root_id=id(obj),
    )
    return _render_tree(nodes), len(nodes) > 0


def assert_no_instances(cls, when="", *, request=None, objs=None, extra_info=None):
    """Assert that no instances of ``cls`` are still alive.

    For any surviving instance, the failure message includes a rendered
    referrer chain (see :func:`referrer_chain`) explaining what's still
    holding a reference to it, which is usually far more actionable than a
    bare instance count. Each survivor's section is headed by its type and
    ``id()`` (in hex, to match default object reprs).

    Parameters
    ----------
    cls : type
        The class to check for live instances of.
    when : str
        A short description of when this check is happening, included in
        the assertion message (e.g. ``"after test"``).
    request : pytest.FixtureRequest | None
        If given, deduplicate the ``gc.collect()`` call across checks within
        the same test item (see :func:`gc_collect_once`).
    objs : list | None
        The result of ``gc.get_objects()`` to check, if already computed by
        the caller. If ``None``, it is computed here.
    extra_info : Callable[[object], list[str]] | None
        If given, called with each surviving instance to produce extra lines
        (e.g. instance-specific diagnostic state) prepended to its entry in
        the failure message.
    """
    __tracebackhide__ = True
    n = 0
    ref = list()
    gc_collect_once(request)
    if objs is None:
        objs = gc.get_objects()
    for obj in objs:  # e.g., vtkPolyData, Brain, Plotter, etc.
        try:
            check = isinstance(obj, cls)
        except Exception:  # such as a weakref
            check = False
        if check:
            extra = list(extra_info(obj)) if extra_info is not None else list()
            lines, has_referrers = referrer_chain(
                obj, exclude_ids={id(objs), id(ref), id(globals())}
            )
            if has_referrers:
                ref.extend(extra)
                # id() tags just the survivors themselves (not every tree
                # node): it distinguishes multiple instances of cls from each
                # other and can be correlated with "<... object at 0x...>"
                # reprs elsewhere. The summary line already gives the full
                # class name, so the short name suffices here (while still
                # revealing subclasses of cls).
                ref.append(f"{obj.__class__.__qualname__} @ 0x{id(obj):x}:")
                ref.extend(lines)
                n += 1
        del obj
    del objs
    assert n == 0, f"\nFound {n} {_fullname(cls)} @ {when}:\n" + "\n".join(ref)

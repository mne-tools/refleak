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


def _attr_name(obj, referent):
    """Find the attribute of obj whose value *is* referent, if any.

    On Python >= 3.11 an instance holding referent as an attribute typically
    shows up as a referrer directly (inline values, or ``__slots__`` on any
    version) rather than via its ``__dict__``, so without this the failure
    message would name the holder but not *which* attribute does the holding.
    """
    if referent is None:
        return None
    try:
        items = list(vars(obj).items())
    except TypeError:  # no __dict__ (e.g. __slots__-only)
        items = list()
    for klass in type(obj).__mro__:
        slots = getattr(klass, "__slots__", ())
        for slot in (slots,) if isinstance(slots, str) else slots:
            try:
                items.append((slot, getattr(obj, slot)))
            except AttributeError:  # unset slot
                continue
    for key, val in items:
        if val is referent:
            return key
    return None


def _module_globals_map():
    """Snapshot ``{id(obj): "module.attr"}`` for all module-level globals.

    This is what lets a failure message name e.g. a long-lived module-level
    registry (cache, weak-value dict, etc.) directly, which is often the
    actual reason an object outlives a single test/example: a plain
    ``gc.get_referrers`` walk only shows an anonymous ``dict``/``list``.
    Snapshotting once per chain (rather than rescanning all of
    ``sys.modules`` for every tree node) keeps rendering fast in processes
    with many/large modules; storing only ids and names means the map itself
    keeps nothing alive.
    """
    out = dict()
    for modname, mod in list(sys.modules.items()):
        d = getattr(mod, "__dict__", None)
        if not d:
            continue
        try:
            items = list(d.items())
        except Exception:
            continue
        for key, val in items:
            out.setdefault(id(val), f"{modname}.{key}")
    return out


def _live_frame_ids():
    """Get ``id()``\\ s of frames currently executing in any thread.

    Objects referenced by these frames are the traversal's own machinery
    and the caller's live stack -- never leaks. Frames *not* here but still
    alive (a stored traceback, a suspended generator/coroutine) are real
    anchors, classically an exception saved somewhere. Computed fresh at
    each use so the traversal's own frames are always included.
    """
    ids = set()
    for frame in sys._current_frames().values():
        while frame is not None:
            ids.add(id(frame))
            frame = frame.f_back
    return ids


def _describe_referrer(r, referent, global_names=None):
    """Build a "name: type = repr"-style description of r, which refers to referent.

    Mirroring a Python variable declaration keeps every referrer kind
    parseable the same way: a name (the best Python-syntax expression for
    reaching ``r``, falling back to its type when nothing better is known),
    its type (omitted when it would just repeat the name), and a repr -- for
    containers (dict/list/tuple) this is always at least a length summary
    rather than their (possibly huge) contents. ``global_names`` is a
    :func:`_module_globals_map` snapshot (built here if not given).

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
    if global_names is None:
        global_names = _module_globals_map()
    if inspect.isframe(r):
        # Only non-executing frames get here (see _referrer_tree): a frame
        # kept alive by a stored traceback or a suspended generator really
        # does anchor its locals.
        code = r.f_code
        qual = getattr(code, "co_qualname", code.co_name)  # co_qualname: 3.11+
        return f"frame of {qual}: frame = {code.co_filename}:{r.f_lineno}", r
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
        global_name = global_names.get(id(r))
        if global_name is not None:
            # e.g. "sys.modules['__main__']: dict = <len=2142>"
            return f"{global_name}{suffix}: dict = <len={len(r)}>", None
        return f"dict{suffix}: dict = <len={len(r)}>", r
    if isinstance(r, (list, tuple)):
        suffix = _key_suffix(r, referent)
        kind = "list" if isinstance(r, list) else "tuple"
        global_name = global_names.get(id(r))
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
    # e.g. ".the_widget" when r holds referent as an instance attribute
    attr = _attr_name(r, referent)
    suffix = f".{attr}" if attr is not None else ""
    global_name = global_names.get(id(r))
    if global_name is not None:
        return f"{global_name}{suffix}: {_fullname(r)} = {_safe_repr(r)}", None
    if suffix:
        name = _fullname(r)
        return f"{name}{suffix}: {name} = {_safe_repr(r)}", r
    # Here the best available "name" is just the type, so writing both (e.g.
    # "generator: generator = ...") would only stutter.
    return f"{_fullname(r)} = {_safe_repr(r)}", r


def _referrer_tree(
    o, depth, *, max_depth, max_lines, count, excluded, recursed, root_id, global_names
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
        # Live-stack frames (recomputed here so this traversal's own frames
        # are included) are machinery, not leaks; dead-but-referenced frames
        # (stored tracebacks, suspended generators) are real anchors and are
        # kept.
        if id(r) in excluded or (inspect.isframe(r) and id(r) in _live_frame_ids()):
            continue
        count[0] += 1
        desc, next_obj = _describe_referrer(r, o, global_names)
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
                    global_names=global_names,
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
        Whether any (non-excluded, non-live-frame) referrers were found at
        all.
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
        global_names=_module_globals_map(),
    )
    return _render_tree(nodes), len(nodes) > 0


def _found_summary(n, what, when):
    """Build the "Found N <what> [@ when]:" summary line."""
    out = f"Found {n} {what}"
    if when:
        out += f" @ {when}"
    return out + ":"


def _match_objects(match, objs):
    """Yield the objects in objs that match, treating a raising check as a miss.

    ``match`` is either a type / tuple of types (checked with ``isinstance``)
    or a predicate callable. The check runs on arbitrary heap objects, so it
    can raise for exotic ones (weakref proxies, half-destroyed native
    wrappers); those are misses, not errors.
    """
    if isinstance(match, (type, tuple)):
        for obj in objs:
            try:
                if isinstance(obj, match):
                    yield obj
            except Exception:  # such as a weakref
                pass
    else:
        for obj in objs:
            try:
                if match(obj):
                    yield obj
            except Exception:  # e.g. a predicate poking a dead C++ proxy
                pass


def _build_report(survivors, *, objs, extra_info, max_depth, max_lines):
    """Build (count, message lines) for survivors, each with a referrer chain.

    Only survivors with at least one non-excluded referrer count: one held
    alive solely by the traversal's own containers isn't a leak.
    """
    n = 0
    ref = list()
    for obj in survivors:
        extra = list(extra_info(obj)) if extra_info is not None else list()
        lines, has_referrers = referrer_chain(
            obj,
            max_depth=max_depth,
            max_lines=max_lines,
            exclude_ids={id(objs), id(survivors), id(ref), id(globals())},
        )
        if has_referrers:
            ref.extend(extra)
            # id() tags just the survivors themselves (not every tree
            # node): it distinguishes multiple instances of a class from
            # each other and can be correlated with "<... object at 0x...>"
            # reprs elsewhere. The summary line already gives the full
            # class name, so the short name suffices here (while still
            # revealing subclasses).
            ref.append(f"{obj.__class__.__qualname__} @ 0x{id(obj):x}:")
            ref.extend(lines)
            n += 1
        del obj
    return n, ref


def assert_no_instances(
    cls,
    when="",
    *,
    request=None,
    objs=None,
    extra_info=None,
    max_depth=5,
    max_lines=40,
):
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
    max_depth : int
        Maximum number of referrer hops to walk per surviving instance
        (see :func:`referrer_chain`).
    max_lines : int
        Maximum number of tree lines to render per surviving instance
        (see :func:`referrer_chain`).
    """
    __tracebackhide__ = True
    gc_collect_once(request)
    if objs is None:
        objs = gc.get_objects()
    survivors = list(_match_objects(cls, objs))  # e.g., vtkPolyData, Brain, ...
    n, ref = _build_report(
        survivors,
        objs=objs,
        extra_info=extra_info,
        max_depth=max_depth,
        max_lines=max_lines,
    )
    del objs, survivors
    assert n == 0, (
        "\n" + _found_summary(n, _fullname(cls), when) + "\n" + "\n".join(ref)
    )


class Snapshot:
    """Snapshot of live matching objects, to later assert none *new* survive.

    :func:`assert_no_instances` requires that *zero* instances exist, which is
    too strict when some legitimately pre-date the code under test (e.g. VTK
    objects held by a theme or module-level cache). A ``Snapshot`` records the
    ``id()``\\ s of matching objects up front so :meth:`assert_no_new` can
    flag only what appeared afterwards and survived garbage collection --
    the pattern used by ``check_gc``-style pytest fixtures: snapshot before
    the test body, assert after.

    Only ids are stored, so a ``Snapshot`` itself keeps nothing alive. The
    unavoidable caveat of id-based snapshotting is id reuse: a new object
    allocated at a dead pre-existing object's address is indistinguishable
    from that pre-existing object (a false negative, never a false positive).
    ``collect=True`` minimizes the window by clearing collectable garbage
    before ids are recorded.

    Parameters
    ----------
    match : type | tuple[type, ...] | callable
        What counts as a matching object: types are checked with
        ``isinstance``, anything else is called with each candidate object
        and should return truthy for a match. A check that raises (e.g. on a
        weakref proxy or a half-destroyed native wrapper) counts as a miss.
    label : str | None
        Adjective for matching objects in the failure summary, e.g. ``"VTK"``
        renders as ``"Found 2 new VTK objects"``. By default a type (or tuple
        of types) is named directly and a callable adds nothing (``"Found 2
        new objects"``).
    collect : bool
        Call ``gc.collect()`` before recording ids (default ``True``). Skip
        only when a collect is prohibitively slow at snapshot time and the
        increased id-reuse window is acceptable.
    objs : list | None
        The result of ``gc.get_objects()`` to snapshot, if already computed
        by the caller. If given, ``collect`` is ignored -- any desired
        collect must have happened before ``objs`` was computed. If ``None``,
        it is computed here.

    Examples
    --------
    >>> from refleak.testing import Snapshot
    >>> class Widget:
    ...     pass
    >>> pre_existing = Widget()  # recorded in the snapshot, never reported
    >>> snap = Snapshot(Widget)
    >>> transient = Widget()
    >>> del transient
    >>> snap.assert_no_new(when="after test")  # passes
    """

    def __init__(self, match, *, label=None, collect=True, objs=None):
        if isinstance(match, tuple):
            if not all(isinstance(m, type) for m in match):
                msg = f"match tuple must contain only types, got {match!r}"
                raise TypeError(msg)
        elif not isinstance(match, type) and not callable(match):
            msg = f"match must be a type, tuple of types, or callable, got {match!r}"
            raise TypeError(msg)
        self._match = match
        if label is None:
            if isinstance(match, type):
                label = _fullname(match)
            elif isinstance(match, tuple):
                label = "(" + " | ".join(_fullname(m) for m in match) + ")"
            else:
                label = ""
        self._label = label
        if objs is None:
            if collect:
                # A plain collect, not gc_collect_once(request): the
                # once-per-item deduplication would make this setup-time
                # collect suppress the teardown-time one in assert_no_new --
                # the collect that matters.
                gc.collect()
            objs = gc.get_objects()
        self._before_ids = {id(obj) for obj in _match_objects(match, objs)}

    def assert_no_new(
        self,
        when="",
        *,
        request=None,
        objs=None,
        extra_info=None,
        max_depth=5,
        max_lines=40,
    ):
        """Assert that no matching objects newer than the snapshot are alive.

        For any surviving new object, the failure message includes a rendered
        referrer chain (see :func:`referrer_chain`) explaining what's still
        holding a reference to it. Each survivor's section is headed by its
        type and ``id()`` (in hex, to match default object reprs).

        Parameters
        ----------
        when : str
            A short description of when this check is happening, included in
            the assertion message (e.g. ``"after test"``).
        request : pytest.FixtureRequest | None
            If given, deduplicate the ``gc.collect()`` call across checks
            within the same test item (see :func:`gc_collect_once`).
        objs : list | None
            The result of ``gc.get_objects()`` to check, if already computed
            by the caller. If ``None``, it is computed here.
        extra_info : Callable[[object], list[str]] | None
            If given, called with each surviving object to produce extra
            lines (e.g. instance-specific diagnostic state) prepended to its
            entry in the failure message.
        max_depth : int
            Maximum number of referrer hops to walk per surviving object
            (see :func:`referrer_chain`).
        max_lines : int
            Maximum number of tree lines to render per surviving object
            (see :func:`referrer_chain`).
        """
        __tracebackhide__ = True
        gc_collect_once(request)
        if objs is None:
            objs = gc.get_objects()
        before = self._before_ids
        survivors = [
            obj for obj in _match_objects(self._match, objs) if id(obj) not in before
        ]
        n, ref = _build_report(
            survivors,
            objs=objs,
            extra_info=extra_info,
            max_depth=max_depth,
            max_lines=max_lines,
        )
        del objs, survivors
        what = f"new {self._label} object" if self._label else "new object"
        what += "" if n == 1 else "s"
        assert n == 0, "\n" + _found_summary(n, what, when) + "\n" + "\n".join(ref)

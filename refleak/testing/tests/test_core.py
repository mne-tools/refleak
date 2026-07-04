"""Tests for refleak.testing."""

# Authors: Eric Larson <larson.eric.d@gmail.com>
#
# License: BSD-3-Clause

import importlib
import importlib.metadata
import sys
import types

import pytest

import refleak
from refleak import testing
from refleak.testing import assert_no_instances
from refleak.testing._core import (
    _describe_referrer,
    _fullname,
    _key_suffix,
    _module_global_name,
    _safe_repr,
)


class _Leaky:
    """A trivial class to instantiate and (optionally) leak."""


_leaked: _Leaky | None = None


@pytest.fixture
def reset_leaky():
    """Clear the module-level _leaked global on teardown."""
    yield
    global _leaked
    _leaked = None


def test_assert_no_instances_passes_when_clean():
    """No live instances -> no assertion error."""
    obj = _Leaky()
    del obj
    assert_no_instances(_Leaky, when="test")


def test_assert_no_instances_reports_referrer_chain(reset_leaky):
    """A leaked instance held by a module-level global is reported."""
    global _leaked
    _leaked = _Leaky()
    with pytest.raises(AssertionError, match=rf"Found 1 {__name__}\._Leaky @ test"):
        assert_no_instances(_Leaky, when="test")


def test_assert_no_instances_extra_info(reset_leaky):
    """extra_info lines are prepended to a failing instance's report."""
    global _leaked
    _leaked = _Leaky()
    with pytest.raises(AssertionError, match="custom-marker"):
        assert_no_instances(
            _Leaky, when="test", extra_info=lambda obj: ["custom-marker"]
        )


def test_gc_collect_once_dedupes(request):
    """A second call with the same request is a no-op."""
    testing.gc_collect_once(request)
    assert request.node._refleak_gc_collected is True
    testing.gc_collect_once(request)  # does not raise, still a no-op


def test_referrer_chain_finds_referrers():
    """A list holding an object shows up as one of its referrers."""
    obj = _Leaky()
    holder = [obj]
    lines, has_referrers = testing.referrer_chain(obj)
    assert has_referrers
    assert any("list" in line for line in lines)
    del holder


def test_assert_no_instances():
    """Test Some basics of our assertions."""

    class _Foo:
        pass

    _holder = {"key": _Foo()}

    with pytest.raises(AssertionError, match="after closing"):
        assert_no_instances(_Foo, "after closing")

    del _holder
    assert_no_instances(_Foo, "test")

    holder = _Foo()
    # A bare local is invisible: its only reference lives in the calling
    # frame's fast locals, which gc.get_referrers cannot see (and frames are
    # deliberately skipped anyway), so it is not reported as a leak.
    assert_no_instances(_Foo, "after closing", objs=[holder])
    assert_no_instances(_Foo, "after closing")

    # Held by a GC-visible container -> reported, including via objs=...
    container = [holder]
    with pytest.raises(AssertionError, match="after closing"):
        assert_no_instances(_Foo, "after closing", objs=[holder])
    with pytest.raises(AssertionError, match="after closing"):
        assert_no_instances(_Foo, "after closing")
    del container
    assert_no_instances(_Foo, "test")


def test_cell_reports_closure_owner():
    """A closure cell keeping an instance alive is named after its function.

    An enclosing scope that pulls a reference into an inner function keeps the
    object alive via a cell (possibly in a cycle); the failure message should
    name the closing-over function and variable, and recursion should continue
    from that function to find what anchors *it*.
    """
    obj = _Leaky()

    def _inner():
        return obj  # closes over obj -> a cell now refers to it

    assert _inner() is obj
    with pytest.raises(AssertionError) as exc_info:
        assert_no_instances(_Leaky, when="test")
    msg = str(exc_info.value)
    # the cell is named after the function closing over it and its variable;
    # its contents would just repeat the referent, so the closure length is
    # summarized instead (as for the other container types)
    assert "_inner.__closure__['obj']: cell = <closure len=1>" in msg
    # obj is a cell variable of *this* frame (that's what the closure closes
    # over), so the cell stays alive -- and visible -- until obj is rebound,
    # which drops the instance from the cell.
    del exc_info, _inner
    obj = None
    assert_no_instances(_Leaky, when="test")


def test_describe_referrer_ownerless_cell():
    """An empty (cleared) cell with no owning closure must render safely."""
    cell = types.CellType()
    holder = [cell]  # a non-tuple referrer of the cell
    decoy = (cell,)  # a tuple referrer that is no function's __closure__
    desc, next_obj = _describe_referrer(cell, None)
    assert desc.startswith("cell = <cell at")
    assert desc.endswith("empty>")
    assert next_obj is cell
    del holder, decoy


def test_describe_referrer_function_and_module():
    """Function and module referrers are described by their qualified names."""
    desc, next_obj = _describe_referrer(test_referrer_chain_finds_referrers, None)
    assert desc.startswith("test_referrer_chain_finds_referrers: function = ")
    assert next_obj is test_referrer_chain_finds_referrers
    desc, next_obj = _describe_referrer(sys, None)
    assert desc.startswith("sys: module = <module 'sys'")
    assert next_obj is sys


_holder_list: list | None = None


@pytest.fixture
def reset_holder_list():
    """Clear the module-level _holder_list global on teardown."""
    yield
    global _holder_list
    _holder_list = None


def test_module_global_list_is_named(reset_holder_list):
    """A leaked instance in a module-level list is anchored by its global name."""
    global _holder_list
    _holder_list = [_Leaky()]
    with pytest.raises(AssertionError, match=r"_holder_list\[0\]: list = <len=1>"):
        assert_no_instances(_Leaky, when="test")


def test_module_global_name_skips_broken_modules(monkeypatch, reset_leaky):
    """Broken sys.modules entries (None, exploding __dict__) are skipped."""

    class _EvilDict(dict):
        def items(self):
            """Raise instead of iterating."""
            raise RuntimeError("boom")

    class _FakeModule:
        pass

    fake = _FakeModule()
    fake.__dict__ = _EvilDict(some_attr=1)
    monkeypatch.setitem(sys.modules, "refleak_test_none_module", None)
    monkeypatch.setitem(sys.modules, "refleak_test_evil_module", fake)
    # Not a global anywhere -> full scan (hitting the broken entries) -> None
    assert _module_global_name(object()) is None
    global _leaked
    _leaked = _Leaky()
    assert _module_global_name(_leaked) == f"{__name__}._leaked"


def test_safe_repr_failure():
    """A raising __repr__ (e.g. a dead C++ proxy) is reported, not propagated."""

    class _BadRepr:
        def __repr__(self):
            """Raise unconditionally."""
            raise ValueError("nope")

    assert _safe_repr(_BadRepr()) == "<repr failed: ValueError: nope>"


def test_key_suffix():
    """Container suffixes, including dict-key hits and not-found fallbacks."""
    obj = _Leaky()
    assert _key_suffix([1, obj], obj) == "[1]"
    assert _key_suffix((obj,), obj) == "[0]"
    assert _key_suffix({"a": obj}, obj) == "['a']"
    assert _key_suffix({obj: 1}, obj) == "-key"
    assert _key_suffix([1, 2], obj) == ""
    assert _key_suffix({"a": 1}, obj) == ""
    assert _key_suffix(42, obj) == ""


def test_assert_no_instances_isinstance_raises():
    """A cls whose isinstance check explodes (like a weakref) is tolerated."""

    class _Meta(type):
        def __instancecheck__(cls, instance):
            """Raise for any isinstance check."""
            raise TypeError("boom")

    class _Weird(metaclass=_Meta):
        pass

    assert_no_instances(_Weird, "test", objs=[object()])


def test_version_fallback():
    """__version__ falls back to 0.0.0 when package metadata is missing."""
    real = refleak.__version__
    with pytest.MonkeyPatch.context() as mp:

        def _raise(name):
            raise importlib.metadata.PackageNotFoundError(name)

        mp.setattr(importlib.metadata, "version", _raise)
        importlib.reload(refleak)
        assert refleak.__version__ == "0.0.0"
    importlib.reload(refleak)
    assert refleak.__version__ == real


def test_describe_referrer_kinds(reset_leaky):
    """Bound methods, anonymous objects, and named globals render correctly."""

    class _A:
        def _meth(self):
            """No-op."""

    a = _A()
    m = a._meth
    desc, next_obj = _describe_referrer(m, a)
    assert "._A._meth: method = <" in desc
    assert next_obj is m

    gen = (i for i in ())
    desc, next_obj = _describe_referrer(gen, None)
    # anonymous fallback: the name is just the type, so it is not repeated
    assert desc.startswith("generator = <generator object")
    assert next_obj is gen

    global _leaked
    _leaked = _Leaky()
    desc, next_obj = _describe_referrer(_leaked, None)
    assert desc == f"{__name__}._leaked: {__name__}._Leaky = {_leaked!r}"
    assert next_obj is None  # a named global is a fully-explained anchor


_holder_dict: dict | None = None


@pytest.fixture
def reset_holder_dict():
    """Clear the module-level _holder_dict global on teardown."""
    yield
    global _holder_dict
    _holder_dict = None


def test_module_global_dict_is_named(reset_holder_dict):
    """A leaked instance in a module-level (plain) dict is anchored by name."""
    global _holder_dict
    _holder_dict = {"k": _Leaky()}
    with pytest.raises(AssertionError, match=r"_holder_dict\['k'\]: dict = <len=1>"):
        assert_no_instances(_Leaky, when="test")


def test_referrer_chain_truncates():
    """Rendering stops with a marker once max_lines nodes are reached."""
    obj = _Leaky()
    holders = [[obj] for _ in range(3)]
    lines, has_referrers = testing.referrer_chain(obj, max_lines=2)
    assert has_referrers
    assert lines[-1].endswith("... (truncated)")
    # and max_depth=1 keeps everything at the top level (no nesting)
    lines, _ = testing.referrer_chain(obj, max_depth=1)
    assert all(line.startswith(("├── ", "└── ")) for line in lines)
    del holders


def test_cycle_back_to_leaked_object_is_marked():
    """A referrer chain that reaches the survivor itself is marked as a cycle."""
    obj = _Leaky()
    obj.ref = obj  # self-reference cycle keeps obj findable via itself
    with pytest.raises(AssertionError) as exc_info:
        assert_no_instances(_Leaky, when="test")
    msg = str(exc_info.value)
    assert f"(cycle back to 0x{id(obj):x})" in msg
    del obj.ref
    assert_no_instances(_Leaky, when="test")


def test_already_expanded_referrer_is_marked():
    """A referrer already expanded elsewhere in the tree says '(see above)'."""
    obj = _Leaky()
    lst = [obj]
    d = {"lst": lst, "obj": obj}  # d is reachable both directly and via lst
    lines, has_referrers = testing.referrer_chain(obj)
    assert has_referrers
    text = "\n".join(lines)
    assert text.count("(see above)") == 1
    del lst, d


def test_instance_attribute_anchor():
    """An instance-attribute holder collapses to its owning instance."""

    class _Parent:
        pass

    parent = _Parent()
    parent.child = _Leaky()
    # Materialize __dict__ so it (not parent, via inline values) can be the
    # reported referrer on Python >= 3.11.
    assert vars(parent) == {"child": parent.child}
    with pytest.raises(AssertionError) as exc_info:
        assert_no_instances(_Leaky, when="test")
    msg = str(exc_info.value)
    assert "_Parent.__dict__['child']: dict = <len=1>" in msg
    del parent.child
    assert_no_instances(_Leaky, when="test")


def test_two_leaks_render_with_ids(reset_leaky, reset_holder_list):
    """Two survivors render as two sections, each id-tagged.

    Run with ``pytest -s`` to eyeball the full rendered failure message.
    """
    global _leaked, _holder_list
    _leaked = _Leaky()
    _holder_list = [_Leaky()]
    with pytest.raises(AssertionError) as exc_info:
        assert_no_instances(_Leaky, when="test")
    msg = str(exc_info.value)
    print(msg)
    assert f"Found 2 {__name__}._Leaky @ test:" in msg
    # section headers use the short class name (full name is in the summary)
    assert f"\n_Leaky @ 0x{id(_leaked):x}:" in msg
    assert f"\n_Leaky @ 0x{id(_holder_list[0]):x}:" in msg


def test_fullname_module_vs_class():
    """_fullname distinguishes modules from instances of user-defined classes."""
    assert _fullname(testing) == "refleak.testing"
    assert _fullname(_Leaky()) == f"{__name__}._Leaky"
    assert _fullname(_Leaky) == f"{__name__}._Leaky"  # classes name themselves

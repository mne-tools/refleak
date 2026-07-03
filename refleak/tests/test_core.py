"""Tests for refleak core functionality."""

# Authors: Eric Larson <larson.eric.d@gmail.com>
#
# License: BSD-3-Clause

import pytest

import refleak
from refleak._core import _fullname


class _Leaky:
    """A trivial class to instantiate and (optionally) leak."""


def test_assert_no_instances_passes_when_clean():
    """No live instances -> no assertion error."""
    obj = _Leaky()
    del obj
    refleak.assert_no_instances(_Leaky, when="test")


def test_assert_no_instances_reports_referrer_chain():
    """A leaked instance held by a module-level global is reported."""
    global _leaked
    _leaked = _Leaky()
    try:
        with pytest.raises(AssertionError, match="1 _Leaky @ test"):
            refleak.assert_no_instances(_Leaky, when="test")
    finally:
        del _leaked


def test_assert_no_instances_extra_info():
    """extra_info lines are prepended to a failing instance's report."""
    global _leaked
    _leaked = _Leaky()
    try:
        with pytest.raises(AssertionError, match="custom-marker"):
            refleak.assert_no_instances(
                _Leaky, when="test", extra_info=lambda obj: ["custom-marker"]
            )
    finally:
        del _leaked


def test_gc_collect_once_dedupes(request):
    """A second call with the same request is a no-op."""
    refleak.gc_collect_once(request)
    assert request.node._refleak_gc_collected is True
    refleak.gc_collect_once(request)  # does not raise, still a no-op


def test_referrer_chain_finds_referrers():
    """A list holding an object shows up as one of its referrers."""
    obj = _Leaky()
    holder = [obj]
    lines, has_referrers = refleak.referrer_chain(obj)
    assert has_referrers
    assert any("list" in line for line in lines)
    del holder


def test_fullname_module_vs_class():
    """_fullname distinguishes modules from instances of user-defined classes."""
    assert _fullname(refleak) == "refleak"
    assert _fullname(_Leaky()) == f"{__name__}._Leaky"

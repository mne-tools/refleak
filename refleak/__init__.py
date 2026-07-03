"""Find out what's still holding a reference to an object that should be dead."""

# Authors: Eric Larson <larson.eric.d@gmail.com>
#
# License: BSD-3-Clause

from importlib.metadata import version

from ._core import assert_no_instances, gc_collect_once, referrer_chain

try:
    __version__ = version("refleak")
except Exception:
    __version__ = "0.0.0"
del version

__all__ = [
    "__version__",
    "assert_no_instances",
    "gc_collect_once",
    "referrer_chain",
]

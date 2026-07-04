refleak
=======

Find out what's still holding a reference to an object that should be dead.

``refleak.testing.assert_no_instances(cls)`` checks that no instances of
``cls`` remain alive after garbage collection, and for any that do, reports a
rendered referrer chain -- what's still holding on to it, and (recursively)
what's holding on to *that* -- so tracking down a reference/GC leak (e.g. a
lingering Qt widget, VTK actor, or GUI object in tests) doesn't require
manually poking at ``gc.get_referrers`` by hand.

Extracted from ``mne.utils.misc._assert_no_instances``, developed over
several years of tracking down reference leaks in `MNE-Python
<https://github.com/mne-tools/mne-python>`__, `PyVista
<https://github.com/pyvista/pyvista>`__, and `pyvistaqt
<https://github.com/pyvista/pyvistaqt>`__.

Installation can be performed via ``pip``::

    pip install refleak

Usage
-----

.. code-block:: python

    import gc
    from refleak import testing


    class Leaky:
        pass


    _leaked = Leaky()  # e.g. accidentally kept alive by a module-level cache
    del _leaked
    gc.collect()
    testing.assert_no_instances(Leaky, when="after test")


A common pattern is a pytest fixture that runs the check on teardown:

.. code-block:: python

    import pytest
    from refleak.testing import assert_no_instances


    @pytest.fixture
    def check_no_leaked_widgets(request):
        yield
        assert_no_instances(MyWidget, when="test teardown", request=request)

When references are held, an AssertionError will be thrown. For example:

.. code-block:: python

    import gc
    from refleak import testing

    class Leaky:
        pass

    class ClingyParent:
        some_dict: dict

    leaked = Leaky()
    parent = ClingyParent()
    parent.some_dict = {"leak_1": leaked}  # e.g. accidentally kept alive by some object
    root_list = ["some_str", leaked, "some_other_str"]
    del leaked
    gc.collect()
    testing.assert_no_instances(Leaky, when="after test")

Would result in:

.. code-block:: console

    AssertionError:
    Found 1 __main__.Leaky @ after test:
    Leaky @ 0x102fe3e00:
    ├── dict['leak_1']: dict = <len=1>
    │   └── __main__.ClingyParent.__dict__['some_dict']: dict = <len=1>
    │       └── __main__.__dict__['parent']: dict = <len=15>
    └── __main__.root_list[1]: list = <len=3>

Comparison to similar packages
-------------------------------

There's no shortage of tools for poking at Python's garbage collector; here's
how ``refleak`` fits in relative to the ones people reach for most. "Monthly
downloads" is from `PyPI Stats <https://pypistats.org>`__ (July 2026) and
includes CI/mirror traffic, so treat it as a rough popularity signal rather
than a count of individual users. "Releases (5y)" counts releases in the
last five years as a rough maintenance signal.

.. list-table::
   :header-rows: 1

   * - Package
     - What it does
     - Monthly downloads
     - Latest release
     - Releases (5y)
   * - **refleak**
     - Assert no instances of a class remain alive; on failure, render the
       referrer chain keeping each survivor alive
     - new
     - --
     - --
   * - `objgraph <https://pypi.org/project/objgraph/>`__
     - General-purpose object-graph exploration: count objects by type,
       diff growth between snapshots, render backref/reference graphs via
       Graphviz
     - ~1.1M
     - 3.6.2 (Oct 2024)
     - 3
   * - `Pympler <https://pypi.org/project/Pympler/>`__
     - Broader memory-profiling suite: object sizing (``asizeof``), live
       monitoring (``muppy``), and class-level lifetime tracking
       (``ClassTracker``)
     - ~5.5M
     - 1.1 (Jun 2024)
     - 3
   * - `guppy3 <https://pypi.org/project/guppy3/>`__ (heapy)
     - Python 3 port of the classic ``guppy``/``heapy`` heap analysis
       toolset, with a query language for slicing the whole heap by type,
       size, or referrer
     - ~1.2M
     - 3.1.7 (May 2026)
     - 7
   * - `pytest-leaks <https://pypi.org/project/pytest-leaks/>`__
     - pytest plugin that reruns each test several times and watches
       ``sys.gettotalrefcount()`` for growth, rather than checking specific
       classes
     - ~2.4k
     - 0.3.1 (Nov 2019)
     - 0 (unmaintained)

None of the alternatives above do exactly what ``refleak`` does: assert that
no instances of a *specific class* remain alive and, on failure, explain
*why* via a rendered referrer chain, in a form meant to be dropped straight
into a test suite's teardown. ``objgraph`` and ``guppy3``/``heapy`` can
answer the same "why is this still alive" question (and go well beyond it,
e.g. full heap graphs and queries), but require driving their APIs
interactively or wiring up Graphviz output yourself rather than getting an
assertion with a readable message for free. ``Pympler`` is aimed more at
memory *sizing* and monitoring over time than one-shot leak assertions.
``pytest-leaks`` checks for leaks generically (via total refcount growth
across repeated runs) instead of targeting specific classes, so it can flag
*that* something leaked without telling you *what* or *why*. If you need
full heap introspection or memory-size profiling, reach for ``objgraph`` or
``Pympler``/``guppy3`` instead; if you just want a pytest-friendly assertion
that a GUI widget, VTK actor, or other object didn't leak, and a readable
explanation when it did, that's what ``refleak`` is for.

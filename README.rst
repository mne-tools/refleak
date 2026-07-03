refleak
=======

Find out what's still holding a reference to an object that should be dead.

``refleak.assert_no_instances(cls)`` checks that no instances of ``cls``
remain alive after garbage collection, and for any that do, reports a
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
    import refleak


    class Leaky:
        pass


    _leaked = Leaky()  # e.g. accidentally kept alive by a module-level cache
    del _leaked
    gc.collect()
    refleak.assert_no_instances(Leaky, when="after test")

A common pattern is a pytest fixture that runs the check on teardown:

.. code-block:: python

    import pytest
    import refleak


    @pytest.fixture
    def check_no_leaked_widgets(request):
        yield
        refleak.assert_no_instances(MyWidget, when="test teardown", request=request)

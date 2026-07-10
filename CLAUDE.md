# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`refleak` is a small, dependency-free library for finding what still holds a
reference to an object that should be dead. The main entry points are
`refleak.testing.assert_no_instances(cls, when=...)`, which asserts that no
instances of `cls` survive `gc.collect()`, and `refleak.testing.Snapshot`,
which records already-existing matching objects up front so only *new*
survivors are flagged (`snap = Snapshot(match)` ... `snap.assert_no_new()`).
On failure both render a box-drawing referrer chain explaining *why* each
survivor is still alive. It was extracted from
`mne.utils.misc._assert_no_instances` and targets test teardown for
GUI/native-object leaks (Qt widgets, VTK actors, PyVista).

## Commands

```bash
pip install -e .[test]      # install with test deps (pytest, pytest-cov)
pytest refleak              # run the full suite (config in pyproject.toml)
pytest refleak/testing/tests/test_core.py::test_assert_no_instances  # single test
pre-commit run --all-files  # ruff (check+format), ty, yamllint, toml-sort, codespell
ty check                    # type-check only (against Python 3.10, the minimum)
```

Note: `pytest` runs with `--doctest-modules`, so docstring examples in
`_core.py` (and the referrer-chain output shown there) are executed as tests —
keep them accurate when editing docstrings.

## Architecture

Everything lives in `refleak/testing/_core.py`; the `__init__.py` files only
re-export the four public names (`Snapshot`, `assert_no_instances`,
`gc_collect_once`, `referrer_chain`). The referrer-tracing pipeline flows
top-down:

- `assert_no_instances` — scans `gc.get_objects()` for `isinstance(obj, cls)`
  survivors and asserts there are none.
- `Snapshot` — records `id()`s of matching objects at construction (types use
  `isinstance`, anything else is a predicate callable); `assert_no_new()`
  re-scans and asserts nothing matching appeared since and survived. Stores
  only ids (pins nothing alive); the documented caveat is id reuse (false
  negatives only), minimized by the constructor's `gc.collect()`.
- Both share `_match_objects` (a raising match check counts as a miss) and
  `_build_report` (per-survivor `referrer_chain` + message lines; survivors
  with no non-excluded referrers don't count).
- `referrer_chain` — public tracer: walks `gc.get_referrers` up to `max_depth`
  hops / `max_lines` nodes and returns rendered lines.
- `_referrer_tree` — the recursion. Shared mutable state threaded through the
  call: `count` (1-element list, a global node cap), `excluded` (ids never
  shown/recursed, e.g. the traversal's own containers), `recursed` (ids already
  expanded, to break reference cycles).
- `_describe_referrer` — formats one referrer as `name: type = repr` and
  decides `next_obj` (what to recurse into next, or `None` to stop). It
  collapses noise: an instance `__dict__` is reported as its owning instance,
  a closure cell is reported as `func.__closure__['varname']` and recursion
  continues from the owning function (skipping the anonymous closure tuple),
  and a module-level global or module attribute is named directly
  (`module.attr` / `module.__dict__['attr']`) and stops the walk — who holds
  the *module* is never the actionable part.

Two correctness constraints that pervade the code:

- **The traversal must exclude its own state.** `gc.get_referrers` will report
  the lists/dicts/globals the tracer itself is holding as if they were real
  anchors. Callers pass `exclude_ids` (see `assert_no_instances` excluding
  `id(objs)`, `id(ref)`, `id(globals())`), and `_referrer_tree` adds
  `id(refs)` each level. Only *live-stack* frames are skipped (recomputed via
  `_live_frame_ids()` at each check so the traversal's own frames count);
  dead-but-referenced frames (stored tracebacks, suspended generators) are
  real anchors and are reported. When adding state that touches traced
  objects, exclude its id too.
- **Reporting must never raise.** Survivors can be half-destroyed native
  objects, so all `repr()` goes through `_safe_repr`, and `isinstance` checks
  are wrapped (weakrefs etc. can raise).

`gc_collect_once(request)` deduplicates `gc.collect()` per pytest test item
(via a `_refleak_gc_collected` attr on the node) because collect cost scales
with total tracked objects and multiple teardown fixtures would otherwise each
pay it.

## Conventions

- Python >= 3.10; no runtime dependencies (pytest is only referenced via an
  optional `request` argument, never imported).
- Line length 88; ruff enforces pep257 docstrings. Every function has a
  docstring; private helpers are prefixed `_`.
- Version is derived from git tags via `setuptools_scm` (no hardcoded version).

"""Shared step-key enum for the run drawer.

``agent/runner.py`` emits ``SCOUT_PROGRESS`` events tagged with a ``key``
(e.g. ``"scrape"``, ``"enrich"``); ``app/main.py`` matches those keys against
its ``GLOBAL_STEPS``/``SEARCH_STEPS`` scaffolding to update the right step in
the drawer. Both sides import this enum instead of retyping the string
literals, so a typo or rename is an ``ImportError``/``AttributeError``
instead of a step that silently never updates in the UI.
"""

from enum import StrEnum


class StepKey(StrEnum):
    """Step-key values shared by runner.py and main.py.

    A ``StrEnum`` rather than a plain class so membership is closed and
    enumerable, while each member still behaves as its plain string value —
    ``json.dumps`` in ``runner.py::emit`` and the ``==`` comparisons against
    parsed-JSON strings in ``main.py::_find_step`` both work unchanged.
    """

    START = "start"
    SCRAPE = "scrape"
    FILTER = "filter"
    CLEAN = "clean"
    ENRICH = "enrich"
    SAVE = "save"

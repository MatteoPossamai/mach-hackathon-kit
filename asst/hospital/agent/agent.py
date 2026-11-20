"""Care-protocol example. ``city init`` does not copy this file.

The scaffold brain is ``templates/agent/agent/agent.py``. This one is the full care
protocol (prescribe, offer, hold, commit, check-in, report), one inbound message per
step. The role is ``role`` in ``card.yaml``. Ledger writes are chat to ``city:ledger``;
citizens read the act word and ``key=value`` tokens (ADR-110).
"""

# The code is split into the hosp_*.py files next to this one, one per section. They are
# executed in order in this module's namespace, so they behave exactly like a single file:
# same globals, no import cycles, and tests can still patch names on this module.
from pathlib import Path as _Path

_HERE = _Path(__file__).resolve().parent
_PARTS = ('hosp_00_base', 'hosp_01_shared', 'hosp_02_medico_di_base', 'hosp_03_centro_prenotazioni_and', 'hosp_04_facility', 'hosp_05_proactive')
for _part in _PARTS:
    _file = _HERE / f"{_part}.py"
    exec(compile(_file.read_text(encoding="utf-8"), str(_file), "exec"), globals())
del _part, _file

"""The schema's CHECK constraints and the enums in states.py list the same values."""
import re

import pytest

from incident_operator import db
from incident_operator.states import FixStatus, IncidentStatus, TurnStatus


def check_values(constraint_start: str) -> set[str]:
    """The values of the last `CHECK (status IN (...))` after `constraint_start` (the ALTERs come after the CREATEs)."""
    at = db.SCHEMA.rindex(constraint_start)
    m = re.search(r"CHECK \(status IN \(([^)]*)\)\)", db.SCHEMA[at:])
    assert m
    return set(re.findall(r"'(\w+)'", m.group(1)))


@pytest.mark.parametrize("start,enum", [("ADD CONSTRAINT incidents_status_check", IncidentStatus),
                                        ("ADD CONSTRAINT op_runs_status_check", FixStatus),
                                        ("CREATE TABLE IF NOT EXISTS model_turns", TurnStatus)])
def test_schema_allows_exactly_the_enum(start, enum):
    assert check_values(start) == {s.value for s in enum}

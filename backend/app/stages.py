"""Small, safe append-only records shared by pipeline workers."""
from datetime import datetime, timezone
from uuid import uuid4

from .db import EventStage

POLICY_VERSION = 'rules-shadow-v1'


def append_stage(session, *, person_id, stage, outcome, source, when,
                 candidate_id=None, event_id=None, urgency=None,
                 policy_version=POLICY_VERSION, details=None):
    row = EventStage(
        id=str(uuid4()), ts=datetime.fromtimestamp(when, timezone.utc),
        person_id=person_id, candidate_id=candidate_id, event_id=event_id,
        stage=stage, outcome=outcome, urgency=urgency, source=source,
        policy_version=policy_version, details=details or {},
    )
    session.add(row)
    return row

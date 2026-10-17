from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Landmark(StrictModel):
    x: float = Field(ge=-5, le=5)
    y: float = Field(ge=-5, le=5)
    z: float = Field(default=0, ge=-20, le=20)
    visibility: float = Field(ge=0, le=1)


class Pose(StrictModel):
    track_id: Identifier
    landmarks: list[Landmark] = Field(min_length=33, max_length=33)


class PoseFrame(StrictModel):
    session_id: Identifier
    camera_id: Identifier
    person_id: Identifier
    timestamp: float = Field(gt=0)
    poses: list[Pose] = Field(max_length=2)
    monitored_track_id: Identifier
    tracking_ambiguous: bool = False
    source: Literal['live', 'replay'] = 'live'
    demo_time_scale: float = Field(default=1, ge=1, le=8)
    region: 'BedZone | None' = None

    @model_validator(mode='after')
    def distinct_tracks(self):
        if len({p.track_id for p in self.poses}) != len(self.poses):
            raise ValueError('Pose track ids must be distinct')
        return self


class Contact(StrictModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=80)
    role: str = Field(default='Trusted contact', max_length=80)
    channel: Literal['push', 'call'] = 'push'
    phone: str = Field(default='', max_length=24, pattern=r'^(\+[1-9]\d{7,14})?$')
    min_urgency: int = Field(default=1, ge=0, le=3)
    delay_seconds: int = Field(default=0, ge=0, le=3600)


class BedZone(StrictModel):
    x1: float = Field(default=0.05, ge=0, le=1)
    y1: float = Field(default=0.25, ge=0, le=1)
    x2: float = Field(default=0.35, ge=0, le=1)
    y2: float = Field(default=0.95, ge=0, le=1)

    @model_validator(mode='after')
    def ordered(self):
        if self.x1 >= self.x2 or self.y1 >= self.y2:
            raise ValueError('Bed zone corners must be ordered')
        return self


class Profile(StrictModel):
    id: Identifier
    version: int = Field(default=1, ge=1)
    alias: str = Field(min_length=1, max_length=60)
    age: int = Field(default=71, ge=18, le=120)
    mode: Literal['care', 'personal']
    timezone: str = 'America/Toronto'
    conditions: list[str] = Field(default_factory=list, max_length=20)
    medication_flags: list[str] = Field(default_factory=list, max_length=10)
    mobility_aid: Literal['none', 'walker', 'cane', 'wheelchair'] = 'none'
    restrict_night_standing: bool = False
    quiet_start: int = Field(default=22, ge=0, le=23)
    quiet_end: int = Field(default=7, ge=0, le=23)
    bed_zone: BedZone | None = None
    check_in_first: bool = True
    check_in_seconds: int = Field(default=60, ge=10, le=300)
    contacts: list[Contact] = Field(default_factory=list, max_length=10)
    privacy: Literal['skeleton_only'] = 'skeleton_only'
    retain_days: int = Field(default=7, ge=1, le=30)
    consent: bool = True
    care_notes: str = Field(default='', max_length=1000)
    risk_categories: list[Literal['balance_support', 'mobility_support', 'transfer_assistance', 'medication_fall_risk']] = Field(default_factory=list, max_length=4)
    gemini_consent: bool = False
    data_category: Literal['synthetic', 'consenting_teammate'] = 'synthetic'

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError('Unknown IANA timezone')
        return value

    @model_validator(mode='after')
    def unique_contacts(self):
        if len({c.id for c in self.contacts}) != len(self.contacts):
            raise ValueError('Contact ids must be unique')
        if any(len(c) > 120 for c in self.conditions + self.medication_flags):
            raise ValueError('Profile labels must be at most 120 characters')
        return self


class Evidence(StrictModel):
    ref: str = Field(max_length=120)
    detail: str = Field(max_length=300)


class Verdict(StrictModel):
    verdict: Literal['fall', 'near_fall', 'unsteady', 'restriction_breach', 'bed_exit', 'inactivity', 'normal_activity', 'unclear']
    urgency: int = Field(ge=0, le=3)
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1, max_length=400)
    evidence: list[Evidence] = Field(min_length=1, max_length=12)
    profile_factors: list[str] = Field(default_factory=list, max_length=10)
    recommended_actions: list[str] = Field(min_length=1, max_length=5)
    needs_human_review: bool = True


class Acknowledgement(StrictModel):
    author: str = Field(default='Operator', min_length=1, max_length=80)
    note: str = Field(default='', max_length=500)


class Feedback(StrictModel):
    label: Literal['confirmed', 'false_alarm', 'uncertain']
    note: str = Field(default='', max_length=500)


class NotificationOut(StrictModel):
    id: str
    recipient: str
    channel: str
    urgency: int
    state: str
    attempts: int
    due_at: float
    last_error: str | None


class EventOut(StrictModel):
    id: str
    person_id: str
    alias: str
    mode: str
    profile_version: int
    source: str
    opened_at: float
    observed_at: float
    updated_at: float
    rule_hits: list[str]
    floor_urgency: int
    urgency: int
    status: str
    summary: str
    verdict_status: str
    verdict: Verdict | None
    features: dict[str, float | str | bool | None]
    keyframes: list[dict]
    acknowledged_at: float | None
    acknowledged_by: str | None
    next_escalation_at: float | None
    notifications: list[NotificationOut]
    rule_summary: str = ''
    human_review: str | None = None
    floor_decision: dict | None = None
    baseline_contributions: list[dict] = Field(default_factory=list)


class IngestResult(StrictModel):
    accepted: bool
    reason: str
    quality: str
    features: dict[str, float | str | bool | None] = Field(default_factory=dict)
    rule_hits: list[str] = Field(default_factory=list)
    event_id: str | None = None
    anomaly_score: float | None = None
    anchor_score: float | None = None
    baseline_contributions: list[dict] = Field(default_factory=list)
    baseline_state: str = 'uncalibrated'


class Login(StrictModel):
    token: str = Field(max_length=512)


class PushRegistration(StrictModel):
    person_id: Identifier
    contact_id: Identifier
    endpoint: str = Field(max_length=2048)
    keys: dict[str, str]


PoseFrame.model_rebuild()

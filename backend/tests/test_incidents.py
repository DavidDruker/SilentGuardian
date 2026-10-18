"""Live concern, timestamped incidents, and the ML gate around falls."""
import asyncio
import json

from backend.app.gemini import GeminiWorker
from backend.app.replay import scenario_frames


def gate(guardian, fall_score):
    guardian.settings.decision_mode = 'enforce'
    guardian.score_candidate = lambda candidate, samples: {
        'status': 'scored', 'model': 'test', 'fall_score': fall_score,
        'staggering_score': .01, 'latency_ms': 1}


def run(guardian, name, start, session, duration):
    """Scored results only; frames between scoring windows carry no reading."""
    results = [guardian.ingest(frame) for frame in
               scenario_frames(name, 'daniel', start=start, duration=duration, session_id=session)]
    return [r for r in results if r.quality != 'buffering']


def test_fall_stays_a_concern_while_down_and_clears_once_up(guardian):
    gate(guardian, .9)
    t0 = guardian.clock()
    down = run(guardian, 'fall', t0, 'clip', 8)
    assert down[-1].concern == 'on_floor'  # Fell and still on the ground, not moving.
    up = run(guardian, 'normal', t0+8, 'clip', 6)
    assert up[-1].concern is None  # Up and settled: no alert at this moment.
    [alert] = guardian.events()
    assert alert.rule_hits == ['fall'] and alert.status == 'open'  # Still logged.


def test_each_separate_fall_is_logged_with_its_own_time(guardian):
    gate(guardian, .9)
    t0 = guardian.clock()
    run(guardian, 'fall', t0, 'clip', 8)
    run(guardian, 'normal', t0+8, 'clip', 6)
    run(guardian, 'fall', t0+14, 'clip', 8)
    falls = sorted(e.observed_at for e in guardian.events() if e.rule_hits == ['fall'])
    assert len(falls) == 2 and falls[1]-falls[0] > 10


def test_rejected_drop_is_not_an_alert_but_lying_still_is_watched(guardian):
    gate(guardian, .01)
    results = run(guardian, 'fall', guardian.clock(), 'clip', 8)
    assert guardian.events() == []
    assert results[-1].candidate_decision is None and results[-1].concern == 'on_floor_watch'
    assert any(r.candidate_decision == 'rejected' for r in results)


def test_motionless_on_floor_alerts_even_after_ml_rejected_the_drop(guardian):
    # A still body never looks like "falling" to the action model, so stillness
    # after a drop uses the rule fallback instead of the ML score.
    gate(guardian, .01)
    results = run(guardian, 'long_lie', guardian.clock(), 'clip', 40)
    [alert] = guardian.events()
    assert alert.rule_hits == ['prolonged_stillness'] and alert.verdict_status == 'model_fallback'
    assert results[-1].concern == 'on_floor'


def test_gemini_can_raise_but_never_lower_urgency(guardian):
    gate(guardian, .9)
    guardian.settings.gemini_api_key = 'test'
    guardian.settings.gemini_billed_project_confirmed = True
    run(guardian, 'fall', guardian.clock(), 'clip', 4)
    worker = GeminiWorker(guardian)
    def verdict(urgency):
        async def review(package, frames):
            return json.dumps({'verdict': 'fall', 'urgency': urgency, 'confidence': .8,
                               'summary': 'A possible fall was observed.',
                               'evidence': [{'ref': 'ml.supported', 'detail': 'Model supported the rule'}],
                               'recommended_actions': ['Check on the person'], 'profile_factors': []})
        return review
    worker.request = verdict(1)
    asyncio.run(worker.tick())
    lowered = guardian.events()[0]
    assert lowered.verdict_status == 'validated' and lowered.urgency == 2
    run(guardian, 'normal', guardian.clock()+10, 'other', 6)
    run(guardian, 'fall', guardian.clock()+16, 'other', 4)
    worker.request = verdict(3)
    asyncio.run(worker.tick())
    raised = next(e for e in guardian.events() if e.id != lowered.id)
    assert raised.urgency == 3

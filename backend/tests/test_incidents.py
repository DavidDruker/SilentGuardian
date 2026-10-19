"""Live concern, timestamped incidents, and the ML gate around falls."""
import asyncio
import math
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
    # Confirmed falls are Urgent even when Gemini proposes a lower level.
    assert lowered.verdict_status == 'validated' and lowered.urgency == 3
    run(guardian, 'normal', guardian.clock()+10, 'other', 6)
    run(guardian, 'fall', guardian.clock()+16, 'other', 4)
    worker.request = verdict(3)
    asyncio.run(worker.tick())
    raised = next(e for e in guardian.events() if e.id != lowered.id)
    assert raised.urgency == 3


def test_face_down_toward_camera_is_lying_not_standing():
    from backend.app.detector import sample
    from backend.app.replay import skeleton
    from backend.app.schemas import PoseFrame
    from backend.app.seed import demo_profiles
    profile = demo_profiles()[1]
    def posture(angle):
        frame = PoseFrame(session_id='s', camera_id='c', person_id=profile.id, timestamp=1,
                          poses=[{'track_id': 'subject', 'landmarks': skeleton(angle=angle)}],
                          monitored_track_id='subject', source='upload')
        return sample(frame, profile)['posture']
    # Rotated 180 degrees: head at the bottom of the image, feet at the top, torso
    # still vertical. This is how a person lying face-down toward the camera looks.
    assert posture(0) == 'standing'
    assert posture(math.pi) == 'lying'


def test_upright_then_lying_is_a_fall_even_without_image_hip_drop(guardian):
    # A camera that follows the person down hides the hip drop; the posture change
    # from upright to lying on the floor still marks the fall.
    from backend.app.replay import skeleton
    from backend.app.schemas import PoseFrame
    gate(guardian, .9)
    start = guardian.clock()
    results = []
    for i in range(90):
        t = i / 15
        angle = 0 if t < 2 else min(math.pi, (t - 2) / .5 * math.pi)
        frame = PoseFrame(session_id='turn', camera_id='browser-camera', person_id='daniel', timestamp=start + t,
                          poses=[{'track_id': 'subject', 'landmarks': skeleton(angle=angle)}],
                          monitored_track_id='subject', source='upload')
        results.append(guardian.ingest(frame))
    assert any('fall' in r.rule_hits for r in results)
    assert [e.rule_hits for e in guardian.events()] == [['fall']]
    assert [r for r in results if r.quality == 'good'][-1].concern == 'on_floor'


def test_restricted_hours_standing_is_logged_once_per_video(guardian):
    gate(guardian, .9)
    start = guardian.clock()
    for offset in (0, 20, 40):  # Three separate stretches of standing, same video.
        for frame in scenario_frames('normal', 'margaret', start=start + offset, duration=6, session_id='night'):
            guardian.ingest(frame)
    assert sum(e.rule_hits == ['restricted_standing'] for e in guardian.events()) == 1


def test_safety_rule_alert_also_goes_to_gemini(guardian):
    guardian.settings.decision_mode = 'enforce'
    guardian.settings.gemini_api_key = 'test'
    guardian.settings.gemini_billed_project_confirmed = True
    guardian.score_candidate = lambda candidate, samples: None  # ML not running
    run(guardian, 'fall', guardian.clock(), 'clip', 4)
    alert = guardian.events()[0]
    assert alert.verdict_status == 'pending' and alert.status == 'open'
    worker = GeminiWorker(guardian)
    async def review(package, frames):
        assert package['model_evidence']['decision'] == 'fallback_open'
        return json.dumps({'verdict': 'fall', 'urgency': 2, 'confidence': .7,
                           'summary': 'A possible fall was observed.',
                           'evidence': [{'ref': 'rule.safety_fallback', 'detail': 'Safety rule raised the alert'}],
                           'recommended_actions': ['Check on the person'], 'profile_factors': []})
    worker.request = review
    asyncio.run(worker.tick())
    assert guardian.events()[0].verdict_status == 'validated'


def test_one_gemini_review_per_video_for_its_most_urgent_alert(guardian):
    from backend.app.replay import skeleton
    from backend.app.schemas import PoseFrame
    guardian.settings.decision_mode = 'enforce'
    guardian.settings.gemini_api_key = 'test'
    guardian.settings.gemini_billed_project_confirmed = True
    guardian.score_candidate = lambda candidate, samples: {
        'status': 'scored', 'model': 'test', 'fall_score': .9, 'staggering_score': .9, 'latency_ms': 1}
    start = guardian.clock()
    def frames(name, offset, duration):
        return [f.model_copy(update={'source': 'upload', 'camera_id': 'browser-camera'})
                for f in scenario_frames(name, 'daniel', start=start + offset, duration=duration, session_id='clip')]
    for frame in frames('stumble', 0, 6) + frames('fall', 12, 6):  # stumble first, then a fall
        guardian.ingest(frame)
    alerts = {e.rule_hits[0]: e for e in guardian.events()}
    assert {alerts['stumble'].verdict_status, alerts['fall'].verdict_status} == {'pending'}
    worker = GeminiWorker(guardian)
    reviewed = []
    async def review(package, frames):
        reviewed.append(package['rules'])
        return json.dumps({'verdict': 'fall', 'urgency': 2, 'confidence': .8, 'summary': 'A possible fall was observed.',
                           'evidence': [{'ref': 'ml.supported', 'detail': 'Model supported the rule'}],
                           'recommended_actions': ['Check on the person'], 'profile_factors': []})
    worker.request = review
    asyncio.run(worker.tick())
    assert reviewed == []  # the video is still sending frames
    guardian.clock.advance(5)
    for _ in range(3):
        asyncio.run(worker.tick())
    assert reviewed == [['fall']]
    alerts = {e.rule_hits[0]: e for e in guardian.events()}
    assert alerts['fall'].verdict_status == 'validated' and alerts['stumble'].verdict_status == 'not_reviewed'

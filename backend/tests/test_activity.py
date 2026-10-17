from uuid import uuid4

from sqlalchemy import select

from backend.app.db import EventStage
from backend.app.replay import scenario_frames


def test_upload_verdict_and_incident_grid_are_session_scoped(client):
    assert client.post('/api/session', json={'token': 'local-demo-only'}).status_code == 200
    from backend.app.schemas import Profile
    profile = Profile(id='clip-person', alias='Clip person', mode='personal',
                      consent=True, data_category='consenting_teammate')
    assert client.put('/api/profiles/clip-person', json=profile.model_dump()).status_code == 200
    session = str(uuid4())
    now = client.app.state.guardian.clock()
    for frame in scenario_frames('fall', 'clip-person', start=now-10, session_id=session):
        body = frame.model_copy(update={'camera_id': 'browser-camera', 'source': 'upload'})
        assert client.post('/api/frames', json=body.model_dump()).status_code == 200
    verdict = client.get(f'/api/capture-result/clip-person/{session}').json()
    assert verdict['status'] == 'ready'
    assert verdict['scored_windows'] > 0
    assert verdict['event']['source'] == 'upload'
    assert client.get(f'/api/capture-result/clip-person/{uuid4()}').json()['event'] is None

    start, end = now-86400, now+86400
    timeline = client.get(f'/api/activity/clip-person/timeline?from_ts={start}&to_ts={end}').json()
    assert timeline['total'] == 1
    assert [s['stage'] for s in timeline['incidents'][0]['stages']][:2] == ['candidate', 'incident_opened']
    grid = client.get('/api/activity/clip-person/heatmap').json()
    assert sum(c['incidents'] for day in grid['days'] for c in day['hours']) == 1
    assert any(c['covered'] for day in grid['days'] for c in day['hours'])
    active_cell = next((day['day'], cell['hour']) for day in grid['days'] for cell in day['hours'] if cell['incidents'])
    selected = client.get(f'/api/activity/clip-person/cell?day={active_cell[0]}&hour={active_cell[1]}').json()
    assert selected['total'] == 1 and selected['incidents'][0]['id'] == verdict['event']['id']

    guardian = client.app.state.guardian
    with guardian.db.sessions() as session_db:
        assert len(list(session_db.scalars(select(EventStage)))) >= 2

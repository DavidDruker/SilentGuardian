from uuid import uuid4

from backend.app.replay import scenario_frames


def auth(client):
    assert client.post('/api/session',json={'token':'local-demo-only'}).status_code==200


def test_auth_validation_and_origin(client):
    assert client.get('/healthz').status_code==200
    assert client.get('/api/profiles').status_code==401
    assert client.post('/api/session',json={'token':'wrong'}).status_code==401
    auth(client)
    assert len(client.get('/api/profiles').json())==2
    assert client.post('/api/frames',json={},headers={'origin':'https://evil.example'}).status_code==403
    assert client.post('/api/frames',json={}).status_code==422


def test_real_profile_and_local_video_without_demo(tmp_path, clock):
    from fastapi.testclient import TestClient
    from backend.app.config import Settings
    from backend.app.main import create_app
    from backend.app.schemas import Profile

    cfg = Settings.model_construct(database_url=f'sqlite:///{tmp_path / "real.db"}',
                                   worker_enabled=False, demo_enabled=False)
    with TestClient(create_app(cfg, clock=clock)) as app:
        auth(app)
        assert app.get('/api/profiles').json() == []
        profile = Profile(id='real-person', alias='Test person', age=17, mode='personal',
                          conditions=['Vertigo'], medication_flags=['May affect balance'],
                          care_notes='Needs a handrail on stairs', data_category='consenting_teammate')
        assert app.put('/api/profiles/real-person', json=profile.model_dump()).status_code == 200
        saved = app.get('/api/profiles').json()[0]
        assert saved['age'] == 17 and saved['conditions'] == ['Vertigo']
        frame = next(scenario_frames('normal', 'daniel')).model_copy(
            update={'person_id': 'real-person', 'source': 'upload'})
        assert app.post('/api/frames', json=frame.model_dump()).status_code == 200
        assert app.get('/api/demo/scenarios/normal?person_id=real-person').status_code == 404


def test_websocket_reconnect_and_identity_binding(client):
    auth(client)
    frames=list(scenario_frames('fall','daniel'))
    with client.websocket_connect('/ws/pose',headers={'origin':'http://localhost:8000'}) as ws:
        for frame in frames[:45]:
            ws.send_json(frame.model_dump()); assert 'error' not in ws.receive_json()
    with client.websocket_connect('/ws/pose',headers={'origin':'http://localhost:8000'}) as ws:
        for frame in frames[40:80]:
            ws.send_json(frame.model_dump()); result=ws.receive_json()
        changed=frames[81].model_copy(update={'person_id':'margaret'})
        ws.send_json(changed.model_dump());assert 'error' in ws.receive_json()
    assert len(client.get('/api/events').json())==1


def test_contact_link_scoped_and_ack_idempotent(client):
    auth(client)
    for frame in scenario_frames('fall','daniel'):
        assert client.post('/api/frames',json=frame.model_dump()).status_code==200
    ev=client.get('/api/events').json()[0]
    link=client.get(f'/api/events/{ev["id"]}/contact-link/son').json()['url']
    token=link.rsplit('/',1)[1]
    headers={'authorization':f'Bearer {token}'}
    client.delete('/api/session')
    assert client.get('/api/profiles',headers=headers).status_code==401
    view=client.get(f'/api/contact/{ev["id"]}/son',headers=headers)
    assert view.status_code==200 and 'conditions' not in view.text and 'contacts' not in view.text
    assert client.get(f'/api/contact/{ev["id"]}/neighbour',headers=headers).status_code==401
    for _ in range(2):
        assert client.post(f'/api/contact/{ev["id"]}/son/ack',headers=headers,json={'note':'On my way'}).status_code==200


def test_demo_call_and_push_validation(client):
    auth(client)
    request=str(uuid4())
    a=client.post(f'/api/demo/call/{request}').json()
    b=client.post(f'/api/demo/call/{request}').json()
    assert a==b
    assert client.post('/api/push',json={'person_id':'daniel','contact_id':'son','endpoint':'http://127.0.0.1/private','keys':{'auth':'x','p256dh':'y'}}).status_code==422


def test_vite_dev_origin_and_spaced_origin_lists(client, tmp_path, clock):
    from fastapi.testclient import TestClient
    from backend.app.config import Settings
    from backend.app.main import create_app
    assert client.post('/api/session',json={'token':'local-demo-only'},headers={'origin':'http://127.0.0.1:5173'}).status_code==200
    cfg=Settings.model_construct(database_url=f'sqlite:///{tmp_path / "origins.db"}', worker_enabled=False,
                                 allowed_origins='https://a.example, https://guardian.example/ ,')
    with TestClient(create_app(cfg, clock=clock)) as other:
        ok=other.post('/api/session',json={'token':'local-demo-only'},headers={'origin':'https://guardian.example'})
        assert ok.status_code==200
        assert other.post('/api/session',json={'token':'local-demo-only'},headers={'origin':'http://localhost:8000'}).status_code==403


def test_production_allows_labelled_demo_but_never_weak_secrets():
    import pytest
    from backend.app.config import Settings
    strong = dict(app_env='production', operator_token='o'*40, signing_secret='s'*40,
                  public_url='https://guardian.example', demo_enabled=True)
    Settings.model_construct(**{**strong, 'database_url': 'postgresql+psycopg://example:secret@localhost/guardian'}).validate_runtime()
    with pytest.raises(ValueError, match='DATABASE_URL'):
        Settings.model_construct(**strong).validate_runtime()
    with pytest.raises(ValueError, match='secrets'):
        Settings.model_construct(**{**strong, 'database_url': 'postgresql+psycopg://example:secret@localhost/guardian',
                                    'signing_secret': 'local-demo-signing-secret-change-before-deploy'}).validate_runtime()
    with pytest.raises(ValueError, match='HTTPS'):
        Settings.model_construct(**{**strong, 'database_url': 'postgresql+psycopg://example:secret@localhost/guardian',
                                    'public_url': 'http://guardian.example'}).validate_runtime()

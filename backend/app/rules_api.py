"""Stateless private rules service. The app owns consent, profile and stream state."""
from fastapi import Depends, FastAPI, HTTPException

from .detector import evaluate
from .internal import internal_auth
from .schemas import RulesRequest, RulesResponse

app = FastAPI(title='Silent Guardian rules service')


@app.get('/healthz')
def health():
    return {'status': 'ok', 'service': 'rules'}


@app.post('/internal/evaluate', response_model=RulesResponse, dependencies=[Depends(internal_auth)])
def score(body: RulesRequest):
    if body.schema_version != 1 or body.profile_version != body.profile.version or body.frame.person_id != body.profile.id:
        raise HTTPException(409, 'Stale or mismatched rules request')
    state, result, reason = evaluate(body.frame, body.profile, body.state)
    return RulesResponse(correlation_id=body.correlation_id, state=state, result=result, reason=reason)

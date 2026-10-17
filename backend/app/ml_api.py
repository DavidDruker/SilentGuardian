"""Private, bounded ST-GCN++ inference service."""
from contextlib import asynccontextmanager
from threading import BoundedSemaphore

from fastapi import Depends, FastAPI, HTTPException

from .action_model import ActionModel, MODEL_ID, prepare_sequence
from .config import settings
from .internal import internal_auth
from .schemas import MLRequest

semaphore = BoundedSemaphore(1)
model = None
load_error = None


@asynccontextmanager
async def lifespan(app):
    global model, load_error
    try:
        model = ActionModel(settings.action_model_path, settings.action_model_threads)
        load_error = None
    except Exception as exc:
        model = None
        load_error = type(exc).__name__
    yield
    model = None


app = FastAPI(title='Silent Guardian ML service', lifespan=lifespan)


@app.get('/healthz')
def health():
    return {'status': 'ready' if model else 'unavailable', 'service': 'ml',
            'model': MODEL_ID, 'error': load_error}


@app.post('/internal/predict', dependencies=[Depends(internal_auth)])
def predict(body: MLRequest):
    if body.schema_version != 1 or body.quality != 'good':
        raise HTTPException(422, 'Unsupported schema or low-quality skeleton')
    if model is None:
        raise HTTPException(503, 'Model unavailable')
    if not semaphore.acquire(blocking=False):
        raise HTTPException(503, 'Model busy')
    try:
        prepare_sequence(body.samples)
        result = model.predict(body.samples)
        return {**result, 'correlation_id': body.correlation_id,
                'candidate_id': body.candidate_id, 'idempotency_key': body.idempotency_key,
                'profile_version': body.profile_version}
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    finally:
        semaphore.release()

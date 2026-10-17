import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.db import Database
from backend.app.main import create_app
from backend.app.service import Guardian


class Clock:
    def __init__(self):
        self.now = 1792217400.0
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def guardian(tmp_path, clock):
    db=Database(f'sqlite:///{tmp_path / "test.db"}')
    db.create()
    # Do not inherit live credentials or switches from the developer's .env.
    g=Guardian(db, Settings.model_construct(worker_enabled=False, demo_enabled=True), clock)
    g.seed()
    return g


@pytest.fixture
def client(tmp_path, clock):
    cfg=Settings.model_construct(database_url=f'sqlite:///{tmp_path / "api.db"}', worker_enabled=False, demo_enabled=True)
    with TestClient(create_app(cfg, clock=clock)) as client:
        yield client

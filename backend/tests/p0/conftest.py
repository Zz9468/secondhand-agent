import os

import pytest
from sqlalchemy.engine import make_url

from app.p0.store import Store, metadata


@pytest.fixture(scope="session")
def store():
    if os.getenv("RUN_P0_INTEGRATION") != "1":
        pytest.skip("use python -m scripts.run_p0_tests for isolated real databases")
    url = os.environ["P0_MYSQL_URL"]
    assert make_url(url).database.startswith("p0_test_")
    instance = Store(url)
    metadata.create_all(instance.engine)
    yield instance
    instance.engine.dispose()


@pytest.fixture
def run(store):
    return store.enqueue(store.seed(), "request-1", {"text": "P0 product query"})


@pytest.fixture
def pg_url(store):
    return os.environ["P0_POSTGRES_URL"]

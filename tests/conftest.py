import pytest
from tests.fixtures import seed
@pytest.fixture
def snapshot(tmp_path):
    db=seed(tmp_path/'private.sqlite',tmp_path/'aggregate.sqlite')
    yield db,tmp_path/'aggregate.sqlite'
    db.close()

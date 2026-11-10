import json
import pytest
import telemetry


def test_sync_counts_and_failure(capsys):
    with telemetry.sync_run():
        telemetry.count('pages',3)
        telemetry.count('retries')
        telemetry.count('items_count',2)
        with telemetry.phase('fetch'):
            telemetry.failure(RuntimeError('SECRET'))
    line=capsys.readouterr().err
    assert 'SECRET' not in line
    record=json.loads(line.split(' ',1)[1])
    assert record['event']=='mcp_sync'
    assert (record['pages'],record['retries'],record['items_count'])==(3,1,2)
    assert record['outcome']=='error' and record['error_class']=='exception'
    assert record['total_ms']>=record['phase_ms']['fetch']>=0


def test_sync_exception_emits_once(capsys):
    with pytest.raises(ValueError):
        with telemetry.sync_run():
            raise ValueError('SECRET')
    lines=capsys.readouterr().err.splitlines()
    assert len(lines)==1 and 'SECRET' not in lines[0]

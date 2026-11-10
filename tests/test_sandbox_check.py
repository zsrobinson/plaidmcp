import pytest
from sandbox_check import CheckError, check_private_strings, check_fields, check_peer_shape


def test_privacy_audit_checks_bytes_and_short_tokens():
    check_private_strings(b'{"type":"mutual fund"}',{'FUN'})
    with pytest.raises(CheckError): check_private_strings(b'{"name":"FUN"}',{'FUN'})
    with pytest.raises(CheckError): check_private_strings(b'\x00PRIVATE MERCHANT NAME\x00',{'Private Merchant Name'})
    with pytest.raises(CheckError): check_fields({'data':[{'payment_note':'anything'}]})


def test_peer_probe_uses_response_shape_without_persisting_notes(tmp_path):
    class Client:
        transaction_template={'transaction_id':'private-id','name':'Private Merchant Name',
            'merchant_name':'Private Merchant Name','date':'2026-11-01',
            'personal_finance_category':{'primary':'GENERAL_MERCHANDISE','detailed':'GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE'}}
        private_strings={'Private Merchant Name'}
    assert check_peer_shape(tmp_path,Client())
    assert check_peer_shape(tmp_path,Client())  # --resume must be idempotent
    for path in tmp_path.iterdir():
        for value in (b'Private Merchant Name',b'PRIVACY_NOTE_SENTINEL_FOR_PEER_PROBE'):
            assert value not in path.read_bytes()

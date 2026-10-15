from agentposthub.flow import _check_output_contract


def test_valid_response_passes():
    resp = {"id": "tg_msg_1", "ok": True, "req_id": "t1#1"}
    assert _check_output_contract(resp, "t1#1") is None


def test_missing_field():
    resp = {"idd": "fb_post_9x8y", "ok": "true"}  # renamed field, wrong type
    violation = _check_output_contract(resp, "t1#1")
    assert violation == "missing field: id"


def test_wrong_ok_type():
    resp = {"id": "x", "ok": "true", "req_id": "t1#1"}
    assert _check_output_contract(resp, "t1#1") == "ok must be bool"


def test_empty_id():
    resp = {"id": "", "ok": True, "req_id": "t1#1"}
    assert _check_output_contract(resp, "t1#1") == "id must be a non-empty str"


def test_req_id_mismatch():
    resp = {"id": "x", "ok": True, "req_id": "other#9"}
    assert _check_output_contract(resp, "t1#1").startswith("req_id mismatch")


def test_non_dict():
    assert _check_output_contract(["not", "a", "dict"], "t1#1").startswith("not a dict")

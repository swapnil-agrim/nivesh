from nivesh_core.errors import InvestRightError, NiveshError, SessionExpired


def test_session_expired_is_a_nivesh_error() -> None:
    assert issubclass(SessionExpired, NiveshError)
    assert str(SessionExpired("gone")) == "gone"


def test_investright_error_carries_message_and_code() -> None:
    e = InvestRightError("denied", 60014)
    assert isinstance(e, NiveshError) and e.code == 60014 and "denied" in str(e)
    assert InvestRightError("x").code is None

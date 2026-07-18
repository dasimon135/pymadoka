from pymadoka import Controller


def test_candidates_callback_reaches_connection():
    marker = lambda: []  # noqa: E731
    ctrl = Controller("00:11:22:33:44:55", candidates_callback=marker)
    assert ctrl.connection.candidates_callback is marker
    assert ctrl.connection.connected_source is None


def test_candidates_callback_defaults_to_none():
    ctrl = Controller("00:11:22:33:44:55")
    assert ctrl.connection.candidates_callback is None

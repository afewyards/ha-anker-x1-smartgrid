from custom_components.anker_x1_smartgrid import const


def test_bin_edges_are_six_bins():
    assert const.EFFICIENCY_DC_BIN_EDGES_W == [400.0, 800.0, 1500.0, 2500.0, 4000.0]


def test_confidence_and_envelope_constants():
    assert const.EFFICIENCY_MIN_RUNS == 10
    assert const.EFFICIENCY_MIN_DC_KWH == 2.0
    assert const.EFFICIENCY_DSOC_GATE_PCT == 3.0
    assert const.EFFICIENCY_ENVELOPE == (0.50, 1.02)
    assert const.EFFICIENCY_WINDOW_DAYS == 30

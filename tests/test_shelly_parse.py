import pytest

from app.monitor import energy_delta
from app.shelly import ShellyError, parse_status


def test_parses_switch_component():
    st = {
        "switch:0": {
            "id": 0, "output": True, "apower": 61.5, "voltage": 229.8, "current": 0.268,
            "freq": 50.0, "aenergy": {"total": 1234.567}, "temperature": {"tC": 35.2},
        },
        "wifi": {"rssi": -61},
    }
    s = parse_status(st)
    assert s.output is True
    assert s.power_w == 61.5 and s.voltage == 229.8 and s.current == 0.268
    assert s.energy_wh == 1234.567 and s.temp_c == 35.2 and s.rssi == -61


def test_missing_optional_fields_are_none():
    s = parse_status({"switch:0": {"output": False}})
    assert s.output is False
    assert s.power_w is None and s.energy_wh is None and s.temp_c is None and s.rssi is None


def test_falls_back_to_pm1_component():
    st = {"switch:0": {"output": True}, "pm1:0": {"apower": 12.0, "aenergy": {"total": 5.0}}}
    s = parse_status(st)
    assert s.power_w == 12.0 and s.energy_wh == 5.0


def test_garbage_values_do_not_crash():
    s = parse_status({"switch:0": {"output": True, "apower": "n/a", "aenergy": None, "temperature": None}})
    assert s.power_w is None and s.energy_wh is None and s.temp_c is None


def test_no_switch_component_is_an_error():
    with pytest.raises(ShellyError):
        parse_status({"wifi": {}})


@pytest.mark.parametrize(
    "prev,cur,expected",
    [(None, 10.0, 0.0), (10.0, None, 0.0), (10.0, 12.5, 2.5), (10.0, 10.0, 0.0),
     (500.0, 3.0, 3.0)],  # counter reset: use the new value
)
def test_energy_delta(prev, cur, expected):
    assert energy_delta(prev, cur) == expected

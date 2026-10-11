"""Pure unit tests for R1-R5 / R8 (no database needed): pytest tests/test_rules.py"""
from datetime import date, datetime, timezone
from fractions import Fraction
from app.main import (attendance_date, compute_late_minutes, compute_overtime, work_hundredths,
                      round_half_up, derive, working_days, ms_to_dt, dt_to_ms)

IST_OFFSET_MIN = 330


def ist(y, mo, d, h, mi, s=0, us=0):
    """An IST wall-clock time as a UTC datetime."""
    from datetime import timedelta
    return datetime(y, mo, d, h, mi, s, us, tzinfo=timezone.utc) - timedelta(minutes=IST_OFFSET_MIN)


def late(h, m, s):  # shift 09:30 on 2026-07-06
    return compute_late_minutes(ist(2026, 7, 6, h, m, s), date(2026, 7, 6), "09:30")


def test_late_examples_from_the_contract():
    assert late(9, 40, 0) == 0      # exactly 10:00 -> not late
    assert late(9, 40, 1) == 10     # 10:01 -> late, counted from shift_start
    assert late(10, 15, 59) == 45
    assert late(9, 0, 0) == 0       # early


def test_late_truncates_subseconds():
    assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 0, 900_000), date(2026, 7, 6), "09:30") == 0


def test_overtime_threshold_and_floor():
    out = lambda h, m, s=0: compute_overtime(ist(2026, 7, 6, h, m, s), date(2026, 7, 6), "09:30", "18:30")
    assert out(18, 59, 59) == 0     # 29 min
    assert out(19, 0) == 30
    assert out(19, 40, 59) == 70


def test_overnight_dates_and_overtime():
    # shift 22:00-06:00; punch-in 01:00 IST on the 7th belongs to the shift of the 6th
    assert attendance_date(ist(2026, 7, 7, 1, 0), "22:00", "06:00") == date(2026, 7, 6)
    assert attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == date(2026, 7, 6)
    assert attendance_date(ist(2026, 7, 7, 6, 0), "22:00", "06:00") == date(2026, 7, 7)
    # sample doc 7: out 06:40 next day -> 40 min overtime
    assert compute_overtime(ist(2026, 7, 7, 6, 40), date(2026, 7, 6), "22:00", "06:00") == 40
    # day shift is unaffected
    assert attendance_date(ist(2026, 7, 6, 1, 0), "09:30", "18:30") == date(2026, 7, 6)


def test_work_hours_half_up():
    pin = ist(2026, 7, 6, 9, 0)
    assert work_hundredths(pin, ist(2026, 7, 6, 9, 0, 18)) == 1   # 0.005 h -> 0.01 (half up)
    assert work_hundredths(pin, ist(2026, 7, 6, 9, 0, 17)) == 0
    assert work_hundredths(ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35)) == 912   # sample doc 1 (9.12)


def test_half_day_uses_rounded_value():
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    d = date(2026, 7, 6)
    pin = ist(2026, 7, 6, 9, 30)
    # 4h29m36s = 4.4933 -> 4.49 (< 4.50) -> half day ; 4h29m42s = 4.495 -> 4.50 -> NOT half day
    assert derive(emp, d, pin, ist(2026, 7, 6, 13, 59, 36))["half_day"] is True
    assert derive(emp, d, pin, ist(2026, 7, 6, 13, 59, 42))["half_day"] is False


def test_round_half_up():
    assert round_half_up(Fraction(5, 2), 0) == 3
    assert round_half_up(Fraction(1005, 1000), 2) == 1.01     # float round() gives 1.0
    assert round_half_up(Fraction(2, 3) * 100, 2) == 66.67


def test_working_days_for_a_mid_month_joiner():
    first, last = date(2026, 7, 1), date(2026, 7, 31)
    assert working_days("2026-01-05", first, last) == 23
    assert working_days("2026-07-13", first, last) == 15
    assert working_days("2026-08-01", first, last) == 0


def test_epoch_roundtrip_truncates_to_seconds():
    assert dt_to_ms(ms_to_dt(1783312500999)) == 1783312500000

"""Needs a REAL MongoDB 6+ (docker run -p 27017:27017 mongo:7). It reloads the nine sample documents into
MONGO_DB (default attendance_db_test) and checks the analytics against numbers worked out by hand from them.
Run:  MONGO_DB=attendance_db_test pytest -q tests/test_integration.py"""
import os
os.environ.setdefault("MONGO_DB", "attendance_db_test")
import pathlib
from concurrent.futures import ThreadPoolExecutor
import pytest
from bson import json_util
from fastapi.testclient import TestClient
import app.main as m

DATA = pathlib.Path(__file__).parent.parent / "sample_data"
OPTS = json_util.JSONOptions(tz_aware=True)


@pytest.fixture(scope="module")
def c():
    try:
        m.client.admin.command("ping")
    except Exception:
        pytest.skip("MongoDB not reachable")
    for name in ("employees", "attendance_logs"):
        m.db[name].drop()
    m.ensure_indexes()
    for name in ("employees", "attendance_logs"):
        m.db[name].insert_many(json_util.loads((DATA / f"{name}.json").read_text(), json_options=OPTS))
    return TestClient(m.app)


def test_employee_monthly(c):
    j = c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert (j["working_days"], j["present_days"], j["attendance_pct"]) == (23, 2, 8.7)
    j = c.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}).json()   # joined 07-13
    assert (j["working_days"], j["present_days"], j["attendance_pct"]) == (15, 1, 6.67)
    j = c.get("/analytics/employees/EMP0004/monthly", params={"month": "2026-07"}).json()   # WFH half day + ABSENT
    assert j["present_days"] == 0.5 and j["leave_days"] == 0
    j = c.get("/analytics/employees/EMP0003/monthly", params={"month": "2026-07"}).json()
    assert (j["leave_days"], j["late_count"], j["total_late_minutes"], j["total_overtime_minutes"]) == (1, 1, 35, 40)
    assert c.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-06"}).json()["attendance_pct"] is None
    assert c.get("/analytics/employees/NOPE/monthly", params={"month": "2026-07"}).status_code == 404
    assert c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code == 422


def test_department_summary(c):
    items = {i["department"]: i for i in c.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]}
    assert list(items) == ["Engineering", "Sales", "Support"]
    e, s, p = items["Engineering"], items["Sales"], items["Support"]
    assert (e["headcount"], e["present_days"], e["avg_work_hours"]) == (2, 3, 9.04)
    assert (s["headcount"], s["present_days"], s["avg_work_hours"]) == (2, 1.5, 6.29)   # EMP0004 has records, still counted
    assert (s["late_count"], s["total_late_minutes"], s["leave_count"]) == (1, 35, 1)
    assert (p["headcount"], p["present_days"], p["avg_work_hours"]) == (2, 2, 8.75)     # open record: present, no hours
    # EMP0002 joins 2026-07-13: not counted in June
    june = {i["department"]: i for i in c.get("/analytics/departments/summary", params={"month": "2026-06"}).json()["items"]}
    assert june["Engineering"]["headcount"] == 1 and june["Engineering"]["avg_work_hours"] is None


def test_leaderboard(c):
    j = c.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()["items"]
    assert [(r["rank"], r["emp_code"], r["total_late_minutes"]) for r in j] == [(1, "EMP0003", 35)]
    assert c.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Engineering"}).json()["items"] == []


def test_ties_share_rank_and_limit_applies_after_ranking(c):
    from datetime import datetime, timezone
    extra = []
    for code, late in (("EMP0001", 20), ("EMP0004", 20), ("EMP0006", 5)):
        extra.append({"emp_code": code, "date": "2026-07-20", "status": "PRESENT", "punch_in": datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc),
                      "punch_out": None, "work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False, "history": []})
    m.db.attendance_logs.insert_many(extra)
    try:
        rows = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
        # totals: EMP0003=35, EMP0001=20, EMP0004=20, EMP0006=5 -> ranks 1,2,2,4 ; limit=2 keeps BOTH tied rank-2 rows
        assert [(r["rank"], r["emp_code"]) for r in rows] == [(1, "EMP0003"), (2, "EMP0001"), (2, "EMP0004")]
    finally:
        m.db.attendance_logs.delete_many({"date": "2026-07-20"})


def test_trend(c):
    r = c.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-05", "to": "2026-07-08"}).json()["items"]
    got = [(i["date"], i["is_working_day"], i["headcount"], i["present_count"], i["attendance_rate"], i["moving_avg_7d"]) for i in r]
    assert got == [("2026-07-05", False, 1, 0, None, None), ("2026-07-06", True, 1, 1, 1.0, 1.0),
                   ("2026-07-07", True, 1, 1, 1.0, 1.0), ("2026-07-08", True, 1, 0, 0.0, 0.6667)]
    r = c.get("/analytics/departments/Sales/trend", params={"from": "2026-07-06", "to": "2026-07-08"}).json()["items"]
    assert [(i["present_count"], i["late_count"], i["attendance_rate"], i["moving_avg_7d"]) for i in r] == \
           [(1, 1, 0.5, 0.5), (0.5, 0, 0.25, 0.375), (0, 0, 0.0, 0.25)]
    # a range with no records at all still yields one row per day
    r = c.get("/analytics/departments/Sales/trend", params={"from": "2025-01-01", "to": "2025-01-10"}).json()["items"]
    assert len(r) == 10
    assert c.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert c.get("/analytics/departments/Sales/trend", params={"from": "2026-07-05", "to": "2026-07-01"}).status_code == 422
    assert c.get("/analytics/departments/Sales/trend", params={"from": "2026-01-01", "to": "2026-04-03"}).status_code == 422  # 93 days


@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01", "status": "PRESENT"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Sales"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("department_trend", {"department": "Sales", "from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_uses_indexes(c, endpoint, params):
    body = c.get(f"/admin/explain/{endpoint}", params=params)
    assert body.status_code == 200, body.text
    text = body.text
    assert "IXSCAN" in text and "COLLSCAN" not in text


def test_punch_in_race_gives_exactly_one_201(c):
    payload = {"emp_code": "EMP0006", "punched_at": 1784000000000}   # a day with no record yet
    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(lambda _: c.post("/attendance/punch-in", json=payload).status_code, range(16)))
    assert sorted(codes) == [201] + [409] * 15


def test_concurrent_regularizations_do_not_lose_history(c):
    url = "/attendance/EMP0001/2026-07-06"
    times = [1783310000000 + 60000 * i for i in range(1, 7)]   # 6 different punch_in values on the same IST day
    def go(t):
        return c.patch(url, json={"punch_in": t, "reason": "race test", "regularized_by": "t"}).status_code
    with ThreadPoolExecutor(6) as ex:
        codes = list(ex.map(go, times))
    hist = c.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-06", "date_to": "2026-07-06"}).json()["items"][0]["history"]
    assert len(hist) == codes.count(200) >= 1

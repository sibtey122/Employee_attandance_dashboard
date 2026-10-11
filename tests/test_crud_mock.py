"""Endpoint tests for CRUD + rules using an in-memory fake MongoDB (mongomock).
The analytics pipelines need a real MongoDB 6+: see tests/test_integration.py."""
import mongomock
import pytest
from fastapi.testclient import TestClient
from datetime import datetime, timezone, timedelta
import app.main as m

IST = timedelta(hours=5, minutes=30)


def ms(y, mo, d, h, mi, s=0):  # IST wall clock -> epoch ms
    return int((datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc) - IST).timestamp() * 1000)


@pytest.fixture()
def c(monkeypatch):
    fake = mongomock.MongoClient(tz_aware=True)["t"]
    monkeypatch.setattr(m, "db", fake)
    m.ensure_indexes()
    cl = TestClient(m.app)
    base = dict(name="A", email="a@x.io", department="Eng", joined_on="2026-01-05")
    assert cl.post("/employees", json={"emp_code": "EMP0001", **base}).status_code == 201
    assert cl.post("/employees", json={"emp_code": "EMP0005", **base, "shift_start": "22:00", "shift_end": "06:00"}).status_code == 201
    return cl


def test_employee_validation_and_duplicates(c):
    ok = dict(name="A", email="a@x.io", department="Eng", joined_on="2026-01-05")
    assert c.post("/employees", json={"emp_code": "EMP0001", **ok}).status_code == 409
    assert c.post("/employees", json={"emp_code": "bad", **ok}).status_code == 422
    assert c.post("/employees", json={"emp_code": "EMP0002", **ok, "shift_start": "09:00", "shift_end": "09:00"}).status_code == 422
    assert c.post("/employees", json={"emp_code": "EMP0002", **{**ok, "joined_on": "2026-02-30"}}).status_code == 422
    r = c.get("/employees", params={"page": 1, "page_size": 1}).json()
    assert r["total"] == 2 and r["items"][0]["emp_code"] == "EMP0001" and "_id" not in r["items"][0]
    assert isinstance(r["items"][0]["created_at"], int)
    assert c.get("/employees", params={"page": 0}).status_code == 422
    assert c.get("/employees", params={"department": "Eng", "page_size": 101}).status_code == 422


def test_punch_in_out_flow_and_rules(c):
    # 10:15:59 IST on a 09:30 shift -> 45 late
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 15, 59)})
    assert r.status_code == 201
    body = r.json()
    assert body["date"] == "2026-07-06" and body["late_minutes"] == 45 and body["history"] == []
    assert "id" not in body and "_id" not in body
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 11, 0)}).status_code == 409
    out = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 10)})
    assert out.status_code == 200
    j = out.json()
    assert j["work_hours"] == 8.9 and j["overtime_minutes"] == 40 and j["half_day"] is False
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 20)}).status_code == 409


def test_bad_input(c):
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    for bad in (1783312500, 1783312500000.5, "1783312500000", True):
        assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422, bad
    assert c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404


def test_punch_out_validation_and_overnight(c):
    c.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)})
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 0)}).status_code == 422
    assert c.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 22, 0)}).status_code == 422  # >24h
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)})
    assert r.status_code == 200 and r.json()["date"] == "2026-07-06"
    assert r.json()["overtime_minutes"] == 40 and r.json()["work_hours"] == 8.75


def test_overnight_punch_in_after_midnight_belongs_to_previous_day(c):
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 1, 0)})
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 180


def test_list_pagination_and_filters(c):
    for day in (6, 7, 8):
        c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, day, 9, 30)})
    r = c.get("/attendance", params={"page": 2, "page_size": 2}).json()
    assert r["total"] == 3 and [i["date"] for i in r["items"]] == ["2026-07-06"]
    r = c.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07"}).json()
    assert r["total"] == 1
    assert c.get("/attendance", params={"date_from": "2026-07-09", "date_to": "2026-07-01"}).status_code == 422
    assert c.get("/attendance", params={"status": "NOPE"}).status_code == 422


def test_regularize_recomputes_and_appends_history(c):
    c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 18, 30)})
    url = "/attendance/EMP0001/2026-07-07"
    body = {"punch_in": ms(2026, 7, 7, 9, 28), "reason": "biometric glitch", "regularized_by": "hr.admin", "late_minutes": 999}
    r = c.patch(url, json=body)
    assert r.status_code == 200
    j = r.json()
    assert j["late_minutes"] == 0 and j["work_hours"] == 9.03 and len(j["history"]) == 1
    ch = j["history"][0]["changes"]
    assert set(ch) == {"punch_in", "late_minutes", "work_hours"}
    assert ch["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)}
    assert ch["late_minutes"] == {"from": 35, "to": 0}
    assert c.patch(url, json=body).status_code == 422                      # changes nothing
    r2 = c.patch(url, json={"punch_out": ms(2026, 7, 7, 17, 0), "reason": "left early", "regularized_by": "hr"})
    assert len(r2.json()["history"]) == 2                                   # appended, not overwritten
    # ABSENT clears the times; sending times with it is a 422
    assert c.patch(url, json={"status": "ABSENT", "punch_in": ms(2026, 7, 7, 9, 0), "reason": "xxxxx", "regularized_by": "hr"}).status_code == 422
    r3 = c.patch(url, json={"status": "ABSENT", "reason": "never came", "regularized_by": "hr"}).json()
    assert r3["punch_in"] is None and r3["work_hours"] is None and r3["half_day"] is False
    # presence again requires a punch_in
    assert c.patch(url, json={"status": "WFH", "reason": "oops wrong", "regularized_by": "hr"}).status_code == 422
    # punch_in on another attendance day / punch_out before punch_in / unknown things
    assert c.patch(url, json={"status": "WFH", "punch_in": ms(2026, 7, 8, 9, 0), "reason": "wrong day", "regularized_by": "hr"}).status_code == 422
    assert c.patch(url, json={"status": "WFH", "punch_in": ms(2026, 7, 7, 9, 0), "punch_out": ms(2026, 7, 7, 8, 0), "reason": "backwards", "regularized_by": "hr"}).status_code == 422
    assert c.patch("/attendance/EMP0001/2026-07-30", json={"status": "LEAVE", "reason": "nothing here", "regularized_by": "hr"}).status_code == 404
    assert c.patch("/attendance/EMP9999/2026-07-07", json={"status": "LEAVE", "reason": "nothing here", "regularized_by": "hr"}).status_code == 404
    assert c.patch("/attendance/EMP0001/2026-13-07", json={"status": "LEAVE", "reason": "nothing here", "regularized_by": "hr"}).status_code == 422


def test_health_reports_ok(c, monkeypatch):
    monkeypatch.setattr(m, "client", mongomock.MongoClient())
    assert c.get("/health").json() == {"status": "ok"}

"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded, but real environment variables win)

Layout of this single file:
  1. configuration & indexes      4. request models / response shaping
  2. time + business rules R1-R5  5. endpoints (employees, attendance)
  3. small parsing helpers        6. analytics pipelines + endpoints, explain
"""
import json
import os
import calendar
import math
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timedelta, timezone
from fractions import Fraction
from typing import Annotated, Any, Literal, Optional

from bson import SON, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment

# =========================================================================== #
# 1. Configuration & indexes
# =========================================================================== #
# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes, never naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=10_000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent. Each index is explained in DECISIONS.md."""
    global _indexes_ready
    # employees: identity guarantee + department/joined_on lookups for the analytics
    db.employees.create_index("emp_code", unique=True, name="uq_emp_code")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="dept_joined")
    db.employees.create_index("joined_on", name="joined")
    # attendance_logs: one record per (employee, day) - also what makes punch-in race-safe,
    # and serves per-employee queries (monthly, punch-out lookup, list by emp_code)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uq_emp_date")
    # list sort order (date desc, emp_code asc) + every date-range scan (summary, leaderboard)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_desc_emp")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        pass  # /health reports 503 and retries index creation once MongoDB answers
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])



# =========================================================================== #
# 2. Time and business rules (R1 - R5)
# =========================================================================== #
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
GRACE_SECONDS = 10 * 60  # R2
OVERTIME_MIN_MINUTES = 30  # R3
HALF_DAY_HUNDREDTHS = 450  # R5
MAX_SHIFT = timedelta(hours=24)

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
# strict=True: no floats, strings or bools; the range rejects values that look like seconds.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)  # R1: whole seconds


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def trunc(dt: datetime) -> datetime:
    return as_utc(dt).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz=UTC)  # floor to whole seconds (R1)


def dt_to_ms(dt: datetime) -> int:
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def ms_or_none(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else dt_to_ms(dt)


def hm_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hm_minutes(shift_end) <= hm_minutes(shift_start)


def at_ist(d: Date, hhmm: str) -> datetime:
    return datetime(d.year, d.month, d.day, hm_minutes(hhmm) // 60, hm_minutes(hhmm) % 60, tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: IST calendar date of the punch-in; overnight shifts started before shift_end belong to the day before."""
    ist = trunc(punch_in).astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and ist.hour * 60 + ist.minute < hm_minutes(shift_end):
        d -= timedelta(days=1)
    return d


def compute_late_minutes(punch_in: datetime, d: Date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then whole minutes since shift_start."""
    delta = (trunc(punch_in) - at_ist(d, shift_start)).total_seconds()
    return int(delta // 60) if delta > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, d: Date, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift_end (next day for overnight shifts), only if >= 30."""
    end_day = d + timedelta(days=1) if is_overnight(shift_start, shift_end) else d
    minutes = int((trunc(punch_out) - at_ist(end_day, shift_end)).total_seconds() // 60)
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def work_hundredths(punch_in: datetime, punch_out: datetime) -> int:
    """R4 in integer arithmetic: round-half-up(seconds / 36) = floor((2*seconds + 36) / 72). No float rounding."""
    seconds = int((trunc(punch_out) - trunc(punch_in)).total_seconds())
    return (2 * seconds + 36) // 72


def derive(emp: dict, d: Date, punch_in: datetime, punch_out: Optional[datetime]) -> dict:
    """All four derived fields for a presence record."""
    out = {
        "late_minutes": compute_late_minutes(punch_in, d, emp["shift_start"]),
        "work_hours": None,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        h = work_hundredths(punch_in, punch_out)
        out["work_hours"] = h / 100
        out["overtime_minutes"] = compute_overtime(punch_out, d, emp["shift_start"], emp["shift_end"])
        out["half_day"] = h < HALF_DAY_HUNDREDTHS
    return out


def round_half_up(x: Fraction, places: int) -> float:
    """R8: exact half-up rounding of a rational number."""
    scale = 10**places
    return math.floor(x * scale + Fraction(1, 2)) / scale


# =========================================================================== #
# 3. Parsing helpers (everything invalid is a 422 in FastAPI's standard shape)
# =========================================================================== #
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def bad(loc: tuple, msg: str, typ: str = "value_error"):
    raise RequestValidationError([{"type": typ, "loc": loc, "msg": msg, "input": None}])


def need(value: Any, name: str):
    if value is None:
        bad(("query", name), "Field required", "missing")
    return value


def parse_date(value: str, loc: tuple) -> Date:
    if not DATE_RE.match(value):
        bad(loc, "Expected a date as YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        bad(loc, "Not a valid calendar date")


def month_bounds(month: str) -> tuple[str, str, Date, Date]:
    try:
        year, mon = int(month[:4]), int(month[5:7])
        first = Date(year, mon, 1)
        last = Date(year, mon, calendar.monthrange(year, mon)[1])
    except (ValueError, IndexError):
        bad(("query", "month"), "Expected a month as YYYY-MM")
    return first.isoformat(), last.isoformat(), first, last


def working_days(joined_on: str, first: Date, last: Date) -> int:
    """R7: Mon-Fri days of the month, counted from joined_on for mid-month joiners."""
    start = max(first, Date.fromisoformat(joined_on))
    if start > last:
        return 0
    return sum(1 for i in range((last - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


def find_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    return emp


# =========================================================================== #
# 4. Request models and response shaping
# =========================================================================== #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field("18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("Expected a date as YYYY-MM-DD")
        Date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):  # unknown keys (e.g. derived fields) are ignored by default
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"], "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"], "created_at": dt_to_ms(doc["created_at"]),
    }


def history_out(entries: Optional[list]) -> list:
    out = []
    for h in entries or []:
        changes = {}
        for key, val in (h.get("changes") or {}).items():
            if key in ("punch_in", "punch_out"):
                val = {"from": ms_or_none(val.get("from")), "to": ms_or_none(val.get("to"))}
            changes[key] = val
        out.append({"at": dt_to_ms(h["at"]), "by": h.get("by"), "reason": h.get("reason"), "changes": changes})
    return out


def record_out(doc: dict) -> dict:
    """Mongo document -> API shape: epoch ms, no _id, legacy gaps filled (missing = 0 / false / [])."""
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": ms_or_none(doc.get("punch_in")), "punch_out": ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history_out(doc.get("history")),
    }


# =========================================================================== #
# 5. Endpoints: system, employees, attendance
# =========================================================================== #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now = datetime.now(UTC)
    doc = body.model_dump()
    doc["created_at"] = now.replace(microsecond=(now.microsecond // 1000) * 1000)  # BSON dates are ms
    try:
        db.employees.insert_one(doc)  # the unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    cursor = db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [employee_out(d) for d in cursor],
        "total": db.employees.count_documents(q),
        "page": page, "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = find_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else utcnow()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": d.isoformat(), "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp["shift_start"]),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one of N racers wins
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = find_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else utcnow()
    # Most recent record whose punch_in <= ts (works across midnight: the overnight record is dated the day before).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("date", -1)]
    )
    if rec is None:
        if db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$gt": ts}}, {"_id": 1}):
            bad(("body", "punched_at"), "punched_at is before the employee's punch_in")
        raise HTTPException(404, "No punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    pin = trunc(rec["punch_in"])
    if ts <= pin:
        bad(("body", "punched_at"), "punched_at must be after punch_in")
    if ts - pin > MAX_SHIFT:
        bad(("body", "punched_at"), "punched_at is more than 24 hours after punch_in")
    derived = derive(emp, Date.fromisoformat(rec["date"]), pin, ts)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  # atomic: only the first closer matches
        {"$set": {"punch_out": ts, "work_hours": derived["work_hours"],
                  "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Already punched out")
    return record_out(updated)


def attendance_list_query(emp_code, date_from, date_to, status, page, page_size):
    """Shared by GET /attendance and the explain endpoint so both run the exact same query."""
    f = parse_date(date_from, ("query", "date_from")) if date_from is not None else None
    t = parse_date(date_to, ("query", "date_to")) if date_to is not None else None
    if f and t and f > t:
        bad(("query", "date_from"), "date_from must not be after date_to")
    filt: dict = {}
    if emp_code is not None:
        filt["emp_code"] = emp_code
    if f or t:
        filt["date"] = {**({"$gte": f.isoformat()} if f else {}), **({"$lte": t.isoformat()} if t else {})}
    if status is not None:
        filt["status"] = status
    return filt, [("date", -1), ("emp_code", 1)], (page - 1) * page_size, page_size


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    filt, sort, skip, limit = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
    docs = db.attendance_logs.find(filt).sort(sort).skip(skip).limit(limit)  # the database sorts and pages
    total = db.attendance_logs.count_documents(filt) if filt else db.attendance_logs.estimated_document_count()
    return {"items": [record_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


def _same(a, b) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        return as_utc(a) == as_utc(b)
    return a == b


def plan_regularization(emp: dict, rec: dict, body: RegularizeIn, d: Date) -> tuple[dict, dict]:
    """Returns (new field values, changes). Raises 422 for invalid corrections."""
    status = body.status or rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            bad(("body", "punch_in"), f"{status} clears the punch times; do not send punch_in/punch_out")
        new_in = new_out = None
        derived = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            bad(("body", "punch_in"), f"{status} requires a punch_in")
        new_in = trunc(new_in)
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != d:
            bad(("body", "punch_in"), "punch_in must stay on the record's attendance date")
        if new_out is not None:
            new_out = trunc(new_out)
            if not (new_in < new_out <= new_in + MAX_SHIFT):
                bad(("body", "punch_out"), "punch_out must be after punch_in and within 24 hours")
        derived = derive(emp, d, new_in, new_out)

    new = {"status": status, "punch_in": new_in, "punch_out": new_out, **derived}
    old = {
        "status": rec.get("status"), "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in new if not _same(old[k], new[k])}
    if not changes:
        bad(("body",), "The request changes nothing")
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    d = parse_date(date, ("path", "date"))
    emp = find_employee(emp_code)
    for _ in range(5):  # optimistic concurrency: re-read and re-plan if someone else edited meanwhile
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": d.isoformat()})
        if rec is None:
            raise HTTPException(404, "No attendance record for that date")
        new, changes = plan_regularization(emp, rec, body, d)
        entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        hist = rec.get("history")
        guard = {  # the write only applies if the record is still exactly as we read it
            "_id": rec["_id"], "status": rec.get("status"),
            "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
            "history": {"$size": len(hist)} if isinstance(hist, list) else {"$exists": False},
        }
        updated = db.attendance_logs.find_one_and_update(
            guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "The record is being modified concurrently; retry")


# =========================================================================== #
# 6. Analytics: aggregation pipelines
# =========================================================================== #
# Reusable expression fragments over a stored attendance_logs document.
_DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None, "onNull": None}}}
IS_WEEKDAY = {"$and": [{"$gte": [_DOW, 2]}, {"$lte": [_DOW, 6]}]}  # $dayOfWeek: 1=Sunday .. 7=Saturday
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
_WEIGHT2 = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}  # half day = 0.5 -> counted in halves
PRESENT2_WEEKDAY = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, _WEIGHT2, 0]}
PRESENT2_ANYDAY = {"$cond": [IS_PRESENT, _WEIGHT2, 0]}
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MIN, 0]}, 1, 0]}
HAS_WH = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
WH_HUNDREDTHS = {"$cond": [HAS_WH, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}  # exact integers
WH_COUNT = {"$cond": [HAS_WH, 1, 0]}


def _is(status: str) -> dict:
    return {"$cond": [{"$eq": ["$status", status]}, 1, 0]}


def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},  # uq_emp_date
        {"$group": {
            "_id": None,
            "present2": {"$sum": PRESENT2_WEEKDAY},
            "leave_days": {"$sum": _is("LEAVE")},
            "late_count": {"$sum": IS_LATE},
            "total_late_minutes": {"$sum": LATE_MIN},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Starts from employees so people with zero logs count (R9); logs are joined per employee by index."""
    emp_match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        emp_match["department"] = department
    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$group": {
                    "_id": None,
                    "present2": {"$sum": PRESENT2_WEEKDAY},
                    "wh100": {"$sum": WH_HUNDREDTHS}, "wh_n": {"$sum": WH_COUNT},
                    "late_count": {"$sum": IS_LATE}, "late_min": {"$sum": LATE_MIN},
                    "leave": {"$sum": _is("LEAVE")}, "on_duty": {"$sum": _is("ON_DUTY")},
                }},
            ],
            "as": "s",
        }},
        {"$set": {"s": {"$arrayElemAt": ["$s", 0]}}},  # missing when the employee has no logs -> $ifNull below
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present2": {"$sum": {"$ifNull": ["$s.present2", 0]}},
            "wh100": {"$sum": {"$ifNull": ["$s.wh100", 0]}}, "wh_n": {"$sum": {"$ifNull": ["$s.wh_n", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "late_min": {"$sum": {"$ifNull": ["$s.late_min", 0]}},
            "leave": {"$sum": {"$ifNull": ["$s.leave", 0]}},
            "on_duty": {"$sum": {"$ifNull": ["$s.on_duty", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    stages: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},  # date_desc_emp
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        stages.append({"$match": {"e.department": department}})  # rank inside the department only
    stages += [
        # $rank = standard competition ranking (1, 2, 2, 4); ties on total share a rank.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking: ties at the cutoff are all kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return stages


def trend_pipeline(codes: list, joins: list, frm: Date, to: Date) -> list:
    """One row per calendar day. Gap filling ($densify), headcount, rate and 7-row moving average all happen in
    MongoDB. Rates and averages are computed in integer ten-thousandths so half-up rounding is exact."""
    lo = datetime(frm.year, frm.month, frm.day, tzinfo=UTC)
    hi = datetime(to.year, to.month, to.day, tzinfo=UTC)
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": frm.isoformat(), "$lte": to.isoformat()}}},
        {"$group": {"_id": "$date", "present2": {"$sum": PRESENT2_ANYDAY}, "late": {"$sum": IS_LATE}}},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi + timedelta(days=1)]}}},
        {"$match": {"d": {"$gte": lo, "$lte": hi}}},  # robust whether the upper bound is inclusive or not
        {"$set": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
            "present2": {"$ifNull": ["$present2", 0]},
            "late": {"$ifNull": ["$late", 0]},
            "working": {"$and": [{"$gte": [{"$dayOfWeek": "$d"}, 2]}, {"$lte": [{"$dayOfWeek": "$d"}, 6]}]},
        }},
        {"$set": {"headcount": {"$size": {"$filter": {"input": {"$literal": joins}, "cond": {"$lte": ["$$this", "$date"]}}}}}},
        {"$set": {"rate_n": {"$cond": [
            {"$and": ["$working", {"$gt": ["$headcount", 0]}]},
            # round-half-up(present2 / (2*headcount) * 10000) = floor((present2*10000 + headcount) / (2*headcount))
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present2", 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]}]}},
            None,
        ]}}},
        {"$set": {"has_rate": {"$cond": [{"$eq": ["$rate_n", None]}, 0, 1]}}},
        {"$setWindowFields": {
            "sortBy": {"d": 1},
            "output": {
                "win_sum": {"$sum": "$rate_n", "window": {"documents": [-6, 0]}},
                "win_n": {"$sum": "$has_rate", "window": {"documents": [-6, 0]}},
            },
        }},
        {"$set": {"mov_n": {"$cond": [
            {"$eq": ["$win_n", 0]}, None,
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$win_sum", 2]}, "$win_n"]}, {"$multiply": ["$win_n", 2]}]}},
        ]}}},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": "$working", "headcount": 1,
            "present_count": {"$divide": ["$present2", 2]}, "late_count": "$late",
            "attendance_rate": {"$cond": [{"$eq": ["$rate_n", None]}, None, {"$divide": ["$rate_n", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$eq": ["$mov_n", None]}, None, {"$divide": ["$mov_n", 10000]}]},
        }},
    ]


def department_roster(department: str) -> tuple[list, list]:
    """emp_codes and joined_on dates of a department (indexed by dept_joined). 404 when it has no employees."""
    rows = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not rows:
        raise HTTPException(404, "Unknown department")
    return [r["emp_code"] for r in rows], [r["joined_on"] for r in rows]


def trend_range(frm: str, to: str) -> tuple[Date, Date]:
    f, t = parse_date(frm, ("query", "from")), parse_date(to, ("query", "to"))
    if t < f:
        bad(("query", "to"), "'to' must not be before 'from'")
    if (t - f).days + 1 > 92:
        bad(("query", "to"), "The range must not exceed 92 days")
    return f, t


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    start, end, first, last = month_bounds(month)
    emp = find_employee(emp_code)
    row = next(iter(db.attendance_logs.aggregate(monthly_pipeline(emp_code, start, end))), None) or {}
    present2 = int(row.get("present2", 0))
    wd = working_days(emp["joined_on"], first, last)
    return {
        "emp_code": emp_code, "month": month, "working_days": wd,
        "present_days": present2 / 2,
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": None if wd == 0 else round_half_up(Fraction(present2, 2 * wd) * 100, 2),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    start, end, _, _ = month_bounds(month)
    rows = db.employees.aggregate(summary_pipeline(start, end, department), allowDiskUse=True)
    items = []
    for r in rows:
        wh_n = int(r["wh_n"])
        items.append({
            "department": r["_id"], "headcount": int(r["headcount"]),
            "present_days": int(r["present2"]) / 2,
            "avg_work_hours": None if wh_n == 0 else round_half_up(Fraction(int(round(r["wh100"])), 100 * wh_n), 2),
            "late_count": int(r["late_count"]), "total_late_minutes": int(r["late_min"]),
            "leave_count": int(r["leave"]), "on_duty_count": int(r["on_duty"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end, _, _ = month_bounds(month)
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(start, end, limit, department), allowDiskUse=True)
    return {"month": month, "items": [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in rows
    ]}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    f, t = trend_range(from_, to)
    codes, joins = department_roster(department)
    items = list(db.attendance_logs.aggregate(trend_pipeline(codes, joins, f, t), allowDiskUse=True))
    return {"department": department, "items": items}


# =========================================================================== #
# Explain: runs the SAME query/pipeline builders as the endpoints above
# =========================================================================== #
def _plain(doc: Any) -> Any:
    return json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))


def _explain_find(coll: str, filt: dict, sort: list, skip: int, limit: int) -> dict:
    cmd = SON([("find", coll), ("filter", filt), ("sort", SON(sort)), ("skip", skip), ("limit", limit)])
    return db.command(SON([("explain", cmd), ("verbosity", "executionStats")]))


def _explain_agg(coll: str, pipeline: list) -> dict:
    cmd = SON([("aggregate", coll), ("pipeline", pipeline), ("cursor", {}), ("allowDiskUse", True)])
    return db.command(SON([("explain", cmd), ("verbosity", "executionStats")]))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        filt, sort, skip, lim = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
        coll, result = "attendance_logs", _explain_find("attendance_logs", filt, sort, skip, lim)
    elif endpoint == "employee_monthly":
        need(emp_code, "emp_code"), need(month, "month")
        start, end, _, _ = month_bounds(month)
        coll, result = "attendance_logs", _explain_agg("attendance_logs", monthly_pipeline(emp_code, start, end))
    elif endpoint == "department_summary":
        need(month, "month")
        start, end, _, _ = month_bounds(month)
        coll, result = "employees", _explain_agg("employees", summary_pipeline(start, end, department))
    elif endpoint == "late_leaderboard":
        need(month, "month")
        start, end, _, _ = month_bounds(month)
        coll = "attendance_logs"
        result = _explain_agg(coll, leaderboard_pipeline(start, end, limit, department))
    else:  # department_trend
        need(department, "department"), need(from_, "from"), need(to, "to")
        f, t = trend_range(from_, to)
        codes, joins = department_roster(department)
        coll, result = "attendance_logs", _explain_agg("attendance_logs", trend_pipeline(codes, joins, f, t))
    return {"endpoint": endpoint, "collection": coll, "explain": _plain(result)}

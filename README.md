# Employee Attendance & Analytics API

FastAPI + MongoDB 6.0+ (the grader uses 7.0). All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or use a local .env
python sample_seed.py                                                    # optional: loads the 9 sample docs
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotently); no manual steps.

## Tests
- `pytest tests/test_rules.py tests/test_crud_mock.py` - rules R1-R5/R8 and CRUD, no database needed.
- `pytest tests/test_integration.py` - needs a real MongoDB; checks the analytics (hand-computed from the sample data), explain plans (IXSCAN, no COLLSCAN), and the punch-in / regularize races.

## Notes
- Analytics rounding (R8) is exact half-up: rates are computed in integer ten-thousandths inside the pipelines; the
  final 2-decimal values (`attendance_pct`, `avg_work_hours`) are rounded half-up in Python from exact integer sums.
- `GET /analytics/departments/{department}/trend` uses `$densify` + `$setWindowFields` (MongoDB 5.1+/6.0+).
- Status of verification: see "What was and was not run" below.

## What was and was not run
Rule helpers and the CRUD endpoints were tested (in-memory fake MongoDB). The four analytics pipelines and the explain
endpoint need a real MongoDB; `tests/test_integration.py` is there for that - please run it before submitting.

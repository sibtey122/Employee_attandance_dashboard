# REVIEW.md: review of the starter `app/main.py`

Format: **where** · **what is wrong** · **how you would notice** · **fix**. All fixes are in the submitted `app/main.py`.

| # | Where | What is wrong | How you would notice | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Grace check is `minutes > 10` on an already-floored value, so 09:40:01 (10 whole minutes) returns 0; the contract wants 10. | Punch in 09:40:01 on a 09:30 shift: expected 10, got 0. | Compare **seconds** (`> 600`), then floor to minutes since `shift_start`. |
| 2 | `compute_late_minutes` | Builds `shift_start` on the punch-in's own (UTC/naive) date, not IST, and ignores overnight shifts. | 22:00 shift, punch-in at 01:00 IST next day: start taken on the wrong day. A 09:30 IST punch-in is 04:00 UTC, so everything is hours off. | Shift times are built in IST on the *attendance date* (R1). |
| 3 | `compute_work_hours` | Uses Python `round()` (banker's rounding on binary floats) and keeps sub-seconds. | 18 s of work should be 0.01 h; `round(0.005, 2)` gives 0.01 or 0.0 depending on float noise. | Truncate to seconds; integer half-up: `(2*sec + 36) // 72` hundredths. |
| 4 | `compute_overtime` | No 30-minute minimum, ignores overnight shifts (end is next day), mixes IST/naive datetimes, uses the string date. | Punch-out 18:45 on a 18:30 shift returns 15 instead of 0. Overnight record returns negative/0. | Floor minutes since shift end (next day for overnight), count only if >= 30. |
| 5 | helpers (all) | Half-day rule (R5) never implemented, and nothing is truncated to whole seconds (R1). | `half_day` is always false. | `derive()` computes late/work/overtime/half-day from truncated instants. |
| 6 | `create_employee` | Check-then-insert race and no unique index: parallel creates can both succeed. | 20 parallel POSTs with one `emp_code` give several 201s. | Unique index on `emp_code`; catch `DuplicateKeyError` -> 409. |
| 7 | `EmployeeIn` | No validation: bad `emp_code`, bad email, `shift_start == shift_end`, `joined_on: "2026-02-30"` are accepted. | POST `{"emp_code": "x", ...}` returns 201. | Patterns/lengths, date check, shift-differs validator -> 422. |
| 8 | `create_employee` | `created_at = datetime.now()` is local and naive, and is returned as a datetime, not epoch ms. | Response `created_at` is an ISO string without zone. | UTC now (ms precision), returned as epoch ms. |
| 9 | `list_employees` | `skip = page * page_size` skips page 1 entirely; `total` counts the whole collection, not the filter; no sort; `page=0`/huge `page_size` accepted. | `GET /employees?page=1` misses the first rows; `total` ignores `department`. | `(page-1)*size`, `count_documents(filter)`, sort by `emp_code`, `Query(ge=1, le=100)`. |
| 10 | `punch_in` | Unknown employee -> `emp` is `None` -> `TypeError` -> **500**. | POST with an unknown `emp_code`. | 404 before use. |
| 11 | `punch_in` | Date comes from the server's local time (`fromtimestamp`, naive), not IST/R1, and ignores overnight shifts. | Server in UTC: a 00:30 IST punch-in lands on the previous date. | `attendance_date()` converts to IST and applies the overnight rule. |
| 12 | `punch_in` | `find_one` then `insert_one`: two simultaneous requests both pass the check. | 16 parallel punch-ins give several 201s (and with no unique index, duplicate documents). | Unique `(emp_code, date)` index; duplicate-key -> 409. |
| 13 | `PunchInIn` | `punched_at: Optional[int]` is lax: accepts `1.0`, `"123"`, seconds-looking values; `status` is any string (`ABSENT`, `foo`). | `punched_at: 1783312500` (seconds) creates a record in 1970. | Strict int with ms range; `status` limited to PRESENT/WFH/ON_DUTY. |
| 14 | `punch_in` response | Returns `id` (ObjectId string) and raw datetimes; contract forbids `_id`/`id` and requires epoch ms. | JSON has `"id"` and ISO timestamps. | `record_out()` shapes every response; legacy gaps (`history`, `half_day`) are defaulted. |
| 15 | `list_attendance` | Loads the entire collection, sorts in Python, slices in Python. At 100k records it is slow and memory-hungry. | Latency/memory grow with the collection; explain shows COLLSCAN. | Filter + sort + skip/limit run in MongoDB, backed by an index; `estimated_document_count` when unfiltered. |
| 16 | `list_attendance` | Sort is `date` only (no `emp_code` tie-break), no `date_from > date_to` check, no `status` validation, unbounded page size. | Ties come back in arbitrary order; `date_from=2026-07-09&date_to=2026-07-01` returns 200. | Sort `(date -1, emp_code 1)`, 422 checks, `Query` bounds. |
| 17 | startup / `/health` | No indexes at all; `/health` returns ok without touching MongoDB; client is not `tz_aware`, so datetimes come back naive. | Stop MongoDB: `/health` still says ok. Datetime arithmetic mixes naive/aware. | `ensure_indexes()` at startup (idempotent), `/health` pings (503 on failure), `tz_aware=True`. |

**Examined and judged fine:** `load_dotenv()` (it does not override real environment variables, which is what the contract asks); reading `MONGO_URI`/`MONGO_DB` with defaults; returning `_id`-less employee docs via `{"_id": 0}`; the overall shape of the `history`/derived-field defaults on a new record.

**Still open / known limits:** public API has no authentication (out of scope); the correction audit entry is written in the same document update (atomic), so no separate audit write can be lost.

# Guidelines for AI agents

## Project overview
Employee Attendance & Analytics API (HROne engineering assignment, contract v2.0). A FastAPI service over MongoDB: employees punch in and out, managers correct a day's record, and analytics answer who is late, which department is slipping, and what the trend looks like.

Read in this order before changing code:

1. `PROBLEM_STATEMENT.docx` — scope, evaluation, and what to submit.
2. `openapi.yaml` — paths, parameters, status codes, response fields, and rules R1–R10.
3. `DATA_MODEL.md` — collections, field types, and the nine sample documents.
4. `app/main.py` — the only application file. The starter runs; Part A is to find and fix its defects.
5. `REVIEW.md` and `DECISIONS.md` — templates for Parts A and C.

## Tech Stack
- Python 3.12
- FastAPI
- Pydantic
- PyMongo
- Uvicorn
- MongoDB Atlas Cloud

## Commands
Use the project virtual environment. Only run the below group of command if there is no `.venv` or if the user asks to reset it.

```bash
py -3.12 -m venv .venv

.venv\Scripts\activate  # if this command not works, try source .venv/Scripts/activate

# Installing the dependencies listed in requirements.txt
pip install -r requirements.txt

# Run
uvicorn app.main:app --port 8000 --reload
# For powershell, use .venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --reload
```

Do not start the server unless the user asks. Do not deploy.

## What to build
Starter routes exist in `app/main.py` and may contain defects (Part A): `GET /health`, `POST /employees`, `GET /employees`, `POST /attendance/punch-in`, `GET /attendance`.

Implement the rest exactly as `openapi.yaml` describes (Part B):

- `POST /attendance/punch-out` — work hours, overtime, half day (R3–R5), including across midnight.
- `PATCH /attendance/{emp_code}/{date}` — regularize one day, recompute derived fields, append history.
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}` — `explain` with `executionStats` for `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, `department_trend`. Same filters, sort, and pagination as the real endpoint. The plan must be IXSCAN with no COLLSCAN on about 100,000 attendance records.

Analytics endpoints are MongoDB aggregation pipelines. Do not fetch documents and loop in Python. Department trend produces exactly one row per calendar day in the database, including weekends and empty days, with a 7-day moving average. The late leaderboard uses competition ranking (1, 2, 2, 4): ties share a rank, the next rank is skipped, `limit` is applied after ranking, and tied rows sort by `emp_code`.

Status codes: 404 for a missing employee or a missing day's record, 409 for conflicts, 422 for validation failures (including a bad timestamp). A 500 is a failed test.

Concurrency: two simultaneous punch-ins for the same employee and date yield exactly one 201 and the rest 409. Two simultaneous punch-outs yield exactly one 200. Concurrent regularizations must not drop each other's history entries.

## Data and rules
- Two collections: `employees` and `attendance_logs`. Identity is `emp_code` (for example `EMP0001`), never MongoDB `_id`. `_id` never appears in a request or response. A record is addressed by `(emp_code, date)`.
- API instants (`punched_at`, `punch_in`, `punch_out`, `created_at`, history times) are epoch milliseconds. MongoDB stores BSON UTC datetimes; convert at the boundary. Seconds, floats, and strings are 422. PyMongo returns naive datetimes by default.
- Dates are `YYYY-MM-DD` (IST), months are `YYYY-MM`, shift times are `HH:MM` (24-hour IST).
- `work_hours`, `late_minutes`, `overtime_minutes`, and `half_day` are computed by the API, stored, and read back by analytics. Clients never send them. A missing `history` is `[]`; a missing `half_day` is `false`. A weekday with no record is not the same as `ABSENT`.
- Status: `PRESENT`, `WFH`, and `ON_DUTY` require `punch_in` and count as present. `ABSENT` and `LEAVE` have both punch times null and do not count as present.
- `history` is the audit trail of PATCH corrections only. Punch-in and punch-out never write it. Entries are never edited or removed.
- R1: IST (UTC+05:30). A record's date is the IST calendar date of `punch_in`. Overnight shifts (`shift_end <= shift_start`): a punch-in earlier than `shift_end` belongs to the shift that started the previous day. Truncate instants to whole seconds before any calculation; store and return the truncated value.
- R2: late only if punch-in is strictly more than 10 minutes after `shift_start`. `late_minutes` is whole minutes (floor) since `shift_start`, not since the end of grace. Shift 09:30: 09:40:00 → 0; 09:40:01 → 10; 10:15:59 → 45.
- R3: overtime is whole minutes (floor) from `shift_end` to `punch_out`, and only if that is at least 30; otherwise 0.
- R4: `work_hours` is `(punch_out - punch_in)` seconds / 3600, rounded half-up to 2 decimals. Null until punch-out.
- R5: after punch-out, `half_day` is true when rounded `work_hours` < 4.50. A half day counts as 0.5 of a present day.
- R7: working days are Monday–Friday, no holiday calendar, counted from `joined_on` for mid-month joiners. `present_days` counts only Mon–Fri records. Weekend records still add to late and overtime totals.
- R8: reported numbers use 2 decimals half-up; rates use 4 decimals half-up.
- R9: headcount is employees with `joined_on` on or before the end of the period, whether or not they have logs.
- R10: `page` starts at 1; `page_size` default 20, max 100; `total` always reflects the filters.
- Department summary: department lives on the employee; logs reference `emp_code`. Include people with no logs; exclude people who have not joined yet. The average is over records, not an average of averages.
- The seeded database has only the default `_id` index. List and analytics endpoints must be backed by indexes you create at startup.
- Test awkward cases yourself: no logs, ties, weekend records, a mid-month joiner, an overnight shift, a missing weekday. A pass on the nine samples is not a pass.

## Code style
- Keep the existing layout of `app/main.py`: helpers, models, then endpoints. Reuse helpers only after checking them against R1–R10; several starter helpers are wrong.
- Prefer small functions and typed Pydantic models. Do not use `Any` when a real type is known.
- Python formatting follows normal style: a space after `if`, `else` on the same line as the closing of the `if` block. Do not apply the brace style from other projects.
- Comments only where a rule or a concurrency constraint is not obvious from the code.
- Do not add a second module, a framework, or a rewrite of the starter into classes unless the contract requires it.

## Write-ups
- `REVIEW.md`: each defect with where it is, what is wrong, how you would notice it, and how you fixed it. Also note anything you examined and left as-is. Concrete failing inputs beat a long vague list.
- `README.md`: how to run, and anything the grader should know, including what is missing if the submission is partial.

## Security and files
- Do not read `.env`, virtual environments, or credential files.
- Ask before deleting a file or directory.
- Do not commit secrets.

## Changes and summaries
- Change only what the current task needs. Leave the contract files (`DECISIONS.md`, `REVIEW.md`, `README.md`, `openapi.yaml`, `DATA_MODEL.md`, `PROBLEM_STATEMENT.docx`, `sample_data/`) as they are unless the user asks to edit them.
- When the user asks for a summary of changes, keep it short and concrete. No emoji.
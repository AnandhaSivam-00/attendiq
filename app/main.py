"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
from math import dist
import os
import re
import calendar
from datetime import datetime, timedelta, timezone, date as dt_date, time as dt_time
from bson.codec_options import CodecOptions
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from typing import Annotated, Any, Literal
from decimal import Decimal, ROUND_HALF_UP

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")].with_options(
    codec_options=CodecOptions(tz_aware=True, tzinfo=timezone.utc)
)

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# Reject booleans, floats, strings, and out-of-range/second-looking numbers.
def validate_epoch_millis(v: Any) -> int:
    if type(v) is not int:
        raise ValueError("Instant must be a strict integer in epoch milliseconds")
    if v < MIN_EPOCH_MS or v > MAX_EPOCH_MS:
        raise ValueError(
            f"Instant must be between {MIN_EPOCH_MS} and {MAX_EPOCH_MS} ms"
        )
    return v

def validate_date_str(v: Any) -> str:
    if not isinstance(v, str) or not DATE_REGEX.match(v):
        raise ValueError("Date must be a string in YYYY-MM-DD format")
    try:
        dt_date.fromisoformat(v)
    except ValueError as exc:
        raise ValueError("Invalid calendar date") from exc
    return v

# --------------------------------------------------------------------------- #
# Validations & Literals
# --------------------------------------------------------------------------- #
DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_REGEX = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SHIFT_REGEX = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMP_CODE_REGEX = re.compile(r"^EMP\d{4,6}$")
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

StatusLiteral = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusLiteral = Literal["PRESENT", "WFH", "ON_DUTY"]

Tuple_Year_Month = tuple[int, int]
EpochMillis = Annotated[int, BeforeValidator(validate_epoch_millis)]
DateStr = Annotated[str, BeforeValidator(validate_date_str)]

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

# Truncates the milliseconds from a datetime object. 
# If dt is None, returns the current UTC time with milliseconds truncated.
def utc_truncate_milliseconds(dt: datetime | None):
    if not dt:
        return datetime.now(timezone.utc).replace(microsecond=0)
    return dt.replace(microsecond=0)

# Converts milliseconds since epoch to a UTC datetime object.
def ms_to_utc_dt(ms: int):
    truncated_sec = ms // 1000
    return datetime.fromtimestamp(truncated_sec, tz=timezone.utc)

def parse_hhmm(hhmm: str):
    h, m = map(int, hhmm.split(":"))
    return dt_time(hour=h, minute=m, second=0)

# Round half-up, avoiding Python's banker's rounding.
def round_half_up(value: float | Decimal, decimals: int = 2):
    q = Decimal("10") ** (-decimals)
    return float(Decimal(str(value)).quantize(q, ROUND_HALF_UP))

# Determine the IST attendance date for a punch_in instant (R1).
# For overnight shifts, a punch-in earlier than shift_end in IST belongs
# to the shift that started the previous calendar day.
def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str):
    ist_dt = punch_in_utc.astimezone(IST)
    att_date = ist_dt.date()
    if shift_end <= shift_start:
        if ist_dt.time() < parse_hhmm(shift_end):
            att_date = att_date - timedelta(days=1)
    return att_date.isoformat()

def build_shift_bounds(att_date_str: str, shift_start: str, shift_end: str):
    base_date = dt_date.fromisoformat(att_date_str)
    start_t = parse_hhmm(shift_start)
    end_t = parse_hhmm(shift_end)

    start_ist = datetime.combine(base_date, start_t, tzinfo=IST)
    end_date = (
        base_date + timedelta(days=1)
        if shift_end <= shift_start
        else base_date
    )
    end_ist = datetime.combine(end_date, end_t, tzinfo=IST)
    return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)

# Rule R2: 10-minute grace period (600s).
# Strictly more than 10:00 after shift_start -> floor minutes since shift_start.
def compute_late_minutes(punch_in_utc: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    shift_start_utc, _ = build_shift_bounds(att_date_str, shift_start, shift_end)
    diff_seconds = (punch_in_utc - shift_start_utc).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)

# Whole minutes (floor) from shift_end to punch_out if >= 30, else 0.
def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    _, shift_end_utc = build_shift_bounds(att_date_str, shift_start, shift_end)
    diff_seconds = (punch_out_utc - shift_end_utc).total_seconds()
    minutes = int(diff_seconds // 60)

    return minutes if minutes >= 30 else 0

def parse_date_param(v: str, param_name: str = "date"):
    if not isinstance(v, str) or not DATE_REGEX.match(v):
        raise HTTPException(status_code=422, detail=f"Invalid {param_name} format")
    try:
        return dt_date.fromisoformat(v)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid calendar {param_name}")

# Rule R7: Mon-Fri in month, counted from joined_on for mid-month joiners.
def compute_working_days_in_month(m_start: dt_date, m_end: dt_date, joined_on_str: str):
    joined_dt = dt_date.fromisoformat(joined_on_str)

    if joined_dt > m_end:
        return 0

    curr = max(m_start, joined_dt)
    count = 0
    while curr <= m_end:
        if curr.weekday() < 5:
            count += 1
        curr += timedelta(days=1)
    return count

# Getting the first and last date of a month from a YYYY-MM string.
def get_month_start_end(month_str: str):
    year, month = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, month)
    return month, dt_date(year, month, 1), dt_date(year, month, last_day)


# --------------------------------------------------------------------------- #
# Models with base validation
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: DateStr

    @model_validator(mode="after")
    def check_shifts_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., min_length=1)
    punched_at: EpochMillis | None = None
    status: PresenceStatusLiteral = "PRESENT"

    @model_validator(mode="before")
    @classmethod
    def check_non_null_defaults(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "status" in data and data["status"] is None:
                raise ValueError("status cannot be null")
        return data


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., min_length=1)
    punched_at: EpochMillis | None = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: StatusLiteral | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in ("status", "punch_in", "punch_out"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} cannot be explicitly null")
        return data

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(503, f"MongoDB connection failed: {e}")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = utc_truncate_milliseconds(None)

    try:
        db.employees.insert_one(doc)
        doc.pop("_id", None)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return doc


@app.get("/employees")
def list_employees(
    department: str | None = None, 
    page: int = Query(1, ge=1), 
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}

    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = ms_to_utc_dt(body.punched_at) if body.punched_at is not None else utc_truncate_milliseconds(None)
    attendance_date = get_attendance_date(ts, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(ts, emp["shift_start"], emp["shift_end"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        res = db.attendance_logs.insert_one(doc)
        doc["id"] = str(res.inserted_id)
        doc.pop("_id", None)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this attendance date")

    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if date_from is not None:
        parse_date_param(date_from, "date_from")
    if date_to is not None:
        parse_date_param(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from cannot be after date_to")

    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    items = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    return {"items": items, "total": total, "page": page, "page_size": page_size}



# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #

@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp_data = db.employees.find_one({"emp_code": body.emp_code})
    if not emp_data:
        raise HTTPException(404, "Employee not found")

    punch_out_utc = (
        ms_to_utc_dt(body.punched_at)
        if body.punched_at is not None
        else utc_truncate_milliseconds(None)
    )

    entry_open = db.attendance_logs.find_one({
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punch_out_utc},
        },
        sort=[("punch_in", DESCENDING)])

    if not entry_open: # extra checking
        future_entry_close = db.attendance_logs.find_one({
            "emp_code": body.emp_code, 
            "punch_in": {"$gt": punch_out_utc}
        })
        if future_entry_close:
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(404, "No matching punch_in record found")

    if entry_open.get("punch_out") is not None: # extra checking
        future_entry_open = db.attendance_logs.find_one({
            "emp_code": body.emp_code,
            "punch_in": {"$gt": punch_out_utc},
            "punch_out": None,
        })
        if future_entry_open:
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(409, "Record is already punched out")

    punch_in_utc = entry_open["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    # Ensures punch_out is strictly after punch_in and within 24 hours.
    elapsed_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if elapsed_sec <= 0:
        raise HTTPException(422, "punched_at must be strictly after punch_in")
    if elapsed_sec > 24 * 60 * 60:
        raise HTTPException(422, "punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_minutes = compute_overtime(punch_out_utc, emp_data["shift_start"], emp_data["shift_end"], entry_open["date"])
    half_day = work_hours < 4.50

    updated_entry = db.attendance_logs.find_one_and_update({
        "_id": entry_open["_id"],
        "punch_out": None
    },
    {
        "$set": {
            "punch_out": punch_out_utc,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }
    },
        return_document=ReturnDocument.AFTER)
    if not updated_entry:
        raise HTTPException(409, "Record is already punched out")
    return updated_entry


@app.patch("/attendance/{emp_code}/{date}")
def patch_attendance(emp_code: str, date: str, body: RegularizeRequest):
    parse_date_param(date, "date")

    emp_data = db.employees.find_one({ "emp_code": emp_code })
    if not emp_data:
        raise HTTPException(404, "Employee not found")

    entry = db.attendance_logs.find_one({ 
        "emp_code": emp_code, 
        "date": date 
    })
    if not entry:
        raise HTTPException(404, "Attendance record not found for this date")

    old_status: str = entry.get("status")
    old_punch_in: datetime | None = entry.get("punch_in")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)

    old_punch_out: datetime | None = entry.get("punch_out")
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    old_work_hours: float | None = (
        round_half_up(entry["work_hours"], 2)
        if entry.get("work_hours") is not None
        else None
    )
    old_late_minutes: int = int(entry.get("late_minutes") or 0)
    old_overtime_minutes: int = int(entry.get("overtime_minutes") or 0)
    old_half_day: bool = bool(entry.get("half_day", False))

    new_status = body.status if body.status is not None else old_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch_in or punch_out when status is ABSENT or LEAVE")
        new_punch_in: datetime | None = None
        new_punch_out: datetime | None = None
        new_work_hours: float | None = None
        new_late_minutes: int = 0
        new_overtime_minutes: int = 0
        new_half_day: bool = False
    else:
        new_punch_in = (
            ms_to_utc_dt(body.punch_in)
            if body.punch_in is not None
            else old_punch_in
        )
        new_punch_out = (
            ms_to_utc_dt(body.punch_out)
            if body.punch_out is not None
            else old_punch_out
        )

        if new_punch_in is None:
            raise HTTPException(422, "A presence status requires punch_in")

        expected_att_date = get_attendance_date(new_punch_in, emp_data["shift_start"], emp_data["shift_end"])
        if expected_att_date != date:
            raise HTTPException(422, "punch_in does not belong to the record's attendance date (R1)")

        new_late_minutes = compute_late_minutes(new_punch_in, emp_data["shift_start"], emp_data["shift_end"], date)

        if new_punch_out is not None:
            elapsed = (new_punch_out - new_punch_in).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp_data["shift_start"], emp_data["shift_end"], date)
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    comparisons = [
        ("status", old_status, new_status),
        ("punch_in", old_punch_in, new_punch_in),
        ("punch_out", old_punch_out, new_punch_out),
        ("work_hours", old_work_hours, new_work_hours),
        ("late_minutes", old_late_minutes, new_late_minutes),
        ("overtime_minutes", old_overtime_minutes, new_overtime_minutes),
        ("half_day", old_half_day, new_half_day),
    ]

    changes: dict[str, dict[str, Any]] = {}
    for field_name, old_val, new_val in comparisons:
        if old_val != new_val:
            changes[field_name] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(422, "Regularization request changes nothing")

    history_entry = {
        "at": utc_truncate_milliseconds(None),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    old_history_len = len(entry.get("history") or [])
    updated_entry = db.attendance_logs.find_one_and_update({
        "_id": entry["_id"],
        "status": old_status,
        "punch_in": entry.get("punch_in"),
        "punch_out": entry.get("punch_out"),
        "$expr": {
            "$eq": [
                {"$size": {"$ifNull": ["$history", []]}},
                old_history_len,
            ]
        },
    },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
    },
        return_document=ReturnDocument.AFTER)

    if not updated_entry:
        raise HTTPException(409, "Concurrent modification detected; please retry")

    return updated_entry

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_summary(emp_code: str, month_str: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp_data = db.employees.find_one({"emp_code": emp_code})
    if not emp_data:
        raise HTTPException(404, "Employee not found")

    month, month_start, month_end = get_month_start_end(month_str)

    working_days = compute_working_days_in_month(month_start, month_end, emp_data["joined_on"])

    aggregate_pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": { "$gte": month_start, "$lte": month_end },
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                                                }
                                            },
                                            5,
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    { "$eq": [{"$ifNull": ["$half_day", False]}, True] }, 
                                    0.5, 
                                    1.0
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "month": {"$literal": month},
                "working_days": {"$literal": working_days},
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {"$literal": None},
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(aggregate_pipeline))

    if not results:
        return {
            "emp_code": emp_code,
            "month": month,
            "working_days": working_days,
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
            "attendance_pct": 0.0 if working_days > 0 else None,
        }

    result = results[0]
    if working_days > 0:
        result["attendance_pct"] = round_half_up(result["present_days"] / working_days * 100, 2)
    else:
        result["attendance_pct"] = None
    return result


@app.get("/analytics/departments/summary")
def department_summary(month_str: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: str | None = None):
    month, month_start, month_end = get_month_start_end(month_str)

    emp_match: dict[str, Any] = { "joined_on": { "$lte": month_end } }
    if department is not None:
        emp_match["department"] = department

    aggregate_pipeline = [
        {
            "$match": emp_match
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": month_start, "$lte": month_end}
                        }
                    }
                ],
                "as": "logs"
            }
        },
        { "$unwind": { "path": "$logs", "preserveNullAndEmptyArrays": True } },
        {
            "$group": {
                "_id": "$department",
                "emp_set": { "$addToSet": "$emp_code" },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lte": [
                                        {
                                            "$isoDayOfWeek": {
                                                "$dateFromString": {
                                                    "dateString": "$logs.date",
                                                    "format": "%Y-%m-%d",
                                                }
                                            }
                                        },
                                        5,
                                    ]}
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                    0.5,
                                    1.0
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "wh_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            {"$toDecimal": "$logs.work_hours"},
                            Decimal128("0"),
                        ]
                    }
                },
                "wh_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$emp_set"},
                "present_days": 1,
                "avg_work_hours": {"$literal": None},
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "wh_sum": 1,
                "wh_count": 1,
            }
        },
        {
            "$sort": {
                "department": 1
            }
        },
    ]

    items = list(db.employees.aggregate(aggregate_pipeline))

    for item in items:
        wh_count = item.get("wh_count", 0)
        if wh_count > 0:
            wh_sum = item.get("wh_sum", 0)
            item["avg_work_hours"] = round_half_up(wh_sum / wh_count, 2)
        else:
            item["avg_work_hours"] = None
        # Remove internal fields
        item.pop("wh_sum", None)
        item.pop("wh_count", None)

    return {
        "month": month_str, 
        "items": items
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month_str: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), 
    limit: int = Query(10, ge=1, le=50),
    department: str | None = None,
):
    _, month_start, month_end = get_month_start_end(month_str)

    emp_match: dict[str, Any] = { "joined_on": { "$lte": month_end } }
    if department is not None:
        emp_match["department"] = department

    aggregate_pipeline = [
        {
            "$match": emp_match
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": month_start, "$lte": month_end}
                        }
                    }
                ],
                "as": "logs"
            }
        },
        { "$unwind": { "path": "$logs", "preserveNullAndEmptyArrays": True } },
        {
            "$group": {
                "_id": "$emp_code",
                "name": { "$first": "$name" },
                "department": { "$first": "$department" },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": 1,
                "department": 1,
                "late_count": 1,
                "total_late_minutes": 1,
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1
            }
        },
        {
            "$limit": limit
        }
    ]

    items = list(db.employees.aggregate(aggregate_pipeline))

    return {
        "month": month_str,
        "items": items
    }
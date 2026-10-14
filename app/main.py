"""
Employee Attendance & Analytics API - HROne assignment v2.0
All application code is intentionally kept in this file.
"""
import os
import re
import calendar
from pathlib import Path
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as ApiPath
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.collection import Collection

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUS = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------- validation ---------------------------------
def valid_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise HTTPException(422, "date must be YYYY-MM-DD")


def valid_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, value.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def parse_shift(value: str) -> time:
    try:
        parsed = time.fromisoformat(value)
        if parsed.second or parsed.microsecond or len(value) != 5:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise HTTPException(422, "shift times must be HH:MM")


def round_half_up(value: float, places: int = 2) -> float:
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def from_epoch_ms(value: int) -> datetime:
    # Pydantic validates type/range before this helper is called.
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0).replace(tzinfo=None)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    # MongoDB stores naive datetimes as UTC; the API exposes epoch milliseconds.
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return int(aware.timestamp() * 1000)


def now_utc_naive() -> datetime:
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def ist_datetime(dt: datetime) -> datetime:
    # Naive values read from MongoDB represent UTC instants.
    aware = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return aware.astimezone(IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = ist_datetime(punch_in)
    end_t = parse_shift(shift_end)
    start_t = parse_shift(shift_start)
    day = local.date()
    if end_t <= start_t and local.time().replace(tzinfo=None) < end_t:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_end_instant(day_str: str, shift_start: str, shift_end: str) -> datetime:
    day = valid_date(day_str)
    start_t, end_t = parse_shift(shift_start), parse_shift(shift_end)
    end_day = day + timedelta(days=1) if end_t <= start_t else day
    local = datetime.combine(end_day, end_t, tzinfo=IST)
    return local.astimezone(UTC).replace(tzinfo=None)


def late_minutes(punch_in: datetime, shift_start: str, day: str) -> int:
    local = ist_datetime(punch_in)
    start_t = parse_shift(shift_start)
    start_local = datetime.combine(valid_date(day), start_t, tzinfo=IST)
    elapsed = (local - start_local).total_seconds()
    if elapsed <= 600:
        return 0
    return max(0, int(elapsed // 60))


def derived_fields(emp: dict, day: str, punch_in: Optional[datetime],
                   punch_out: Optional[datetime]) -> dict:
    if punch_in is None:
        return {"late_minutes": 0, "work_hours": None,
                "overtime_minutes": 0, "half_day": False}
    late = late_minutes(punch_in, emp["shift_start"], day)
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None,
                "overtime_minutes": 0, "half_day": False}
    seconds = (punch_out - punch_in).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    hours = round_half_up(seconds / 3600, 2)
    end = shift_end_instant(day, emp["shift_start"], emp["shift_end"])
    overtime_delta = (punch_out - end).total_seconds()
    overtime = int(overtime_delta // 60) if overtime_delta >= 0 else 0
    if overtime < 30:
        overtime = 0
    return {"late_minutes": late, "work_hours": hours,
            "overtime_minutes": overtime, "half_day": hours < 4.50}


def serialize_employee(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    if "created_at" in result:
        result["created_at"] = to_epoch_ms(result["created_at"])
    return result


def serialize_attendance(doc: dict) -> dict:
    result = {k: v for k, v in doc.items() if k != "_id"}
    result.setdefault("history", [])
    result.setdefault("half_day", False)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    for key in ("punch_in", "punch_out"):
        result[key] = to_epoch_ms(result.get(key))
    history = []
    for entry in result.get("history", []):
        e = dict(entry)
        e["at"] = to_epoch_ms(e.get("at"))
        changes = {}
        for field, change in e.get("changes", {}).items():
            change = dict(change)
            if field in ("punch_in", "punch_out"):
                change["from"] = to_epoch_ms(change.get("from"))
                change["to"] = to_epoch_ms(change.get("to"))
            changes[field] = change
        e["changes"] = changes
        history.append(e)
    result["history"] = history
    return result


def round2_expr(field: str) -> dict:
    # MongoDB $round uses ties-to-even; final reported aggregates use Decimal half-up.
    return {"$round": [field, 2]}


def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)],
                              name="ix_employee_department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)],
                                    unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)],
                                    name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
                                    name="ix_attendance_employee_date_status")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)],
                                    name="ix_attendance_status_date")
    db.attendance_logs.create_index([("punch_in", DESCENDING), ("emp_code", ASCENDING)],
                                    name="ix_attendance_punch_in")


@app.on_event("startup")
def startup_indexes():
    create_indexes()


# --------------------------------- models ------------------------------------
class EpochMillis(int):
    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        from pydantic_core import core_schema
        return core_schema.no_info_after_validator_function(
            cls.validate, core_schema.int_schema(strict=True)
        )

    @classmethod
    def validate(cls, value):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be an integer epoch-milliseconds value")
        if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
            raise ValueError("epoch milliseconds out of range")
        return value


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, v):
        valid_date(v)
        return v

    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------- helpers -----------------------------------
def employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


def month_query(month: str) -> dict:
    start, end = valid_month(month)
    return {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    total = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)
    return total


def present_contribution(doc: dict) -> float:
    if doc.get("status") not in PRESENCE:
        return 0.0
    return 0.5 if doc.get("half_day", False) else 1.0


def jsonable_explain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): jsonable_explain(v) for k, v in value.items() if k != "queryPlanner" or True}
    if isinstance(value, list):
        return [jsonable_explain(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "binary") and hasattr(value, "generation_time"):
        return str(value)
    return value


# -------------------------------- endpoints ----------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_naive()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    docs = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)
    return {"items": [serialize_employee(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employee_or_404(body.emp_code)
    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc_naive()
    ts = truncate_seconds(ts)
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": ts, "punch_out": None, "work_hours": None,
        **derived_fields(emp, day, ts, None), "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from:
        valid_date(date_from)
    if date_to:
        valid_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        if status not in ALL_STATUS:
            raise HTTPException(422, "invalid status")
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employee_or_404(body.emp_code)
    ts = from_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc_naive()
    ts = truncate_seconds(ts)
    # Most recent still-open punch-in not later than the punch-out instant.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts},
         "punch_out": None, "status": {"$in": list(PRESENCE)}},
        sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        # Distinguish already-closed most recent record from no punch-in.
        closed = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
            sort=[("punch_in", DESCENDING)]
        )
        if closed and closed.get("punch_out") is not None:
            raise HTTPException(409, "record is already punched out")
        raise HTTPException(404, "no punch-in found")
    seconds = (ts - rec["punch_in"]).total_seconds()
    if seconds <= 0 or seconds > 24*3600:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    vals = derived_fields(emp, rec["date"], rec["punch_in"], ts)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **vals}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record is already punched out")
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    valid_date(date)
    emp = employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "attendance record not found")
    old = dict(rec)
    status = body.status if body.status is not None else rec["status"]
    punch_in = from_epoch_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
    punch_out = from_epoch_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        punch_in, punch_out = None, None
    elif status in PRESENCE and punch_in is None:
        raise HTTPException(422, "presence status requires punch_in")
    if punch_in is not None:
        punch_in = truncate_seconds(punch_in)
        if attendance_date(punch_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
    if punch_out is not None:
        punch_out = truncate_seconds(punch_out)
    if status in ("ABSENT", "LEAVE") and (punch_in is not None or punch_out is not None):
        raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
    vals = derived_fields(emp, date, punch_in, punch_out)
    new_fields = {"status": status, "punch_in": punch_in, "punch_out": punch_out, **vals}
    changes = {}
    for key, new_value in new_fields.items():
        old_value = old.get(key)
        if old_value != new_value:
            if key in ("punch_in", "punch_out"):
                changes[key] = {"from": old_value, "to": new_value}
            else:
                changes[key] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    history_entry = {"at": now_utc_naive(), "by": body.regularized_by,
                     "reason": body.reason, "changes": changes}
    update = {"$set": new_fields, "$push": {"history": history_entry}}
    # Compare-and-set the old fields to avoid silently overwriting a concurrent edit.
    filt = {"_id": rec["_id"]}
    for key in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if key in old:
            filt[key] = old[key]
        else:
            filt[key] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(filt, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry")
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = employee_or_404(emp_code)
    start, end = valid_month(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", list(PRESENCE)]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                ]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }}
    ]
    result = list(db.attendance_logs.aggregate(pipeline))
    values = result[0] if result else {}
    month_start = max(start, valid_date(emp["joined_on"]))
    workdays = working_days(month_start, end) if month_start <= end else 0
    present = float(values.get("present_days", 0))
    pct = round_half_up(present / workdays * 100, 2) if workdays else None
    return {"emp_code": emp_code, "month": month, "working_days": workdays,
            "present_days": round_half_up(present, 2), "leave_days": values.get("leave_days", 0),
            "late_count": values.get("late_count", 0), "total_late_minutes": values.get("total_late_minutes", 0),
            "total_overtime_minutes": values.get("total_overtime_minutes", 0), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = valid_month(month)
    employee_match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    pipeline = [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "dept_join": "$joined_on"},
                     "pipeline": [{"$match": {"$expr": {"$and": [
                         {"$eq": ["$emp_code", "$$code"]},
                         {"$gte": ["$date", start.isoformat()]}, {"$lte": ["$date", end.isoformat()]},
                     ]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department",
            "headcount_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE)]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]},
                ]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0
            ]}},
            "hours_sum": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]},
                "$logs.work_hours", 0]}},
            "hours_count": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]},
                1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount_codes"},
                      "present_days": 1,
                      "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]},
                          {"$divide": ["$hours_sum", "$hours_count"]}, None]},
                      "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}},
    ]
    items = []
    for row in db.employees.aggregate(pipeline, allowDiskUse=True):
        row["present_days"] = round_half_up(row.get("present_days", 0), 2)
        row["avg_work_hours"] = round_half_up(row["avg_work_hours"], 2) if row.get("avg_work_hours") is not None else None
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end = valid_month(month)
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = valid_date(from_date), valid_date(to_date)
    if end < start:
        raise HTTPException(422, "to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range must not exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")
    # MongoDB $densify uses an exclusive upper bound. Add one day so the
    # requested end date is included in the generated daily series.
    start_dt = datetime.combine(start, time.min)
    end_exclusive = datetime.combine(end + timedelta(days=1), time.min)

    # All day filling and window calculations are performed in MongoDB.
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department, "employee.joined_on": {"$lte": end.isoformat()}}},
        {"$group": {"_id": "$date",
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "date": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day",
            "bounds": [start_dt, end_exclusive]}}},
        {"$set": {"date_str": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d"}},
                  "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$date"}, 2]},
                                                {"$lte": [{"$dayOfWeek": "$date"}, 6]}]},
                  "present_count": {"$ifNull": ["$present_count", 0]},
                  "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$lookup": {"from": "employees", "let": {"d": "$date_str"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}
            ]}}}, {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}},
        },
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
                "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}},
    ]
    items = []
    for row in db.attendance_logs.aggregate(pipeline, allowDiskUse=True):
        for key in ("attendance_rate", "moving_avg_7d"):
            if row.get(key) is not None:
                row[key] = round_half_up(row[key], 4)
        row["present_count"] = round_half_up(row.get("present_count", 0), 2)
        items.append(row)
    # A MongoDB $densify stage emits dates only if at least one input row exists.
    # For a department with zero records in the whole requested range, seed a
    # single anchor row so the database can densify the range.
    if not items:
        anchor = {
            "date": start_dt,
            "present_count": 0,
            "late_count": 0,
        }
        seed_pipeline = [
            {"$documents": [anchor]},
            {"$densify": {"field": "date", "range": {"step": 1, "unit": "day",
                "bounds": [start_dt, end_exclusive]}}},
            {"$set": {"date_str": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d"}},
                "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$date"}, 2]},
                    {"$lte": [{"$dayOfWeek": "$date"}, 6]}]}}},
            {"$lookup": {"from": "employees", "let": {"d": "$date_str"},
                "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$department", department]},
                    {"$lte": ["$joined_on", "$$d"]}]}}}, {"$count": "n"}], "as": "hc"}},
            {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                "attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": [
                    {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}, 0]}]}, 0, None]}}},
            {"$setWindowFields": {"sortBy": {"date": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
            {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1,
                "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
            {"$sort": {"date": 1}},
        ]
        # $documents must be the first stage and is supported on MongoDB 6.0+.
        for row in db.aggregate(seed_pipeline):
            for key in ("attendance_rate", "moving_avg_7d"):
                if row.get(key) is not None:
                    row[key] = round_half_up(row[key], 4)
            row["present_count"] = round_half_up(row.get("present_count", 0), 2)
            items.append(row)
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from:
            valid_date(date_from)
        if date_to:
            valid_date(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status:
            if status not in ALL_STATUS: raise HTTPException(422, "invalid status")
            q["status"] = status
        exp = db.command({"explain": {"find": "attendance_logs", "filter": q,
            "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size},
            "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": jsonable_explain(exp)}
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        start, end = valid_month(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                    {"$group": {"_id": "$emp_code", "records": {"$sum": 1}}}]
        exp = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                          "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": jsonable_explain(exp)}
    if endpoint == "department_summary":
        if not month: raise HTTPException(422, "month is required")
        start, end = valid_month(month)
        pipeline = [{"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                    {"$group": {"_id": "$emp_code", "records": {"$sum": 1}}}]
        exp = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                          "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": jsonable_explain(exp)}
    if endpoint == "late_leaderboard":
        if not month: raise HTTPException(422, "month is required")
        start, end = valid_month(month)
        pipeline = [{"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
                                "late_minutes": {"$gt": 0}}},
                    {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}]
        exp = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                          "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": jsonable_explain(exp)}
    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        start, end = valid_date(from_date), valid_date(to_date)
        if end < start or (end-start).days+1 > 92:
            raise HTTPException(422, "invalid trend date range")
        pipeline = [{"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                    {"$group": {"_id": "$date", "records": {"$sum": 1}}}]
        exp = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                          "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": jsonable_explain(exp)}

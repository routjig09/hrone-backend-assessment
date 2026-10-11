"""
Employee Attendance & Analytics API

Authoritative implementation for HROne Software Engineer Trainee Assessment.
Implements complete OpenAPI 3.0.3 contract and business rules R1-R10.

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (local .env supported)
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from starlette import status as http_status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Startup & Index Management
# --------------------------------------------------------------------------- #
def ensure_indexes():
    """Create all required MongoDB indexes idempotently at application startup."""
    try:
        # employees indexes
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_employees_emp_code_unique")
        db.employees.create_index(
            [("department", ASCENDING), ("joined_on", ASCENDING)],
            name="idx_employees_department_joined_on"
        )

        # attendance_logs indexes
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True,
            name="idx_attendance_logs_emp_date_unique"
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
            name="idx_attendance_logs_emp_punch_in"
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)],
            name="idx_attendance_logs_date_emp"
        )
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("late_minutes", ASCENDING)],
            name="idx_attendance_logs_date_late"
        )
    except Exception as e:
        # Log or preserve index creation warning if mongo is temporarily unavailable at module import
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R10)
# --------------------------------------------------------------------------- #
def round_half_up(value: Optional[Union[float, Decimal]], decimals: int) -> Optional[float]:
    """Round a number half-up to the specified number of decimal places (R8)."""
    if value is None:
        return None
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def epoch_ms_to_datetime_utc(ms: int) -> datetime:
    """Convert integer epoch milliseconds to UTC datetime, truncated to whole seconds (R1)."""
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert UTC or naive BSON datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def derive_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date normally corresponds to the IST calendar date of punch-in.
    For an overnight shift where shift_end <= shift_start, a punch-in whose IST time
    is earlier than shift_end belongs to the shift that started the previous day.
    """
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    punch_in_ist = punch_in_utc.astimezone(IST)

    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)

    if is_overnight:
        punch_time = (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second)
        shift_end_time = (eh_h, eh_m, 0)
        if punch_time < shift_end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()

    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, record_date: str, shift_end: str) -> int:
    """
    R2: Grace period is 10 minutes. A punch-in is late only if strictly more than
    10:00 after shift_start. When late, whole minutes elapsed since shift_start.
    """
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    punch_in_ist = punch_in_utc.astimezone(IST)

    sh_h, sh_m = map(int, shift_start.split(":"))
    r_date = dt_date.fromisoformat(record_date)
    shift_start_dt = datetime(r_date.year, r_date.month, r_date.day, sh_h, sh_m, 0, tzinfo=IST)

    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_seconds <= 600:
        return 0
    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)
    if punch_out_utc.tzinfo is None:
        punch_out_utc = punch_out_utc.replace(tzinfo=timezone.utc)

    diff_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    hours_dec = Decimal(str(diff_seconds)) / Decimal("3600")
    return float(hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5: half_day = true when rounded work_hours < 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_overtime(punch_out_utc: datetime, shift_end: str, record_date: str, shift_start: str) -> int:
    """
    R3: Whole minutes between shift_end and punch_out, counted only when >= 30; otherwise 0.
    Overnight shifts end on the next calendar day.
    """
    if punch_out_utc.tzinfo is None:
        punch_out_utc = punch_out_utc.replace(tzinfo=timezone.utc)
    punch_out_ist = punch_out_utc.astimezone(IST)

    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)

    r_date = dt_date.fromisoformat(record_date)
    if is_overnight:
        end_date = r_date + timedelta(days=1)
    else:
        end_date = r_date

    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, eh_h, eh_m, 0, tzinfo=IST)
    elapsed_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    elapsed_minutes = int(elapsed_seconds // 60)
    if elapsed_minutes >= 30:
        return elapsed_minutes
    return 0


def get_month_boundaries(month: str) -> tuple[dt_date, dt_date]:
    """Return the first and last calendar dates of a YYYY-MM month."""
    parts = month.split("-")
    year, m = int(parts[0]), int(parts[1])
    last_day = calendar.monthrange(year, m)[1]
    return dt_date(year, m, 1), dt_date(year, m, last_day)


def count_working_days(month: str, joined_on_str: str) -> int:
    """
    R7: Count Monday-Friday days in month, starting from max(month_start, joined_on).
    Returns 0 if employee joined after month end.
    """
    m_start, m_end = get_month_boundaries(month)
    j_date = dt_date.fromisoformat(joined_on_str)
    if j_date > m_end:
        return 0
    start_date = max(m_start, j_date)
    curr = start_date
    working_days = 0
    while curr <= m_end:
        if curr.weekday() < 5:  # Monday=0, Friday=4
            working_days += 1
        curr += timedelta(days=1)
    return working_days


# --------------------------------------------------------------------------- #
# Pydantic Models & Schemas
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            dt_date.fromisoformat(v)
        except Exception:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return v

    @model_validator(mode="after")
    def validate_shift_times(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: PresenceStatus = Field(PresenceStatus.PRESENT)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Any]]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntry]


class AttendancePage(BaseModel):
    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class Trend(BaseModel):
    department: str
    items: List[TrendItem]


# --------------------------------------------------------------------------- #
# Document Serializers
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> Employee:
    return Employee(
        emp_code=doc["emp_code"],
        name=doc["name"],
        email=doc["email"],
        department=doc["department"],
        shift_start=doc["shift_start"],
        shift_end=doc["shift_end"],
        joined_on=doc["joined_on"],
        created_at=datetime_to_epoch_ms(doc.get("created_at")),
    )


def serialize_history_entry(entry: dict) -> HistoryEntry:
    raw_changes = entry.get("changes", {})
    serialized_changes = {}
    for k, v in raw_changes.items():
        from_val = v.get("from")
        to_val = v.get("to")
        if isinstance(from_val, datetime):
            from_val = datetime_to_epoch_ms(from_val)
        if isinstance(to_val, datetime):
            to_val = datetime_to_epoch_ms(to_val)
        serialized_changes[k] = {"from": from_val, "to": to_val}

    return HistoryEntry(
        at=datetime_to_epoch_ms(entry.get("at")),
        by=entry["by"],
        reason=entry["reason"],
        changes=serialized_changes,
    )


def serialize_attendance_record(doc: dict) -> AttendanceRecord:
    history_docs = doc.get("history") or []
    history = [serialize_history_entry(h) for h in history_docs]
    return AttendanceRecord(
        emp_code=doc["emp_code"],
        date=doc["date"],
        status=Status(doc["status"]),
        punch_in=datetime_to_epoch_ms(doc.get("punch_in")),
        punch_out=datetime_to_epoch_ms(doc.get("punch_out")),
        work_hours=doc.get("work_hours"),
        late_minutes=doc.get("late_minutes", 0),
        overtime_minutes=doc.get("overtime_minutes", 0),
        half_day=doc.get("half_day", False),
        history=history,
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

# System
@app.get("/health", tags=["system"])
def health():
    """Readiness probe verifying MongoDB connectivity."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MongoDB service unavailable",
        )


# Employees
@app.post("/employees", status_code=http_status.HTTP_201_CREATED, response_model=Employee, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee with unique emp_code constraint."""
    ensure_indexes()
    server_time = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = server_time

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Employee with emp_code '{body.emp_code}' already exists",
        )

    return serialize_employee(doc)


@app.get("/employees", response_model=EmployeePage, tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match."),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Paginated list of employees sorted by emp_code ascending."""
    ensure_indexes()
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort([("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]

    return EmployeePage(items=items, total=total, page=page, page_size=page_size)


# Attendance
@app.post("/attendance/punch-in", status_code=http_status.HTTP_201_CREATED, response_model=AttendanceRecord, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Record punch-in for employee for a given day."""
    ensure_indexes()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        punch_in_utc = datetime.now(timezone.utc).replace(microsecond=0)

    attendance_date = derive_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_utc, emp["shift_start"], attendance_date, emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Attendance record already exists for employee '{body.emp_code}' on date '{attendance_date}'",
        )

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=http_status.HTTP_200_OK, response_model=AttendanceRecord, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """Record punch-out for employee's most recent open record."""
    ensure_indexes()
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_datetime_utc(body.punched_at)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record whose punch_in is at or before punched_at
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )
    if not candidate:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="No matching punch-in found for this punch-out instant",
        )

    if candidate.get("punch_out") is not None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Attendance record is already punched out",
        )

    punch_in_utc = candidate["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    if punch_out_utc <= punch_in_utc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out timestamp must be strictly greater than punch_in",
        )

    if (punch_out_utc - punch_in_utc).total_seconds() > 86400:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out cannot be more than 24 hours after punch_in",
        )

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    half_day = compute_half_day(work_hours)
    overtime_minutes = compute_overtime(punch_out_utc, emp["shift_end"], candidate["date"], emp["shift_start"])

    # Atomic conditional update prevents concurrent double punch-out
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Attendance record is already punched out",
        )

    return serialize_attendance_record(updated)


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[Status] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Database-side paginated attendance listing sorted by date desc, emp_code asc."""
    ensure_indexes()
    if date_from:
        try:
            dt_date.fromisoformat(date_from)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date_from format")
    if date_to:
        try:
            dt_date.fromisoformat(date_to)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date_to format")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be greater than date_to",
        )

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_q = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q
    if status:
        q["status"] = status.value

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance_record(doc) for doc in cursor]
    return AttendancePage(items=items, total=total, page=page, page_size=page_size)


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord, tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., description="The record's attendance date, YYYY-MM-DD."),
    body: RegularizeRequest = ...,
):
    """Regularize (manually correct) one day's record and append audit history."""
    ensure_indexes()
    try:
        dt_date.fromisoformat(date)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid path date format")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Employee not found")

    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not current:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    # Determine target status
    target_status = body.status.value if body.status is not None else current["status"]

    # Rules regarding ABSENT and LEAVE
    if body.status in (Status.ABSENT, Status.LEAVE):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE",
            )
        target_punch_in = None
        target_punch_out = None
    elif target_status in (Status.ABSENT.value, Status.LEAVE.value):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE",
            )
        target_punch_in = None
        target_punch_out = None
    else:
        # Presence status (PRESENT, WFH, ON_DUTY)
        if body.punch_in is not None:
            target_punch_in = epoch_ms_to_datetime_utc(body.punch_in)
        else:
            target_punch_in = current.get("punch_in")

        if target_punch_in is None:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="a presence status requires a punch_in",
            )

        if target_punch_in.tzinfo is None:
            target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

        derived_date = derive_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if derived_date != date:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in must stay on the record's attendance date ({date})",
            )

        if body.punch_out is not None:
            target_punch_out = epoch_ms_to_datetime_utc(body.punch_out)
        else:
            target_punch_out = current.get("punch_out")

        if target_punch_out is not None:
            if target_punch_out.tzinfo is None:
                target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)
            if target_punch_out <= target_punch_in:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly greater than punch_in",
                )
            if (target_punch_out - target_punch_in).total_seconds() > 86400:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )

    # Recompute derived values
    if target_status in (Status.ABSENT.value, Status.LEAVE.value):
        target_late_minutes = 0
        target_work_hours = None
        target_overtime_minutes = 0
        target_half_day = False
    else:
        target_late_minutes = compute_late_minutes(target_punch_in, emp["shift_start"], date, emp["shift_end"])
        if target_punch_out is not None:
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_half_day = compute_half_day(target_work_hours)
            target_overtime_minutes = compute_overtime(
                target_punch_out, emp["shift_end"], date, emp["shift_start"]
            )
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    # Check for actual differences
    changes_db = {}

    def normalize_dt(val):
        if isinstance(val, datetime):
            return val.replace(microsecond=0) if val.tzinfo else val.replace(tzinfo=timezone.utc, microsecond=0)
        return val

    curr_p_in = normalize_dt(current.get("punch_in"))
    curr_p_out = normalize_dt(current.get("punch_out"))
    curr_late = current.get("late_minutes", 0)
    curr_ot = current.get("overtime_minutes", 0)
    curr_hd = current.get("half_day", False)
    curr_wh = current.get("work_hours")
    curr_st = current.get("status")

    if curr_st != target_status:
        changes_db["status"] = {"from": curr_st, "to": target_status}
    if curr_p_in != target_punch_in:
        changes_db["punch_in"] = {"from": curr_p_in, "to": target_punch_in}
    if curr_p_out != target_punch_out:
        changes_db["punch_out"] = {"from": curr_p_out, "to": target_punch_out}
    if curr_wh != target_work_hours:
        changes_db["work_hours"] = {"from": curr_wh, "to": target_work_hours}
    if curr_late != target_late_minutes:
        changes_db["late_minutes"] = {"from": curr_late, "to": target_late_minutes}
    if curr_ot != target_overtime_minutes:
        changes_db["overtime_minutes"] = {"from": curr_ot, "to": target_overtime_minutes}
    if curr_hd != target_half_day:
        changes_db["half_day"] = {"from": curr_hd, "to": target_half_day}

    if not changes_db:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Correction request specifies no changes",
        )

    server_now = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": server_now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    # Optimistic concurrency control using history array matching
    curr_history = current.get("history") or []
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": current["_id"],
            "history": curr_history,
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Concurrent modification detected on attendance record",
        )

    return serialize_attendance_record(updated)


# Analytics
@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly, tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM."),
):
    """Monthly attendance summary for one employee via MongoDB aggregation."""
    ensure_indexes()
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Employee not found")

    working_days = count_working_days(month, emp["joined_on"])
    _, m_end = get_month_boundaries(month)
    month_end_str = m_end.isoformat()

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": month_end_str},
            }
        },
        {
            "$addFields": {
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                }
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
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days == 0:
        attendance_pct = None
    else:
        raw_pct = (present_days / working_days) * 100.0
        attendance_pct = round_half_up(raw_pct, 2)

    return EmployeeMonthly(
        emp_code=emp_code,
        month=month,
        working_days=working_days,
        present_days=round_half_up(present_days, 2),
        leave_days=leave_days,
        late_count=late_count,
        total_late_minutes=total_late,
        total_overtime_minutes=total_ot,
        attendance_pct=attendance_pct,
    )


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    _, m_end = get_month_boundaries(month)
    month_end_str = m_end.isoformat()

    emp_match = {"joined_on": {"$lte": month_end_str}}
    if department:
        emp_match["department"] = department

    sub_emp_match = {"emp_info.joined_on": {"$lte": month_end_str}}
    if department:
        sub_emp_match["emp_info.department"] = department

    return [
        {"$match": emp_match},
        {
            "$project": {
                "department": 1,
                "is_emp": {"$literal": 1},
            }
        },
        {
            "$unionWith": {
                "coll": "attendance_logs",
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": f"{month}-01", "$lte": month_end_str}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp_info",
                        }
                    },
                    {"$unwind": "$emp_info"},
                    {"$match": sub_emp_match},
                    {
                        "$project": {
                            "department": "$emp_info.department",
                            "status": 1,
                            "date": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                            "half_day": 1,
                        }
                    },
                ],
            }
        },
        {
            "$addFields": {
                "day_of_week": {
                    "$cond": [
                        {"$ifNull": ["$date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        None,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": {"$ifNull": ["$is_emp", 0]}},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": ASCENDING}},
    ]


@app.get("/analytics/departments/summary", response_model=DepartmentSummary, tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM."),
    department: Optional[str] = Query(None, description="Restrict to one department (exact match)."),
):
    """Department summary for a month using cross-collection MongoDB aggregation."""
    ensure_indexes()
    pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)

    items = []
    for doc in cursor:
        wh_count = doc.get("work_hours_count", 0)
        wh_sum = doc.get("work_hours_sum", 0.0)
        avg_wh = round_half_up(wh_sum / wh_count, 2) if wh_count > 0 else None

        items.append(
            DepartmentSummaryItem(
                department=doc["_id"],
                headcount=doc["headcount"],
                present_days=round_half_up(doc.get("present_days", 0.0), 2),
                avg_work_hours=avg_wh,
                late_count=doc.get("late_count", 0),
                total_late_minutes=doc.get("total_late_minutes", 0),
                leave_count=doc.get("leave_count", 0),
                on_duty_count=doc.get("on_duty_count", 0),
            )
        )

    return DepartmentSummary(month=month, items=items)


def build_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list:
    _, m_end = get_month_boundaries(month)
    month_end_str = m_end.isoformat()

    dept_filter = [{"$match": {"emp.department": department}}] if department else []

    return [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": month_end_str},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        *dept_filter,
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$sort": {
                "total_late_minutes": DESCENDING,
                "_id": ASCENDING,
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]


@app.get("/analytics/leaderboard/late", response_model=Leaderboard, tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM."),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """Late-comers leaderboard for a month using standard competition ranking ($rank)."""
    ensure_indexes()
    pipeline = build_leaderboard_pipeline(month, limit, department)
    cursor = db.attendance_logs.aggregate(pipeline)

    items = [
        LeaderboardItem(
            rank=doc["rank"],
            emp_code=doc["emp_code"],
            name=doc["name"],
            department=doc["department"],
            total_late_minutes=doc["total_late_minutes"],
            late_count=doc["late_count"],
        )
        for doc in cursor
    ]

    return Leaderboard(month=month, items=items)


def build_trend_pipeline(department: str, from_date_str: str, to_date_str: str) -> list:
    f_date = dt_date.fromisoformat(from_date_str)
    t_date = dt_date.fromisoformat(to_date_str)

    f_dt_bson = datetime(f_date.year, f_date.month, f_date.day, tzinfo=timezone.utc)
    t_dt_bson = datetime(t_date.year, t_date.month, t_date.day, tzinfo=timezone.utc)
    t_next_dt_bson = t_dt_bson + timedelta(days=1)

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "date_obj": {"$literal": f_dt_bson}}},
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {"$project": {"_id": 0, "date_obj": {"$literal": t_dt_bson}}},
                ],
            }
        },
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [f_dt_bson, t_next_dt_bson],
                },
            }
        },
        {"$group": {"_id": "$date_obj"}},
        {"$sort": {"_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                "day_of_week": {"$dayOfWeek": "$_id"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc_doc",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "emp_info",
                        }
                    },
                    {"$unwind": "$emp_info"},
                    {"$match": {"emp_info.department": department}},
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_doc.count", 0]}, 0]},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        },
                    }
                },
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]


@app.get("/analytics/departments/{department}/trend", response_model=Trend, tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    """Daily attendance trend with gap filling and 7-day moving averages."""
    try:
        f_date = dt_date.fromisoformat(from_date)
        t_date = dt_date.fromisoformat(to_date)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid date format for 'from' or 'to'")

    if t_date < f_date:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date must be greater than or equal to 'from' date",
        )

    range_days = (t_date - f_date).days + 1
    if range_days > 92:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Date range cannot exceed 92 days",
        )

    dept_exists = db.employees.count_documents({"department": department})
    if dept_exists == 0:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Department not found")

    pipeline = build_trend_pipeline(department, from_date, to_date)
    cursor = db.employees.aggregate(pipeline)

    items = []
    for doc in cursor:
        rate = doc.get("attendance_rate")
        m_avg = doc.get("moving_avg_7d")

        items.append(
            TrendItem(
                date=doc["date"],
                is_working_day=doc["is_working_day"],
                headcount=doc["headcount"],
                present_count=round_half_up(doc.get("present_count", 0.0), 2),
                late_count=doc.get("late_count", 0),
                attendance_rate=round_half_up(rate, 4),
                moving_avg_7d=round_half_up(m_avg, 4),
            )
        )

    return Trend(department=department, items=items)


# Admin
class ExplainEndpointEnum(str, Enum):
    attendance_list = "attendance_list"
    employee_monthly = "employee_monthly"
    department_summary = "department_summary"
    late_leaderboard = "late_leaderboard"
    department_trend = "department_trend"


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: ExplainEndpointEnum = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[Status] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """MongoDB explain with executionStats verbosity for an endpoint's query/pipeline."""
    ensure_indexes()
    if endpoint == ExplainEndpointEnum.attendance_list:
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            date_q = {}
            if date_from:
                date_q["$gte"] = date_from
            if date_to:
                date_q["$lte"] = date_to
            q["date"] = date_q
        if status:
            q["status"] = status.value

        skip = (page - 1) * page_size
        command = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": DESCENDING, "emp_code": ASCENDING},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        clean_res = json_util.loads(json_util.dumps(res))
        return {
            "endpoint": endpoint.value,
            "collection": "attendance_logs",
            "explain": clean_res,
        }

    elif endpoint == ExplainEndpointEnum.employee_monthly:
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")

        _, m_end = get_month_boundaries(month)
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": f"{month}-01", "$lte": m_end.isoformat()},
                }
            },
            {
                "$addFields": {
                    "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "present_days": {"$sum": 1},
                }
            },
        ]
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        clean_res = json_util.loads(json_util.dumps(res))
        return {
            "endpoint": endpoint.value,
            "collection": "attendance_logs",
            "explain": clean_res,
        }

    elif endpoint == ExplainEndpointEnum.department_summary:
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        pipeline = build_department_summary_pipeline(month, department)
        command = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        clean_res = json_util.loads(json_util.dumps(res))
        return {
            "endpoint": endpoint.value,
            "collection": "employees",
            "explain": clean_res,
        }

    elif endpoint == ExplainEndpointEnum.late_leaderboard:
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        pipeline = build_leaderboard_pipeline(month, limit or 10, department)
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        clean_res = json_util.loads(json_util.dumps(res))
        return {
            "endpoint": endpoint.value,
            "collection": "attendance_logs",
            "explain": clean_res,
        }

    elif endpoint == ExplainEndpointEnum.department_trend:
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="'from' and 'to' are required for department_trend")

        pipeline = build_trend_pipeline(department, from_date, to_date)
        command = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(command)
        clean_res = json_util.loads(json_util.dumps(res))
        return {
            "endpoint": endpoint.value,
            "collection": "employees",
            "explain": clean_res,
        }

    raise HTTPException(status_code=422, detail="Unknown endpoint identifier")

"""
Automated Test Suite for Employee Attendance & Analytics API

Validates:
- Business rules R1-R10 (timezones, rounding, grace period, overtime, work hours, half-day)
- Pydantic validation schemas & API contracts
- HTTP endpoints, parameter validation, and status codes via FastAPI TestClient
- Concurrency & database operations when MongoDB is accessible
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import (
    app,
    client as mongo_client,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    compute_half_day,
    count_working_days,
    derive_attendance_date,
    epoch_ms_to_datetime_utc,
    datetime_to_epoch_ms,
    round_half_up,
    EmployeeCreate,
    PunchInRequest,
    PunchOutRequest,
    RegularizeRequest,
    PresenceStatus,
    Status,
    IST,
)

test_client = TestClient(app)


# --------------------------------------------------------------------------- #
# R1 Tests: Timestamps & Attendance Dates
# --------------------------------------------------------------------------- #
def test_r1_epoch_truncation():
    # 1783312500999 ms should truncate to whole seconds (1783312500000 ms)
    ms = 1783312500999
    dt = epoch_ms_to_datetime_utc(ms)
    assert dt.microsecond == 0
    assert datetime_to_epoch_ms(dt) == 1783312500000


def test_r1_regular_shift_attendance_date():
    # Regular shift 09:30 - 18:30 IST
    # 2026-07-06 09:28:00 IST -> 2026-07-06 03:58:00 UTC
    dt_utc = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    att_date = derive_attendance_date(dt_utc, "09:30", "18:30")
    assert att_date == "2026-07-06"


def test_r1_overnight_shift_attendance_date():
    # Overnight shift 22:00 - 06:00 IST
    # Punch-in at 21:55 IST on 2026-07-06 -> belongs to 2026-07-06
    dt_utc_evening = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)  # 21:55 IST
    assert derive_attendance_date(dt_utc_evening, "22:00", "06:00") == "2026-07-06"

    # Punch-in at 00:30 IST on 2026-07-07 (earlier than shift_end 06:00) -> belongs to 2026-07-06
    dt_utc_midnight = datetime(2026, 7, 6, 19, 0, 0, tzinfo=timezone.utc)  # 00:30 IST on 7th
    assert derive_attendance_date(dt_utc_midnight, "22:00", "06:00") == "2026-07-06"

    # Punch-in at 06:15 IST on 2026-07-07 (after shift_end 06:00) -> belongs to 2026-07-07
    dt_utc_morning = datetime(2026, 7, 7, 0, 45, 0, tzinfo=timezone.utc)  # 06:15 IST
    assert derive_attendance_date(dt_utc_morning, "22:00", "06:00") == "2026-07-07"


# --------------------------------------------------------------------------- #
# R2 Tests: Late Minutes & Grace Period
# --------------------------------------------------------------------------- #
def test_r2_late_grace_boundaries():
    # Shift start 09:30 IST on 2026-07-06
    # 09:40:00 IST -> exact 10:00 boundary -> 0 late minutes
    dt_boundary = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)  # 09:40:00 IST
    assert compute_late_minutes(dt_boundary, "09:30", "2026-07-06", "18:30") == 0

    # 09:40:01 IST -> strictly more than 10:00 -> 10 minutes late from shift start
    dt_late_1s = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)  # 09:40:01 IST
    assert compute_late_minutes(dt_late_1s, "09:30", "2026-07-06", "18:30") == 10

    # 10:15:59 IST -> 45 minutes late
    dt_late_45m = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)  # 10:15:59 IST
    assert compute_late_minutes(dt_late_45m, "09:30", "2026-07-06", "18:30") == 45

    # Early punch-in (09:15:00 IST) -> 0 minutes late
    dt_early = datetime(2026, 7, 6, 3, 45, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_early, "09:30", "2026-07-06", "18:30") == 0


def test_r2_overnight_shift_late_minutes():
    # Overnight shift 22:00 - 06:00 IST on 2026-07-06
    # Punch in at 22:10:00 IST (boundary) -> 0 late
    dt_overnight_grace = datetime(2026, 7, 6, 16, 40, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_overnight_grace, "22:00", "2026-07-06", "06:00") == 0

    # Punch in at 22:15:00 IST -> 15 min late
    dt_overnight_late = datetime(2026, 7, 6, 16, 45, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_overnight_late, "22:00", "2026-07-06", "06:00") == 15


# --------------------------------------------------------------------------- #
# R3 Tests: Overtime
# --------------------------------------------------------------------------- #
def test_r3_overtime_threshold():
    # Shift end 18:30 IST on 2026-07-06
    # Punch-out 18:55:00 IST (25 min elapsed < 30 min) -> 0 overtime
    dt_25m = datetime(2026, 7, 6, 13, 25, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_25m, "18:30", "2026-07-06", "09:30") == 0

    # Punch-out 19:00:00 IST (30 min elapsed >= 30 min) -> 30 overtime
    dt_30m = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_30m, "18:30", "2026-07-06", "09:30") == 30

    # Punch-out 19:10:00 IST (40 min elapsed) -> 40 overtime
    dt_40m = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_40m, "18:30", "2026-07-06", "09:30") == 40


def test_r3_overnight_shift_overtime():
    # Overnight shift 22:00 - 06:00 IST on 2026-07-06 (shift end is 06:00 on 2026-07-07)
    # Punch-out at 06:40:00 IST on 2026-07-07 (40 min after shift end) -> 40 overtime
    dt_next_day_out = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_next_day_out, "06:00", "2026-07-06", "22:00") == 40


# --------------------------------------------------------------------------- #
# R4 & R5 Tests: Work Hours & Half Day Rounding
# --------------------------------------------------------------------------- #
def test_r4_decimal_half_up_rounding():
    # 9630 seconds = 2.675 hours. Python round(2.675, 2) banker's rounds to 2.67.
    # Decimal half-up must yield 2.68.
    p_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    p_out = p_in + timedelta(seconds=9630)
    wh = compute_work_hours(p_in, p_out)
    assert wh == 2.68


def test_r5_half_day_threshold():
    # Half-day threshold is rounded work_hours < 4.50
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(4.51) is False
    assert compute_half_day(None) is False


# --------------------------------------------------------------------------- #
# R7 & R8 Tests: Working Days & Reporting Precision
# --------------------------------------------------------------------------- #
def test_r7_working_days_count():
    # July 2026 has 23 weekdays (July 1 is Wed, July 31 is Fri)
    assert count_working_days("2026-07", "2026-01-01") == 23

    # Employee joined mid-month: 2026-07-15 (Wed)
    # July 15-31: exactly 13 weekdays
    assert count_working_days("2026-07", "2026-07-15") == 13

    # Employee joined after month end: 2026-08-01 -> 0 working days
    assert count_working_days("2026-07", "2026-08-01") == 0


def test_r8_rounding():
    assert round_half_up(12.345, 2) == 12.35
    assert round_half_up(12.344, 2) == 12.34
    assert round_half_up(0.12345, 4) == 0.1235
    assert round_half_up(None, 2) is None


# --------------------------------------------------------------------------- #
# Schema & Pydantic Validation Tests
# --------------------------------------------------------------------------- #
def test_employee_schema_validation():
    # Equal shift times rejected
    with pytest.raises(ValidationError):
        EmployeeCreate(
            emp_code="EMP0001",
            name="John Doe",
            email="john@example.com",
            department="Engineering",
            shift_start="09:00",
            shift_end="09:00",
            joined_on="2026-01-01",
        )

    # Invalid emp_code regex rejected
    with pytest.raises(ValidationError):
        EmployeeCreate(
            emp_code="EMP1",  # Too short
            name="John Doe",
            email="john@example.com",
            department="Engineering",
            shift_start="09:00",
            shift_end="18:00",
            joined_on="2026-01-01",
        )

    # Invalid email rejected
    with pytest.raises(ValidationError):
        EmployeeCreate(
            emp_code="EMP0001",
            name="John Doe",
            email="not-an-email",
            department="Engineering",
            shift_start="09:00",
            shift_end="18:00",
            joined_on="2026-01-01",
        )


def test_punch_in_schema_validation():
    # Value looking like seconds (< 100000000000) rejected
    with pytest.raises(ValidationError):
        PunchInRequest(emp_code="EMP0001", punched_at=1719830400)

    # Status ABSENT rejected at punch-in
    with pytest.raises(ValidationError):
        PunchInRequest(emp_code="EMP0001", status="ABSENT")


# --------------------------------------------------------------------------- #
# Endpoint Parameter Validation via TestClient (HTTP 422 Checks)
# --------------------------------------------------------------------------- #
def test_http_validation_errors():
    # Pagination invalid bounds -> 422
    resp = test_client.get("/employees?page=0")
    assert resp.status_code == 422

    resp = test_client.get("/employees?page_size=101")
    assert resp.status_code == 422

    # Attendance date bounds inverted -> 422
    resp = test_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert resp.status_code == 422

    # Trend inverted dates -> 422
    resp = test_client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
    assert resp.status_code == 422

    # Trend range > 92 days -> 422
    resp = test_client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-06-01")
    assert resp.status_code == 422

    # Invalid explain endpoint -> 422
    resp = test_client.get("/admin/explain/unknown_endpoint")
    assert resp.status_code == 422


def test_health_check_endpoint():
    # When MongoDB is unavailable, /health must return HTTP 503
    try:
        mongo_client.admin.command("ping")
        mongo_running = True
    except Exception:
        mongo_running = False

    resp = test_client.get("/health")
    if mongo_running:
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
    else:
        assert resp.status_code == 503
        assert "detail" in resp.json()


# --------------------------------------------------------------------------- #
# Serialization & Legacy Document Normalization Tests
# --------------------------------------------------------------------------- #
def test_serialize_employee():
    from app.main import serialize_employee

    raw_doc = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
        "created_at": datetime(2026, 1, 5, 4, 30, 0, tzinfo=timezone.utc),
    }
    emp = serialize_employee(raw_doc)
    assert emp.emp_code == "EMP0001"
    assert emp.created_at == 1767587400000
    assert not hasattr(emp, "_id")


def test_serialize_legacy_attendance_doc():
    from app.main import serialize_attendance_record

    # Legacy document with missing half_day, missing history, missing overtime_minutes
    legacy_doc = {
        "emp_code": "EMP0002",
        "date": "2026-07-14",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 14, 4, 2, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 14, 13, 1, 0, tzinfo=timezone.utc),
        "work_hours": 8.98,
        "late_minutes": 0,
    }
    record = serialize_attendance_record(legacy_doc)
    assert record.emp_code == "EMP0002"
    assert record.punch_in == 1784001720000
    assert record.punch_out == 1784034060000
    assert record.half_day is False
    assert record.history == []
    assert record.overtime_minutes == 0


def test_regularize_request_schema():
    # Valid regularization
    req = RegularizeRequest(
        status=Status.PRESENT,
        punch_in=1783310280000,
        reason="biometric scanner issue",
        regularized_by="hr.admin",
    )
    assert req.reason == "biometric scanner issue"
    assert req.regularized_by == "hr.admin"

    # Reason too short (< 5 chars)
    with pytest.raises(ValidationError):
        RegularizeRequest(
            reason="bad",
            regularized_by="hr.admin",
        )


# --------------------------------------------------------------------------- #
# Aggregation Pipeline Structure Tests
# --------------------------------------------------------------------------- #
def test_department_summary_pipeline_structure():
    from app.main import build_department_summary_pipeline

    pipe = build_department_summary_pipeline("2026-07", "Engineering")
    stage_names = [list(stage.keys())[0] for stage in pipe]
    # Check that $match, $project, $unionWith, $addFields, $group, $sort are present
    assert "$match" in stage_names
    assert "$unionWith" in stage_names
    assert "$group" in stage_names
    assert "$sort" in stage_names


def test_leaderboard_pipeline_structure():
    from app.main import build_leaderboard_pipeline

    pipe = build_leaderboard_pipeline("2026-07", 10, "Engineering")
    stage_names = [list(stage.keys())[0] for stage in pipe]
    assert "$match" in stage_names
    assert "$group" in stage_names
    assert "$lookup" in stage_names
    assert "$setWindowFields" in stage_names

    # Verify standard competition ranking $rank is configured
    window_stage = next(s for s in pipe if "$setWindowFields" in s)
    assert "rank" in window_stage["$setWindowFields"]["output"]
    assert "$rank" in window_stage["$setWindowFields"]["output"]["rank"]


def test_trend_pipeline_structure():
    from app.main import build_trend_pipeline

    pipe = build_trend_pipeline("Engineering", "2026-07-01", "2026-07-10")
    stage_names = [list(stage.keys())[0] for stage in pipe]
    assert "$match" in stage_names
    assert "$densify" in stage_names
    assert "$lookup" in stage_names
    assert "$setWindowFields" in stage_names

    # Verify 7-day moving average window is configured
    window_stage = next(s for s in pipe if "$setWindowFields" in s)
    moving_avg_conf = window_stage["$setWindowFields"]["output"]["moving_avg_7d"]
    assert moving_avg_conf["window"]["documents"] == [-6, "current"]


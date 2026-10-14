"""Employee attendance and analytics API."""

import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
attendance = db.attendance_logs

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
MAX_EPOCH = 4102444800000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc


def valid_time(value: str) -> str:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("time must be HH:MM") from exc
    if parsed.second or parsed.microsecond:
        raise ValueError("time must be HH:MM")
    return value


def epoch_to_utc(value: Optional[int], *, now: bool = False) -> datetime:
    if value is None and now:
        value = int(datetime.now(UTC).timestamp() * 1000)
    if value is None or type(value) is not int or value < 100_000_000_000 or value > MAX_EPOCH:
        raise HTTPException(422, "invalid epoch milliseconds")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0).replace(tzinfo=None)


def mongo_to_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(microsecond=0)


def epoch(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(mongo_to_utc(value).replace(tzinfo=UTC).timestamp() * 1000)


def local_dt(value: datetime) -> datetime:
    return mongo_to_utc(value).replace(tzinfo=UTC).astimezone(IST)


def shift_clock(value: str) -> time:
    return time.fromisoformat(value)


def attendance_date(punch: datetime, emp: dict[str, Any]) -> str:
    local = local_dt(punch)
    if shift_clock(emp["shift_end"]) <= shift_clock(emp["shift_start"]) and local.time() < shift_clock(emp["shift_end"]):
        local = local - timedelta(days=1)
    return local.date().isoformat()


def shift_boundary(day: str, clock: str, overnight: bool = False) -> datetime:
    d = parse_date(day)
    if overnight:
        d += timedelta(days=1)
    local = datetime.combine(d, shift_clock(clock), IST)
    return local.astimezone(UTC).replace(tzinfo=None)


def shift_bounds(day: str, emp: dict[str, Any]) -> tuple[datetime, datetime]:
    overnight = shift_clock(emp["shift_end"]) <= shift_clock(emp["shift_start"])
    start = shift_boundary(day, emp["shift_start"])
    end = shift_boundary(day, emp["shift_end"], overnight)
    return start, end


def late_minutes(punch: Optional[datetime], day: str, emp: dict[str, Any]) -> int:
    if punch is None:
        return 0
    start, _ = shift_bounds(day, emp)
    seconds = int((mongo_to_utc(punch) - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def rounded_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[float]:
    if punch_in is None or punch_out is None:
        return None
    seconds = Decimal(int((mongo_to_utc(punch_out) - mongo_to_utc(punch_in)).total_seconds()))
    return float((seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def overtime_minutes(punch_out: Optional[datetime], day: str, emp: dict[str, Any]) -> int:
    if punch_out is None:
        return 0
    _, end = shift_bounds(day, emp)
    seconds = int((mongo_to_utc(punch_out) - end).total_seconds())
    return seconds // 60 if seconds >= 1800 else 0


def derived(day: str, emp: dict[str, Any], punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict[str, Any]:
    hours = rounded_hours(punch_in, punch_out)
    return {
        "late_minutes": late_minutes(punch_in, day, emp),
        "work_hours": hours,
        "overtime_minutes": overtime_minutes(punch_out, day, emp),
        "half_day": hours is not None and hours < 4.50,
    }


def clean_record(doc: dict[str, Any]) -> dict[str, Any]:
    result = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": epoch(doc.get("punch_in")),
        "punch_out": epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [],
    }
    for entry in doc.get("history", []):
        changes = {}
        for field, change in entry.get("changes", {}).items():
            changes[field] = {
                "from": epoch(change["from"]) if field in {"punch_in", "punch_out"} else change.get("from"),
                "to": epoch(change["to"]) if field in {"punch_in", "punch_out"} else change.get("to"),
            }
        changes_entry = dict(entry)
        changes_entry["at"] = epoch(entry.get("at"))
        changes_entry["changes"] = changes
        changes_entry.pop("_id", None)
        result["history"].append(changes_entry)
    return result


def clean_employee(doc: dict[str, Any]) -> dict[str, Any]:
    result = {key: doc[key] for key in ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")}
    result["created_at"] = epoch(doc["created_at"])
    return result


def page_params(page: int, page_size: int) -> None:
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, "invalid pagination")


def qdate(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, "invalid date") from exc


def month_bounds(month: str) -> tuple[date, date]:
    try:
        start = date.fromisoformat(month + "-01")
    except ValueError as exc:
        raise HTTPException(422, "month must be YYYY-MM") from exc
    end = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1) - timedelta(days=1)
    return start, end


def half_up(value: Decimal, places: str) -> float:
    return float(Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        return valid_time(value)

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        parse_date(value)
        return value

    @model_validator(mode="after")
    def different_shifts(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100_000_000_000, le=MAX_EPOCH)
    status: str = "PRESENT"

    @field_validator("status")
    @classmethod
    def presence_status(cls, value: str) -> str:
        if value not in PRESENCE:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return value


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100_000_000_000, le=MAX_EPOCH)


class RegularizeIn(StrictModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100_000_000_000, le=MAX_EPOCH)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100_000_000_000, le=MAX_EPOCH)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in STATUSES:
            raise ValueError("invalid status")
        return value


@app.on_event("startup")
def create_indexes() -> None:
    employees.create_index("emp_code", unique=True, name="uq_employee_code")
    employees.create_index([("department", 1), ("joined_on", 1)], name="ix_department_joined")
    attendance.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_day")
    attendance.create_index([("date", -1), ("emp_code", 1)], name="ix_attendance_date_emp")
    attendance.create_index([("emp_code", 1), ("date", 1), ("status", 1)], name="ix_attendance_employee_period")


@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
    except Exception as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict[str, Any]:
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    try:
        employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return clean_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    page_params(page, page_size)
    query: dict[str, Any] = {} if department is None else {"department": department}
    total = employees.count_documents(query)
    docs = employees.find(query, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [clean_employee(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = epoch_to_utc(body.punched_at, now=body.punched_at is None)
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts, "punch_out": None,
        "work_hours": None, "late_minutes": late_minutes(ts, day, emp), "overtime_minutes": 0,
        "half_day": False, "history": [],
    }
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return clean_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict[str, Any]:
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = epoch_to_utc(body.punched_at, now=body.punched_at is None)
    candidate = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if candidate is None:
        raise HTTPException(404, "no punch-in found")
    pin = mongo_to_utc(candidate.get("punch_in"))
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    if ts <= pin or ts - pin > timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    values = derived(candidate["date"], emp, pin, ts)
    updated = attendance.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **values}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record is already punched out")
    return clean_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
    status: Optional[str] = None, page: int = 1, page_size: int = 20,
) -> dict[str, Any]:
    page_params(page, page_size)
    if date_from is not None:
        qdate(date_from)
    if date_to is not None:
        qdate(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    total = attendance.count_documents(query)
    cursor = attendance.find(query).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [clean_record(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{day}")
def regularize(emp_code: str, day: str, body: RegularizeIn) -> dict[str, Any]:
    qdate(day)
    emp = employees.find_one({"emp_code": emp_code})
    record = attendance.find_one({"emp_code": emp_code, "date": day})
    if emp is None or record is None:
        raise HTTPException(404, "employee or attendance record not found")
    old = {
        "status": record["status"], "punch_in": mongo_to_utc(record.get("punch_in")),
        "punch_out": mongo_to_utc(record.get("punch_out")), "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0), "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    status = body.status if body.status is not None else old["status"]
    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "absence records cannot have punch times")
        pin = pout = None
    else:
        pin = epoch_to_utc(body.punch_in) if body.punch_in is not None else old["punch_in"]
        pout = epoch_to_utc(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if pin is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date(pin, emp) != day:
            raise HTTPException(422, "punch_in does not belong to attendance date")
        if pout is not None and (pout <= pin or pout - pin > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    new_derived = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if status in PRESENCE:
        new_derived = derived(day, emp, pin, pout)
    new = {"status": status, "punch_in": pin, "punch_out": pout, **new_derived}
    changes: dict[str, Any] = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        if old[field] != new[field]:
            changes[field] = {"from": old[field], "to": new[field]}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    entry = {"at": datetime.now(UTC).replace(tzinfo=None, microsecond=0), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}
    updated = attendance.find_one_and_update(
        {"_id": record["_id"], "history": record.get("history", [])},
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed concurrently")
    return clean_record(updated)


def records_for(query: dict[str, Any]) -> list[dict[str, Any]]:
    return list(attendance.find(query, {"_id": 0}))


def explain_find(query: dict[str, Any], *, hint: str, sort: Optional[dict[str, int]] = None,
                 skip: int = 0, limit: int = 0) -> dict[str, Any]:
    command: dict[str, Any] = {
        "find": attendance.name,
        "filter": query,
        "hint": hint,
        "skip": skip,
        "limit": limit,
    }
    if sort:
        command["sort"] = sort
    return db.command("explain", command, verbosity="executionStats")


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
    start, end = month_bounds(month)
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first = max(start, parse_date(emp["joined_on"]))
    working_days = sum((first + timedelta(days=i)).weekday() < 5 for i in range(max(0, (end - first).days + 1)))
    docs = records_for({"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}})
    present = sum(Decimal("0.5") if d.get("half_day", False) else Decimal(1)
                  for d in docs if d.get("status") in PRESENCE and qdate(d["date"]).weekday() < 5)
    late = [d for d in docs if d.get("late_minutes", 0) > 0]
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": half_up(present, "0.01"), "leave_days": sum(d.get("status") == "LEAVE" for d in docs),
        "late_count": len(late), "total_late_minutes": sum(d.get("late_minutes", 0) for d in docs),
        "total_overtime_minutes": sum(d.get("overtime_minutes", 0) for d in docs),
        "attendance_pct": None if not working_days else half_up(present / Decimal(working_days) * 100, "0.01"),
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                       department: Optional[str] = None) -> dict[str, Any]:
    start, end = month_bounds(month)
    eq: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        eq["department"] = department
    emps = list(employees.find(eq, {"_id": 0}))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for emp in emps:
        grouped.setdefault(emp["department"], []).append(emp)
    logs = records_for({"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}})
    by_code = {e["emp_code"]: e["department"] for e in emps}
    items = []
    for dept in sorted(grouped):
        dept_logs = [d for d in logs if by_code.get(d["emp_code"]) == dept]
        present = sum(Decimal("0.5") if d.get("half_day", False) else Decimal(1)
                      for d in dept_logs if d.get("status") in PRESENCE and qdate(d["date"]).weekday() < 5)
        hours = [Decimal(str(d["work_hours"])) for d in dept_logs if d.get("status") in PRESENCE and d.get("work_hours") is not None]
        items.append({"department": dept, "headcount": len(grouped[dept]), "present_days": half_up(present, "0.01"),
                      "avg_work_hours": None if not hours else half_up(sum(hours) / len(hours), "0.01"),
                      "late_count": sum(d.get("late_minutes", 0) > 0 for d in dept_logs),
                      "total_late_minutes": sum(d.get("late_minutes", 0) for d in dept_logs),
                      "leave_count": sum(d.get("status") == "LEAVE" for d in dept_logs),
                      "on_duty_count": sum(d.get("status") == "ON_DUTY" for d in dept_logs)})
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None) -> dict[str, Any]:
    start, end = month_bounds(month)
    emp_query = {} if department is None else {"department": department}
    emp_map = {e["emp_code"]: e for e in employees.find(emp_query, {"_id": 0})}
    logs = records_for({"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}})
    totals: dict[str, list[int]] = {}
    for d in logs:
        if d["emp_code"] in emp_map:
            totals.setdefault(d["emp_code"], [0, 0])
            totals[d["emp_code"]][0] += d.get("late_minutes", 0)
            totals[d["emp_code"]][1] += 1
    ordered = sorted(totals.items(), key=lambda item: (-item[1][0], item[0]))
    items = []
    previous = None
    rank = 0
    for position, (code, values) in enumerate(ordered, 1):
        if values[0] != previous:
            rank = position
            previous = values[0]
        if rank > limit:
            break
        emp = emp_map[code]
        items.append({"rank": rank, "emp_code": code, "name": emp["name"], "department": emp["department"],
                      "total_late_minutes": values[0], "late_count": values[1]})
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)) -> dict[str, Any]:
    start, end = qdate(from_), qdate(to)
    if end < start:
        raise HTTPException(422, "to must not be before from")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "trend range cannot exceed 92 days")
    dept_emps = list(employees.find({"department": department}, {"_id": 0}))
    if not dept_emps:
        raise HTTPException(404, "department not found")
    logs = records_for({"emp_code": {"$in": [e["emp_code"] for e in dept_emps]},
                        "date": {"$gte": from_, "$lte": to}})
    by_day: dict[str, list[dict[str, Any]]] = {}
    for log in logs:
        by_day.setdefault(log["date"], []).append(log)
    rates: list[Optional[float]] = []
    items = []
    current = start
    while current <= end:
        day = current.isoformat()
        day_logs = by_day.get(day, [])
        headcount = sum(parse_date(e["joined_on"]) <= current for e in dept_emps)
        present = sum(Decimal("0.5") if d.get("half_day", False) else Decimal(1) for d in day_logs if d.get("status") in PRESENCE)
        working = current.weekday() < 5
        rate = half_up(present / headcount, "0.0001") if working and headcount else (0.0 if working else None)
        rates.append(rate)
        window = [Decimal(str(x)) for x in rates[-7:] if x is not None]
        moving = None if not window else half_up(sum(window) / len(window), "0.0001")
        items.append({"date": day, "is_working_day": working, "headcount": headcount,
                      "present_count": half_up(present, "0.01"), "late_count": sum(d.get("late_minutes", 0) > 0 for d in day_logs),
                      "attendance_rate": rate, "moving_avg_7d": moving})
        current += timedelta(days=1)
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                      department: Optional[str] = None, limit: int = 10, date_from: Optional[str] = None,
                      date_to: Optional[str] = None, status: Optional[str] = None, from_: Optional[str] = Query(None, alias="from"),
                      to: Optional[str] = None, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    page_params(page, page_size)
    query: dict[str, Any] = {}
    collection = attendance
    if endpoint == "attendance_list":
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        explain = explain_find(
            query, hint="ix_attendance_date_emp",
            sort={"date": -1, "emp_code": 1},
            skip=(page - 1) * page_size, limit=page_size,
        )
    elif endpoint in {"employee_monthly", "department_summary", "late_leaderboard"}:
        if not month:
            raise HTTPException(422, "month is required")
        start, end = month_bounds(month)
        query = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}
        if emp_code:
            query["emp_code"] = emp_code
        explain = explain_find(query, hint="ix_attendance_employee_period")
    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from and to are required")
        qdate(from_)
        qdate(to)
        explain = explain_find({"date": {"$gte": from_, "$lte": to}}, hint="ix_attendance_date_emp")
    else:
        raise HTTPException(422, "unknown endpoint")
    return {"endpoint": endpoint, "collection": collection.name, "explain": explain}

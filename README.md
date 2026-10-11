# Employee Attendance & Analytics API

A high-performance FastAPI and MongoDB backend service implementing employee attendance tracking, automated timecard processing, and department analytics according to the HROne Engineering Candidate Kit specification.

---

## Architecture & Features

- **FastAPI + PyMongo**: High-throughput REST API with strict Pydantic v2 data validation adhering strictly to `openapi.yaml`.
- **Atomic Concurrency**: Natural-key compound unique indexes preventing duplicate punch-in and employee race conditions with deterministic 409 responses. Conditional atomic updates (`find_one_and_update`) preventing punch-out races and regularized history loss.
- **Accurate Business Rules (R1–R10)**: Decimal half-up rounding (`ROUND_HALF_UP`), overnight shift boundary handling, 10-minute grace period threshold, and 30-minute overtime qualifying cutoffs.
- **Pure Database Aggregations**: All monthly metrics, cross-collection department summaries (including zero-log headcount), competition ranking (`$rank`), and 7-day moving average daily trends with gap filling (`$densify`) execute directly inside MongoDB 6.0+ without in-memory Python looping.
- **Explain Instrumentation**: `/admin/explain/{endpoint}` executes MongoDB explain plans with `executionStats` confirming `IXSCAN` index coverage.

---

## Prerequisites

- **Python**: 3.11+ (tested on Python 3.11–3.14)
- **MongoDB**: 6.0+ (MongoDB 7.0/8.0 or MongoDB Atlas Free Tier M0)

---

## Installation & Setup

1. **Clone the repository:**
   ```bash
   git clone <repository_url>
   cd <repository_root>
   ```

2. **Set up virtual environment:**
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # Linux/macOS:
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure Environment Variables:**
   Create a local `.env` file (or export variables in your shell). Real environment variables will take precedence over `.env`:
   ```env
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db
   ```
   *Note: For MongoDB Atlas, use standard SRV format: `mongodb+srv://<user>:<password>@cluster0.mongodb.net/?retryWrites=true&w=majority`.*

5. **Load Sample Data:**
   ```bash
   python sample_seed.py
   ```

---

## Running the Application

Start the server using the exact assessment command:

```bash
uvicorn app.main:app --port 8000
```

- **Interactive API Documentation (Swagger UI)**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **OpenAPI Schema**: [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json)
- **Readiness Health Check**: [http://localhost:8000/health](http://localhost:8000/health)

---

## Running Automated Tests

Run the test suite with pytest:

```bash
pytest
```

---

## API Endpoints

### System
- `GET /health` — Readiness probe verifying active MongoDB connectivity.

### Employees
- `POST /employees` — Creates an employee with unique `emp_code` enforcement.
- `GET /employees` — Paginated listing with optional department filter, sorted by `emp_code`.

### Attendance
- `POST /attendance/punch-in` — Records daily punch-in; enforces single-record-per-day concurrency.
- `POST /attendance/punch-out` — Closes open attendance record; calculates work hours, overtime, and half-day status across midnight.
- `GET /attendance` — Database-side paginated attendance listing with inclusive date filtering.
- `PATCH /attendance/{emp_code}/{date}` — Manual regularization with atomic audit history appending.

### Analytics (MongoDB Aggregations)
- `GET /analytics/employees/{emp_code}/monthly` — Monthly employee attendance metrics and percentage.
- `GET /analytics/departments/summary` — Cross-collection department rollups including zero-log employee headcount.
- `GET /analytics/leaderboard/late` — Standard competition ranking (`1, 2, 2, 4`) of late minutes with tie-retention.
- `GET /analytics/departments/{department}/trend` — Daily gap-filled timeline with 7-day moving averages.

### Administration & Explain
- `GET /admin/explain/{endpoint}` — Raw MongoDB `executionStats` query plans.

---

## Project Structure

```text
├── app/
│   ├── __init__.py
│   └── main.py              # Complete FastAPI implementation
├── sample_data/
│   ├── employees.json        # Extended JSON employee records
│   └── attendance_logs.json  # Extended JSON attendance records
├── tests/
│   ├── __init__.py
│   └── test_api.py          # Comprehensive test suite
├── DATA_MODEL.md             # Authoritative database model specification
├── DECISIONS.md              # Architecture and design rationale
├── REVIEW.md                 # Starter code defect audit and resolution log
├── openapi.yaml              # Authoritative OpenAPI 3.0.3 contract
├── sample_seed.py            # MongoDB seed utility
└── requirements.txt          # Python dependencies
```

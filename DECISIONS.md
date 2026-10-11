# DECISIONS.md

1. **Indexes.**
We created:
- `employees.emp_code` (unique): guarantees business ID uniqueness and O(1) lookup.
- `employees.{department: 1, joined_on: 1}`: accelerates department filtering and headcount eligibility checks.
- `attendance_logs.{emp_code: 1, date: 1}` (unique): enforces at most one record per employee per day and eliminates punch-in race conditions.
- `attendance_logs.{emp_code: 1, punch_in: -1}`: supports fast punch-out candidate selection.
- `attendance_logs.{date: -1, emp_code: 1}`: serves paginated listing with stable secondary sorting without collection scans.
- `attendance_logs.{date: 1, late_minutes: 1}`: supports monthly late leaderboard filtering.
We considered and rejected an index on `{status: 1}` alone due to low cardinality (5 statuses), which offers poor selectivity and incurs unnecessary write overhead.

2. **Punch-in race.**
Both concurrent requests identify the employee and derive the attendance date under R1. Both attempt an atomic `insert_one`. The database engine's unique compound index on `(emp_code, date)` serializes write lock acquisition: exactly one insert succeeds and returns HTTP 201. The losing request encounters a PyMongo `DuplicateKeyError` (E11000), which our exception handler intercepts to immediately return HTTP 409 without corrupted state.

3. **Ties.**
We compute standard competition ranking (1, 2, 2, 4) using MongoDB's `$setWindowFields` with `$rank`. The query applies `rank <= limit` after ranking. Tied employees share the same rank and are both retained, followed by a deterministic secondary sort on `emp_code` ascending. As specified, the result may return more rows than `limit` to avoid arbitrarily cutting off tied performers.

4. **Headcount.**
We anchor department analytics in the `employees` collection (filtering `joined_on <= month_end`) and merge logs via `$unionWith`. Every eligible employee emits an identity record contributing to headcount (`$sum: 1`), ensuring employees with zero attendance records are fully counted while departments with zero eligible headcount are omitted.

5. **One thing you would change for 100x data.**
At 100x volume (~10M attendance records), we would maintain incremental daily rollups via MongoDB change streams or background cron aggregations into a dedicated `monthly_department_metrics` collection, turning real-time multi-collection pipeline scans into O(1) point reads, paired with time-based collection sharding on `{date: 1, emp_code: 1}`.

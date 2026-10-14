# DECISIONS.md

1. **Indexes.** `employees.emp_code` is unique so the business identifier is enforced by MongoDB. `employees(department, joined_on)` supports department/headcount filtering. `attendance_logs(emp_code, date)` is unique and supports employee-period reads, while `(date desc, emp_code)` supports attendance listing and date-range reads. I considered a status-first index, but the required date and employee filters are more selective.

2. **Punch-in race.** Both requests compute the same natural key, but neither relies on a prior existence check. MongoDB accepts one insert and rejects the other with `DuplicateKeyError`; the API maps that rejection to 409. Exactly one request returns 201.

3. **Ties.** Ranking is assigned from the sorted position only when the total changes. If the cutoff lands on a tie, every employee sharing that rank is returned, so the response can contain more than `limit` rows.

4. **Headcount.** The summary starts from employees joined by month end, groups those employees by department, and only then attaches logs. Employees with no attendance documents remain in the count.

5. **At 100x scale.** I would move department summary and trend calculations fully into indexed aggregation pipelines, and likely pre-aggregate daily facts, so Python does not materialize broad periods.

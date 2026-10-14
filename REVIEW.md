# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Grace-period and whole-minute boundary rules were wrong. | A 09:40:01 punch for a 09:30 shift was not 10 late minutes. | Compare elapsed seconds strictly with 600, then floor minutes. |
| 2 | `compute_overtime` | Overnight boundaries and the 30-minute minimum were not handled. | A 22:00-06:00 shift used the wrong day, or a 10-minute overrun got overtime. | Build the shift end on the correct calendar day and require 1,800 seconds. |
| 3 | `create_employee` | Duplicate protection was a check-then-insert race and timestamps were not serialized. | Concurrent creates could both succeed; `created_at` was a BSON date in JSON. | Add a unique index, catch `DuplicateKeyError`, and serialize instants. |
| 4 | `list_employees` | Page 1 skipped records, totals ignored filters, and sorting was unspecified. | The first page started at item 21 and filtered totals were too large. | Use `(page - 1) * page_size`, filtered counts, and `emp_code` sorting. |
| 5 | `punch_in` | Unknown employees, local timestamps, overnight dates, and duplicate races were mishandled. | Unknown codes raised a server error and boundary/concurrency tests failed. | Validate the employee, normalize epoch milliseconds, calculate R1, and use the unique index. |
| 6 | `list_attendance` | It loaded all records into memory, sorted incorrectly, mutated documents, and exposed BSON dates. | Large collections were slow and responses violated the contract. | Use cursor pagination, deterministic sorting, and response serialization. |
| 7 | Missing endpoints | Punch-out, regularization, analytics, and explain operations were absent. | Those OpenAPI operations returned 404. | Implement all contract operations and rules. |

I also checked the original work-hours formula. Its basic duration calculation was sound, but it needed
decimal half-up rounding, so it was replaced by `rounded_hours`. A missing record is intentionally
different from an `ABSENT` record, so analytics preserve that distinction.

# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
   On `employees`, I created a unique `(emp_code)` index for lookups and duplicate rejection, plus `(department, emp_code)`, `(department, joined_on)`, and `(joined_on, department)` to support sorted department lists and headcount queries. On `attendance_logs`, I created a unique `(emp_code, date)` index for one log per day guarantees and monthly queries, `(emp_code, punch_in: -1)` for finding open punch-ins, and `(date: -1, emp_code: 1)` plus status-prefixed compound indexes for fast paginated listing and date-range analytics. I considered and rejected a standalone `(late_minutes)` index because leaderboard queries always filter by `date` range first.
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
  Both requests validate the employee, compute the IST attendance `date`, and call `insert_one` directly without a check-then-insert query. MongoDB's unique index on `(emp_code, date)` ensures only one write commits (returning `201`), while the second immediately raises `DuplicateKeyError`, which the handler catches to return `409`.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
   Both tied employees are returned, ordered by `emp_code` ascending, so all employees tied at the cutoff are included.
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
   The pipeline starts on `employees` (`joined_on <= month_end`), performs a `$lookup` into `attendance_logs` for that month, and runs `$unwind` with `preserveNullAndEmptyArrays: true` so zero-log employees remain in the department group.
5. **One thing you would change** if this had to serve 100x the data.
   I would maintain pre-aggregated daily and monthly summary collections updated on punch/regularization writes instead of aggregating raw logs on every read.

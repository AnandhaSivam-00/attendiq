# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | 13th line from the `main.py` (Optional module) | The `Optional` module is deprecated | Python interpreter shows the deprecation warning | I removed the `Optional` import and used `Union` instead |
| 2 | `health` function | It always returns `200`, even when MongoDB is unreachable. | Before connecting with MongoDB and calling `GET /health` returns `200` instead of `503`. | Added `client.admin.command("ping")` inside a `try/except` block and raised `HTTPException(503, detail="Database unavailable")` on failure. |
| 3 | `create_employee` function | Uses `find_one` then `insert_one` without catching `DuplicateKeyError`, sets `created_at` to a local `datetime.now()` (not truncated to seconds), and returns a `datetime` object instead of integer epoch milliseconds. | Just look and think about the code. | Rely on a unique index on `emp_code`, catch `DuplicateKeyError` to raise `409`, store UTC BSON datetime truncated to whole seconds, and return `created_at` as epoch milliseconds `int`. |
| 4 | `list_employees` function | `skip = page * page_size` skips the entire first page when `page=1`; `count_documents({})` ignores `department` and results are not sorted (ascending) by `emp_code` | Calling `GET /employees?page=1&page_size=20` returns page 2 (items 21–40). Filtering by `?department=Engineering` still returns the whole company's `total`. | Changed `skip` to `(page - 1) * page_size`, counted with `count_documents(q)`, added `.sort("emp_code", 1)`, validated `page >= 1` and `1 <= page_size <= 100`, checked `if department is not None:`, and converted `created_at` to epoch ms. |

Also note anything you looked at and decided was **not** a defect, and why.

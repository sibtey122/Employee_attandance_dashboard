# DECISIONS.md

1. **Indexes.** `employees.emp_code` unique (identity, 409 on duplicates). `employees (department, joined_on)` and `(joined_on)` for headcount. `attendance_logs (emp_code, date)` unique: one record per day, race-safe punch-in, monthly and per-employee queries. `attendance_logs (date desc, emp_code)`: the list's sort order and month scans. Rejected: an index on `status`; low cardinality, extra write cost, and the date index already avoids a collection scan.
2. **Punch-in race.** Both requests find the employee, compute the same attendance date and call insert. The unique (emp_code, date) index lets MongoDB accept one; the other gets a duplicate-key error, which I turn into 409. There is no "check, then insert".
3. **Ties.** The pipeline ranks first (`$rank`: 1, 2, 2, 4) and only then keeps rank <= limit, so every employee tied at the cutoff is returned, even if that means more than `limit` rows.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and joins logs onto each person, so someone with zero logs still counts.
5. **100x data.** I would stop scanning raw logs for every analytics call: keep per-employee monthly totals updated at write time, and switch list pagination from skip to a date/emp_code cursor.

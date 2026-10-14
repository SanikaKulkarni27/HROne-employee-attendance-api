# DECISIONS.md

**1. Indexes.** I created a unique index on `emp_code` to prevent duplicate employees and a unique compound index on `emp_code` and `date` to prevent duplicate daily attendance records. Other indexes support department and joining-date lookups, date-based attendance queries, status filters, and punch-in searches. I would avoid adding indexes without measuring their benefit because each index increases storage and write costs.

**2. Punch-in race.** If two identical punch-in requests arrive together, both may initially check the employee, but the unique attendance index allows only one record for that employee and date. The other insert raises a duplicate-key error and returns HTTP 409.

**3. Ties.** The leaderboard uses competition ranking. Employees tied on total late minutes receive the same rank, and everyone whose rank is within the limit is included, even if this means returning more rows than the limit.

**4. Headcount.** The department summary starts with eligible employees and uses a lookup with `preserveNullAndEmptyArrays`. Employees without attendance logs therefore remain in the results and count toward headcount.

**5. Scaling.** For 100 times more data, I would measure query plans and response times, review index effectiveness, and consider caching frequently requested analytics where appropriate.
# Calculate and validate a typed table

- A formula column derives its value; edit its inputs. Invalid dates or numeric values are errors, not zero.

**Offline rows:** Brush: quantity 3, price 4, date 2026-10-14, active true. Saw: quantity 2, price 8, date 2026-10-15, active true.

**Supplied view definition:**
```yaml
version: 1
views:
  - id: purchases
    name: Purchases
    type: table
    columns:
      - file.name
      - property: date
        type: date
      - property: quantity
        type: number
      - property: price
        type: number
      - id: total
        label: Total
        formula: price * quantity
    summaries:
      total: sum
```
The totals are 12 and 16, sum 28. Changing Brush quantity to 5 gives 20 and 16, sum 36. Date `2026-02-31` is invalid. Price `oops` produces a type/formula error; it is not a valid zero-priced row.

This fixture exercises typed properties and formulas. It does not promise a currency type or complete Office spreadsheet compatibility.

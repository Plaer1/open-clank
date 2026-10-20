# Trace an admitted tool script

- Native script execution depends on the route's toolset, enablement and filtered allowlist. It is not a universal Run Script button or unrestricted Node environment.
- Read the declared tool schema and call budget. A script cannot gain an excluded capability by naming it.

**Fictional offline declarations:** A has exec flag=false and read_file only; no exec capability. B admits exec and `tools.practice_read({key: string}) → {quantity: number, price: number}`; max_tool_calls=1. Task, actor, session, workflow and nested exec capabilities are excluded. Record R1 is quantity 3, price 4.

**B's paper script:**
```javascript
const row = await tools.practice_read({key: "R1"});
return {subtotal: row.quantity * row.price};
```
It returns subtotal 12 with one permitted call in this fictional trace. C attempts `tools.task(...)`, excluded. D calls practice_read twice, exceeding the budget.

The practice_read name is an invented teaching schema, not an advertised installed API. Actual tool execution still needs the real advertised capability and host permission.

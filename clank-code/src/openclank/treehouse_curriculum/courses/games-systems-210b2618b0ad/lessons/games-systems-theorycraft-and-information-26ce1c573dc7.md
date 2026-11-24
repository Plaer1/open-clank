# Price an informed decision

Optional practice.

- Each independent round hides treasure L/R with probability1/2 each. Actions: choose L, choose R, or buy a perfect pre-choice scout for cost 2 then choose. Correct earns10, wrong0; score changes by reward minus scout cost; next state is a fresh independent L/R.
- Observation without scout is empty; scout reports true side. Objective: maximize expected score per round. Feedback reveals side and net score after choice.
- Intervention: imperfect scout reliability3/4, symmetric errors: P(report L|L)=P(report R|R)=3/4; cost remains2. Compare cost 6 with perfect accuracy. Each scout also takes one extra action; simple choice takes one action.

**Self-check:** List state/action/observation/payoff/transition. Compare expected net score and net score per action for no scout, perfect cost2, imperfect cost2 and perfect cost6. State a downside and sensitivity conclusion.

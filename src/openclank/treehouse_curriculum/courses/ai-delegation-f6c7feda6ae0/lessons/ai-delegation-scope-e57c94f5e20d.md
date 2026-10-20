# Delegate one bounded outcome

- Split work only when the outcomes can be separated. Give each worker its inputs, owned scope, constraints, evidence format and stopping rule.
- Model selection follows the task's declared authority. A worker definition or role name does not authorize a different model or new tool.
- Integrate returned evidence and resolve conflicts; separate workers do not make conclusions true by agreement.

Hypothetical contract: compare documents A and B read-only; each worker owns one document; only model M is authorized; no external writes. Return supported claims with source locations, uncertainties and a check for the lead. If M is unavailable, report that condition instead of silently substituting a model.

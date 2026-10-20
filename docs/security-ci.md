# Security CI guide

This is maintainer guidance about the **checked-in workflows**, not deployment
security or a certification of a release. For running Open Clank securely and
private vulnerability reporting, see [SECURITY.md](../SECURITY.md).

The public default/contribution branch is `main`. Workflow code and repository
rules are separate authorities: a failed job blocks merging only if the actual
branch/ruleset requires it. This document does not claim branch-protection
settings or recent workflow outcomes have been inspected.

## Configured checks

| Workflow / job name | Configured behavior | Where to review |
| --- | --- | --- |
| `secret-scan.yml` / `gitleaks` | Pull requests, main pushes and manual dispatch; failures are eligible to be required | Check logs; rotate confirmed leaked credentials |
| `workflow-security.yml` / `actionlint`, `zizmor (Actions SAST)` | Pull requests, main pushes and manual dispatch; syntax/security lint failures | Check logs |
| `dependency-review.yml` / `dependency-review (PR gate)` | PR-only job; fails for newly introduced moderate-or-worse dependency advisories | PR check/logs |
| `dependency-review.yml` / `pip-audit (advisory)` | Requirements audit with `continue-on-error: true` | Job output; no SARIF upload is configured here |
| `container-scan.yml` / `hadolint (Dockerfile lint)` | Pull requests, main pushes and manual dispatch; Dockerfile lint failures | Check logs |
| `container-trivy.yml` / `Trivy (image scan, advisory)` | Path-filtered PR/manual scan, advisory | Job output |
| `container-trivy.yml` / `Trivy (image scan + SARIF upload)` | Path-filtered main-push scan, advisory, with SARIF upload | Job output and accepted code-scanning upload |
| `codeql.yml` / `Analyze (actions)`, `Analyze (javascript-typescript)`, `Analyze (python)` | Pushes to dev/main, PRs targeting dev, weekly schedule | Check logs and accepted code-scanning findings |
| `ci.yml` / `Python syntax (compileall)`, `JS syntax (node --check)` | Syntax checks; possible required checks when maintainers configure them | Check logs |
| `ci.yml` / managed engine, History service, Windows portable and pytest jobs | Build/protocol/behavior checks with job-specific conditions; docs-only pytest can be skipped by workflow logic | Actual run details; a build is not OS feature acceptance |

**Coverage mismatch for maintainer review:** CodeQL's current PR filter targets
`dev`, while public contributions target `main`. The normal/main-push scanners
and CodeQL push/schedule behavior do not make every main PR a CodeQL run. Changing
this prose does not change workflow filters; any CI policy change needs its own
review.

## Results and failures

Read the actual PR Checks/run logs first. Security-tab findings depend on a
scanner's upload path, permissions and GitHub accepting the report. “Advisory”
does not mean every finding appears there or that it can never be made required.
`continue-on-error` behavior also differs from repository merge rules.

- A confirmed secret finding needs credential rotation and source cleanup;
  removing a visible string or commit alone does not undo disclosure.
- Dependency review identifies changed dependencies; pip-audit reviews declared
  current requirements and can flag existing issues. Review the advisory and
  scope before selecting a patched version.
- Workflow/hadolint findings should be resolved against the exact failing
  message. Trivy/CodeQL findings need review of reachability, affected versions
  and actual deployment behavior.

## Maintainer configuration

Review the current GitHub ruleset/branch protections for **main**. Choose the
checks that must succeed, using exact names from a completed run; verify that
required checks report for every applicable PR and do not hang because of branch
or path filters. Requiring reviews/Code Owners is a maintainer policy decision,
not an inspected current setting in this guide.

Review dependency graph, Dependabot alerts/security updates and code-scanning
settings in the repository. CodeQL already uses checked-in advanced setup;
avoid accidentally enabling a conflicting second setup. The configured
[Dependabot file](../.github/dependabot.yml) requests weekly pip, npm,
GitHub Actions and Docker updates. Review actual PRs/results rather than
assuming every configured update or scan ran successfully.

[Contributing](../CONTRIBUTING.md) · [Beta 1 limits](known-limits.md)

## Exact artifact boundary

`scripts/check_release_artifacts.py` checks actual index bytes and the modeled
Docker context, with scanner output kept value-silent. `--paths-only` verifies
exclusions; content mode requires the verified Gitleaks executable. An assembled
package uses `--export-root` so ignore rules cannot hide files already copied
into it. External/broken export symlinks are refused.

The gate excludes private Clanker trees, runtime History credentials and their
sidecars, environment secrets and database artifacts. Exact synthetic/UI-label
classifications are constrained to the relevant rule, source/value and path;
immutable history fingerprints do not waive a future secret at the same line.
A clean gate is scoped evidence, not a complete personal-prose/media audit or a
scan of every local recovery ref. Final release index, ancestry, media and export
must be reviewed together. No hosted workflow success is claimed here.

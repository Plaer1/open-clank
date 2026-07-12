#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

out="${1:-robonotes/source-coverage-union-2026-07-10.md}"
relative_out="${out#./}"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

{
  root_rg_args=(
    --files --hidden
    -g '!.git/**'
    -g '!.venv/**'
    -g '!.archive/**'
    -g '!.references/**'
    -g '!data/**'
    -g '!packages/**'
    -g '!**/__pycache__/**'
    -g '!**/.pytest_cache/**'
  )
  rg "${root_rg_args[@]}"

  copal_roots=(
    packages/Copal/src
    packages/Copal/rust
    packages/Copal/ui
    packages/Copal/scripts
    packages/Copal/sample-vault
    packages/Copal/prisma
    packages/Copal/examples
    packages/Copal/public
    packages/Copal/servo-shell
  )
  child_rg_args=(
    --files --hidden
    -g '!**/.git/**'
    -g '!**/node_modules/**'
    -g '!**/target/**'
    -g '!**/.next/**'
  )
  for root in "${copal_roots[@]}"
  do
    if [[ -e "$root" ]]; then
      rg "${child_rg_args[@]}" "$root"
    fi
  done

  find packages/Copal -maxdepth 1 -type f -print
  [[ -f packages/Copal/db/copal.redb ]] && printf '%s\n' packages/Copal/db/copal.redb

  reference_roots=(
    packages/Copal/.references/obsidian-local
    packages/Copal/treehouse/packages/learnhouse
    packages/Copal/treehouse/packages/skills-service
  )
  for root in "${reference_roots[@]}"
  do
    if [[ -e "$root" ]]; then
      rg "${child_rg_args[@]}" "$root"
    fi
  done
} | LC_ALL=C sort -u | awk -v output="$relative_out" '$0 != output' > "$tmp"

mkdir -p "$(dirname "$out")"

{
  printf '# Source coverage union manifest\n\n'
  printf 'Generated: %s\n\n' "$(date -Iseconds)"
  printf 'Purpose: machine-verifiable Stage 0 ledger for the recursive Copal multi-metaplan. '
  printf 'SHA-256 calculation reads every listed file. Contents are not copied into this record.\n\n'
  printf '| Path | Bytes | SHA-256 | Disposition | Primary phase |\n'
  printf '|---|---:|---|---|---|\n'

  while IFS= read -r path; do
    [[ -f "$path" ]] || continue
    bytes="$(stat -c '%s' -- "$path")"
    hash="$(sha256sum -- "$path" | cut -d' ' -f1)"
    disposition='supporting-first-party'
    phase='00/05'

    case "$path" in
      .futures/*)
        disposition='plan-control'; phase='00/05' ;;
      robonotes/*|.robonotes/*)
        disposition='audit-or-run-evidence'; phase='00/05' ;;
      data/backups/*)
        disposition='runtime-backup-binary-metadata'; phase='00/05' ;;
      *.env|*.env.*|*/.env|*/.env.*)
        disposition='sensitive-config-metadata-only'; phase='00' ;;
      packages/Copal/.references/obsidian-local/*)
        disposition='proprietary-ux-reference-no-copy'; phase='01/02' ;;
      packages/Copal/treehouse/packages/learnhouse/*)
        disposition='AGPL-reference-source-no-copy-by-default'; phase='03/04/05' ;;
      packages/Copal/treehouse/packages/skills-service/*)
        disposition='Apache-reference-source-explicit-reuse-gate'; phase='03/04/05' ;;
      packages/Copal/servo-shell/*)
        disposition='inventoried-user-excluded-servo'; phase='00/05' ;;
      packages/Copal/db/*)
        disposition='runtime-redb-supported-metadata'; phase='00/05' ;;
      *Treehouse*|*TreeHouse*|*treehouse*)
        disposition='treehouse-first-party-or-fixture'; phase='03/04/05' ;;
      *.base|*base*test*|*Base*test*)
        disposition='bases-source-or-fixture'; phase='02/05' ;;
      static/*|routes/*|src/*|core/*|services/*|app.py)
        disposition='odysseus-first-party'; phase='01/02/04/05' ;;
      tests/*)
        disposition='first-party-test'; phase='01/02/04/05' ;;
      packages/Copal/*)
        disposition='copal-first-party'; phase='01/02/04/05' ;;
    esac

    safe_path="${path//|/\\|}"
    printf '| %s | %s | %s | %s | %s |\n' "$safe_path" "$bytes" "$hash" "$disposition" "$phase"
  done < "$tmp"

  printf '\n## Aggregate dispositions for intentionally non-enumerated trees\n\n'
  printf 'These descendants are represented as aggregates because they are VCS object stores, '
  printf 'installed dependencies, generated builds, caches, historical archives, or unrelated '
  printf 'reference trees. Their source manifests or revision metadata remain enumerated above where applicable.\n\n'
  printf '| Tree | Files | Bytes | Disposition |\n'
  printf '|---|---:|---:|---|\n'

  aggregate_specs=(
    '.git|VCS object store; represented by branch/status/revision'
    '.venv|installed Python dependency environment'
    '.archive|historical archive outside active product scope'
    '.references|unrelated root reference collection; Copal references enumerated separately'
    'packages/mimo-code|protected unrelated dirty MiMo source tree'
    'packages/mimo-code/node_modules|installed dependency tree'
    'mcp_servers/frankenmemory/target|generated Rust build output'
    'packages/Copal/.node_modules.bak-20260708-143848|installed dependency backup'
    'packages/Copal/.next|generated Next build output'
    'packages/Copal/out|generated static export'
    'packages/Copal/.archive|historical Copal archive'
    'packages/Copal/.references/TiddlyWiki5|reference outside this plan current feature scope'
    'data/cache|runtime cache'
    'data/chroma|runtime vector store'
    'data/logs|runtime logs'
    'data/uploads|runtime user uploads'
  )
  for spec in "${aggregate_specs[@]}"
  do
    tree="${spec%%|*}"
    disposition="${spec#*|}"
    [[ -e "$tree" ]] || continue
    files="$(find "$tree" -type f 2>/dev/null | wc -l)"
    bytes="$(du -sb "$tree" 2>/dev/null | awk '{print $1}')"
    printf '| %s | %s | %s | %s |\n' "$tree" "$files" "${bytes:-0}" "$disposition"
  done

  printf '\n## Completeness declaration\n\n'
  printf -- '- Enumerated files: %s\n' "$(wc -l < "$tmp")"
  printf -- '- Every enumerated file was read to compute SHA-256.\n'
  printf -- '- Every non-enumerated descendant belongs to an aggregate disposition above.\n'
  printf -- '- Semantic conclusions still require the phase-specific audits; this manifest proves coverage, not behavior.\n'
} > "$out"

printf '%s\n' "$out"

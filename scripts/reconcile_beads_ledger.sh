#!/usr/bin/env bash
# scripts/reconcile_beads_ledger.sh — idempotent diff of open bead set vs open ledger entries
#
# Cross-checks open beads against critical/high gaps in the parity ledger to prevent
# completion illusion where all beads are closed but critical gaps remain open.
#
# Usage:
#   ./scripts/reconcile_beads_ledger.sh
#
# Exit codes:
#   0: All active ledger gaps have corresponding active beads, no orphans
#   1: Found orphans (active ledger gaps without beads, or active gap beads without active ledger entries)
#   2: Script error (missing files, invalid JSON, etc.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# File paths. Keep the historical location as a fallback for older checkouts.
LEDGER_CANDIDATES=(
    "$PROJECT_ROOT/docs/evidence/dropin-parity-gap-ledger.json"
    "$PROJECT_ROOT/docs/dropin-parity-gap-ledger.json"
)
LEDGER_FILE="${LEDGER_CANDIDATES[0]}"
for candidate in "${LEDGER_CANDIDATES[@]}"; do
    if [[ -f "$candidate" ]]; then
        LEDGER_FILE="$candidate"
        break
    fi
done

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

# Check prerequisites
check_prerequisites() {
    if [[ ! -f "$LEDGER_FILE" ]]; then
        log_error "Gap ledger not found: $LEDGER_FILE"
        exit 2
    fi

    if ! command -v jq >/dev/null 2>&1; then
        log_error "jq is required but not installed"
        exit 2
    fi

    if ! command -v br >/dev/null 2>&1; then
        log_error "br (beads) command is required but not found"
        exit 2
    fi
}

# Extract active critical/high gaps from ledger
get_open_ledger_gaps() {
    log_info "Extracting open critical/high gaps from ledger..."

    # Get entries with critical or high severity that are not retired/resolved/closed.
    # Missing status/mismatch_kind means the gap is still active.
    jq -r '.entries[] |
        select(.severity == "critical" or .severity == "high") |
        select((.status // "open") != "retired" and (.status // "open") != "resolved" and (.status // "open") != "closed") |
        select((.mismatch_kind // "open") != "retired" and (.mismatch_kind // "open") != "resolved" and (.mismatch_kind // "open") != "closed") |
        "\(.gap_id)|\(.severity)|\(.owner_issue_primary // "")|\(.area // "")"' "$LEDGER_FILE" | \
    while IFS='|' read -r gap_id severity owner_bead area; do
        echo "LEDGER_GAP:$gap_id:$severity:$owner_bead:$area"
    done
}

# Get active beads from beads system
get_open_beads() {
    log_info "Fetching active beads..."

    # Get open/in-progress beads with their IDs, titles, labels, and external refs.
    # The exported bead field is external_ref; external_id is kept as a compatibility
    # fallback for older br versions.
    br list --json 2>/dev/null | \
    jq -r '(.issues? // .)[] |
        select(.status == "open" or .status == "in_progress") |
        "\(.id)|\(.title // "")|\(.labels // [] | join(","))|\(.external_ref // .external_id // "")"' | \
    while IFS='|' read -r bead_id title labels external_ref; do
        echo "OPEN_BEAD:$bead_id|$title|$labels|$external_ref"
    done
}

# Match ledger gaps with beads
match_gaps_to_beads() {
    local ledger_gaps=()
    local open_beads=()
    local matched_gaps=()
    local matched_beads=()
    local active_gap_ids=()

    log_info "Reading gap ledger entries..."
    while read -r line; do
        if [[ $line == LEDGER_GAP:* ]]; then
            ledger_gaps+=("$line")
        fi
    done < <(get_open_ledger_gaps)

    log_info "Reading active beads..."
    while read -r line; do
        if [[ $line == OPEN_BEAD:* ]]; then
            open_beads+=("$line")
        fi
    done < <(get_open_beads)

    for gap_line in "${ledger_gaps[@]}"; do
        local gap_payload="${gap_line#LEDGER_GAP:}"
        active_gap_ids+=("${gap_payload%%:*}")
    done

    log_info "Found ${#ledger_gaps[@]} active ledger gaps and ${#open_beads[@]} active beads"

    local ledger_orphan_count=0
    local bead_orphan_count=0

    # Check for ledger gaps without corresponding beads
    log_info "Checking for ledger gaps without beads..."
    for gap_line in "${ledger_gaps[@]}"; do
        # Parse: LEDGER_GAP:gap_id:severity:owner_bead:area
        local gap_id="${gap_line#LEDGER_GAP:}"
        local gap_id_only="${gap_id%%:*}"
        local rest="${gap_id#*:}"
        local severity="${rest%%:*}"
        local rest2="${rest#*:}"
        local owner_bead="${rest2%%:*}"
        local area="${rest2#*:}"

        local found_bead=false

        # Exact match by active owner_issue_primary.
        if [[ -n "$owner_bead" ]]; then
            for bead_line in "${open_beads[@]}"; do
                local bead_id="${bead_line#OPEN_BEAD:}"
                local bead_id_only="${bead_id%%|*}"
                if [[ "$bead_id_only" == "$owner_bead" ]]; then
                    found_bead=true
                    matched_gaps+=("$gap_line")
                    matched_beads+=("$bead_line")
                    break
                fi
            done
        fi

        # Exact match by external_ref=<gap-id>. This is the preferred durable link
        # because ledger owner_issue_primary can point at an older umbrella bead.
        if [[ "$found_bead" == false ]]; then
            for bead_line in "${open_beads[@]}"; do
                local bead_content="${bead_line#OPEN_BEAD:}"
                local bead_external_ref="${bead_content##*|}"
                if [[ "$bead_external_ref" == "$gap_id_only" ]]; then
                    found_bead=true
                    matched_gaps+=("$gap_line")
                    matched_beads+=("$bead_line")
                    break
                fi
            done
        fi

        if [[ "$found_bead" == false ]]; then
            log_error "ORPHAN LEDGER GAP: $gap_id_only ($severity severity, area: $area)"
            if [[ -n "$owner_bead" ]]; then
                log_error "  Expected owner bead: $owner_bead"
            fi
            log_error "  Create or reopen a bead with --external-ref $gap_id_only, or update owner_issue_primary to an open bead."
            ((ledger_orphan_count += 1))
        fi
    done

    # Check for active gap-tracking beads that reference closed/non-existent ledger entries.
    log_info "Checking for beads without corresponding ledger gaps..."
    for bead_line in "${open_beads[@]}"; do
        local bead_content="${bead_line#OPEN_BEAD:}"
        local bead_id="${bead_content%%|*}"
        local rest="${bead_content#*|}"
        local title="${rest%%|*}"
        local external_ref="${bead_content##*|}"

        # Skip if this bead was already matched
        local already_matched=false
        for matched in "${matched_beads[@]}"; do
            if [[ "$matched" == "$bead_line" ]]; then
                already_matched=true
                break
            fi
        done

        if [[ "$already_matched" == false ]] && [[ "$external_ref" == gap-* ]]; then
            local gap_is_active=false
            for active_gap_id in "${active_gap_ids[@]}"; do
                if [[ "$external_ref" == "$active_gap_id" ]]; then
                    gap_is_active=true
                    break
                fi
            done

            if [[ "$gap_is_active" == false ]]; then
                log_error "ORPHAN GAP BEAD: $bead_id - $title"
                log_error "  external_ref=$external_ref is not an active critical/high ledger gap"
                log_error "  Close the bead, clear/update external_ref, or restore the ledger gap if it is still active."
                ((bead_orphan_count += 1))
            fi
        fi
    done

    if [[ $ledger_orphan_count -eq 0 && $bead_orphan_count -eq 0 ]]; then
        log_info "SUCCESS: No orphan ledger gaps or gap-tracking beads found"
        return 0
    else
        log_error "FAILURE: Found $ledger_orphan_count orphan ledger gaps and $bead_orphan_count orphan gap beads"
        return 1
    fi
}

# Main function
main() {
    log_info "Starting beads ↔ ledger reconciliation..."

    check_prerequisites

    if ! match_gaps_to_beads; then
        log_error "Reconciliation failed - there are orphan entries"
        exit 1
    fi

    log_info "Reconciliation completed successfully - no orphans found"
    exit 0
}

# Run if called directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi

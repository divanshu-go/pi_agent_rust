#!/usr/bin/env bash
# Run cargo with explicit build/temp storage and fail-fast filesystem headroom
# checks. This prevents long all-target runs from dying late during linking with
# opaque ENOSPC errors or from creating repo-root bead-named target directories.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

usage() {
    cat <<'EOF'
Usage:
  scripts/cargo_headroom.sh [options] <cargo-subcommand> [cargo-args...]

Options:
  --runner <rch|auto|local>   Cargo runner mode (default: PI_CARGO_RUNNER or rch)
  --target-dir <path>         Override CARGO_TARGET_DIR for this invocation
  --tmpdir <path>             Override TMPDIR for this invocation
  --min-free-mb <mb>          Required free MB on target/tmp mounts (default: 24576)
  --min-inode-free-pct <pct>  Required free inode percent (default: 5)
  --admit-only                Emit the admission decision without running cargo
  --decision-json <path>      Also write the machine-readable decision to <path>
  --allow-local-fallback      Permit auto-mode local fallback for heavy commands
  -h, --help                  Show this help

Environment:
  PI_CARGO_BUILD_ROOT         Build root used when CARGO_TARGET_DIR is unset
                              (default: /data/tmp/pi_agent_rust, or
                              /data/tmp/pi_agent_rust_cargo if the former
                              resolves inside this repository)
  PI_CARGO_AGENT_SUFFIX       Per-agent subdirectory suffix (default: $USER)
  PI_CARGO_ALLOW_REPO_TARGET  Set to 1 to allow target dirs under the repo root
  PI_CARGO_ALLOW_LOCAL_FALLBACK
                              Set to 1 to permit heavy local fallback in auto mode
EOF
}

die() {
    echo "[cargo-headroom] ERROR: $*" >&2
    exit 2
}

RUNNER="${PI_CARGO_RUNNER:-rch}"
MIN_FREE_MB="${PI_CARGO_HEADROOM_MIN_FREE_MB:-24576}"
MIN_INODE_FREE_PCT="${PI_CARGO_HEADROOM_MIN_FREE_INODE_PCT:-5}"
DEFAULT_BUILD_ROOT="/data/tmp/pi_agent_rust"
if [[ -e "$DEFAULT_BUILD_ROOT" ]]; then
    if DEFAULT_BUILD_ROOT_REAL="$(cd "$DEFAULT_BUILD_ROOT" && pwd -P 2>/dev/null)"; then
        case "$DEFAULT_BUILD_ROOT_REAL" in
            "$PROJECT_ROOT"|"$PROJECT_ROOT"/*)
                DEFAULT_BUILD_ROOT="/data/tmp/pi_agent_rust_cargo"
                ;;
        esac
    fi
fi
BUILD_ROOT="${PI_CARGO_BUILD_ROOT:-$DEFAULT_BUILD_ROOT}"
TARGET_OVERRIDE=""
TMPDIR_OVERRIDE=""
ADMIT_ONLY=0
DECISION_JSON_PATH=""
ALLOW_LOCAL_FALLBACK="${PI_CARGO_ALLOW_LOCAL_FALLBACK:-0}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runner)
            [[ $# -ge 2 ]] || die "--runner requires a value"
            RUNNER="$2"
            shift 2
            ;;
        --target-dir)
            [[ $# -ge 2 ]] || die "--target-dir requires a value"
            TARGET_OVERRIDE="$2"
            shift 2
            ;;
        --tmpdir)
            [[ $# -ge 2 ]] || die "--tmpdir requires a value"
            TMPDIR_OVERRIDE="$2"
            shift 2
            ;;
        --min-free-mb)
            [[ $# -ge 2 ]] || die "--min-free-mb requires a value"
            MIN_FREE_MB="$2"
            shift 2
            ;;
        --min-inode-free-pct)
            [[ $# -ge 2 ]] || die "--min-inode-free-pct requires a value"
            MIN_INODE_FREE_PCT="$2"
            shift 2
            ;;
        --admit-only)
            ADMIT_ONLY=1
            shift
            ;;
        --decision-json)
            [[ $# -ge 2 ]] || die "--decision-json requires a value"
            DECISION_JSON_PATH="$2"
            shift 2
            ;;
        --allow-local-fallback)
            ALLOW_LOCAL_FALLBACK=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -V|--version)
            break
            ;;
        --)
            shift
            break
            ;;
        --*)
            die "unknown option: $1"
            ;;
        *)
            break
            ;;
    esac
done

[[ $# -gt 0 ]] || die "missing cargo subcommand; run with --help for usage"

case "$RUNNER" in
    rch|auto|local) ;;
    *) die "invalid runner '$RUNNER' (expected rch, auto, or local)" ;;
esac

[[ "$MIN_FREE_MB" =~ ^[0-9]+$ && "$MIN_FREE_MB" -gt 0 ]] \
    || die "invalid --min-free-mb '$MIN_FREE_MB'"
[[ "$MIN_INODE_FREE_PCT" =~ ^[0-9]+$ && "$MIN_INODE_FREE_PCT" -gt 0 && "$MIN_INODE_FREE_PCT" -lt 100 ]] \
    || die "invalid --min-inode-free-pct '$MIN_INODE_FREE_PCT'"

safe_agent_suffix() {
    printf '%s' "${PI_CARGO_AGENT_SUFFIX:-${USER:-agent}}" | tr -c 'A-Za-z0-9._-' '_'
}

resolve_dir() {
    local dir="$1"
    mkdir -p "$dir" || die "cannot create directory '$dir'"
    (cd "$dir" && pwd -P)
}

candidate_path() {
    local path="$1"
    local parent base

    if [[ "$path" != /* ]]; then
        path="$PROJECT_ROOT/$path"
    fi

    parent="$(dirname "$path")"
    base="$(basename "$path")"
    while [[ ! -d "$parent" && "$parent" != "/" ]]; do
        base="$(basename "$parent")/$base"
        parent="$(dirname "$parent")"
    done

    if [[ -d "$parent" ]]; then
        parent="$(cd "$parent" && pwd -P)"
        printf '%s/%s' "$parent" "$base"
    else
        printf '%s' "$path"
    fi
}

write_cache_tag() {
    local dir="$1"
    local tag="$dir/CACHEDIR.TAG"
    if [[ ! -e "$tag" ]]; then
        {
            echo "Signature: 8a477f597d28d172789f06886806bc55"
            echo "# This directory contains disposable Cargo build artifacts."
            echo "# See https://bford.info/cachedir/."
        } > "$tag"
    fi
}

json_escape() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//$'\n'/\\n}"
    value="${value//$'\r'/\\r}"
    value="${value//$'\t'/\\t}"
    printf '%s' "$value"
}

cargo_command_string() {
    local out="" arg
    for arg in "$@"; do
        if [[ -n "$out" ]]; then
            out+=" "
        fi
        out+="$arg"
    done
    printf '%s' "$out"
}

is_safe_local_command() {
    local subcommand="$1"
    shift || true
    case "$subcommand" in
        fmt|metadata|fetch|tree|locate-project|-V|--version|version)
            return 0
            ;;
    esac

    return 1
}

emit_admission_decision() {
    local decision="$1"
    local resolved_runner="$2"
    local reason="$3"
    local command_class="$4"
    local rch_detail="$5"
    shift 5
    local command_text json recommended_target_dir recommended_tmpdir target_remediation tmpdir_remediation

    command_text="$(cargo_command_string "$@")"
    recommended_target_dir="$BUILD_ROOT/$(safe_agent_suffix)/target"
    recommended_tmpdir="$BUILD_ROOT/$(safe_agent_suffix)/tmp"
    target_remediation="Set CARGO_TARGET_DIR or pass --target-dir to an off-repo scratch path such as $recommended_target_dir; current CARGO_TARGET_DIR=$CARGO_TARGET_DIR"
    tmpdir_remediation="Set TMPDIR or pass --tmpdir to an off-repo scratch path such as $recommended_tmpdir; current TMPDIR=$TMPDIR"
    json="{\"schema\":\"pi.cargo_headroom.admission.v1\",\"decision\":\"$(json_escape "$decision")\",\"requested_runner\":\"$(json_escape "$RUNNER")\",\"resolved_runner\":\"$(json_escape "$resolved_runner")\",\"reason\":\"$(json_escape "$reason")\",\"command_class\":\"$(json_escape "$command_class")\",\"allow_local_fallback\":$(if [[ "$ALLOW_LOCAL_FALLBACK" == "1" ]]; then echo true; else echo false; fi),\"cargo_target_dir\":\"$(json_escape "$CARGO_TARGET_DIR")\",\"tmpdir\":\"$(json_escape "$TMPDIR")\",\"recommended_cargo_target_dir\":\"$(json_escape "$recommended_target_dir")\",\"recommended_tmpdir\":\"$(json_escape "$recommended_tmpdir")\",\"storage_remediation\":{\"cargo_target_dir\":\"$(json_escape "$target_remediation")\",\"tmpdir\":\"$(json_escape "$tmpdir_remediation")\"},\"cargo_command\":\"$(json_escape "$command_text")\",\"rch_detail\":\"$(json_escape "$rch_detail")\"}"

    echo "$json"
    if [[ -n "$DECISION_JSON_PATH" ]]; then
        printf '%s\n' "$json" > "$DECISION_JSON_PATH"
    fi
}

check_rch_health() {
    if ! command -v rch >/dev/null 2>&1; then
        RCH_DETAIL="rch_not_found"
        return 1
    fi
    if RCH_DETAIL="$(rch check --quiet 2>&1)"; then
        RCH_DETAIL="rch_check_ok"
        return 0
    fi
    if [[ -z "$RCH_DETAIL" ]]; then
        RCH_DETAIL="rch_check_failed"
    fi
    return 1
}

if [[ -n "$TARGET_OVERRIDE" ]]; then
    export CARGO_TARGET_DIR="$TARGET_OVERRIDE"
elif [[ -z "${CARGO_TARGET_DIR:-}" ]]; then
    export CARGO_TARGET_DIR="$BUILD_ROOT/$(safe_agent_suffix)/target"
fi

if [[ -n "$TMPDIR_OVERRIDE" ]]; then
    export TMPDIR="$TMPDIR_OVERRIDE"
elif [[ -z "${TMPDIR:-}" ]]; then
    export TMPDIR="$BUILD_ROOT/$(safe_agent_suffix)/tmp"
fi

TARGET_CANDIDATE="$(candidate_path "$CARGO_TARGET_DIR")"
case "$TARGET_CANDIDATE" in
    "$PROJECT_ROOT"/*)
        if [[ "${PI_CARGO_ALLOW_REPO_TARGET:-0}" != "1" ]]; then
            die "CARGO_TARGET_DIR is under the repo root ($TARGET_CANDIDATE). Use /data/tmp or set PI_CARGO_ALLOW_REPO_TARGET=1 explicitly."
        fi
        ;;
esac

case "$TARGET_CANDIDATE" in
    "$PROJECT_ROOT"/bd-*|"$PROJECT_ROOT"/bd-*/*)
        die "refusing bead-named repo-root target dir '$TARGET_CANDIDATE'; use an absolute off-repo CARGO_TARGET_DIR"
        ;;
esac

TMPDIR_CANDIDATE="$(candidate_path "$TMPDIR")"
case "$TMPDIR_CANDIDATE" in
    "$PROJECT_ROOT"/*)
        if [[ "${PI_CARGO_ALLOW_REPO_TARGET:-0}" != "1" ]]; then
            die "TMPDIR is under the repo root ($TMPDIR_CANDIDATE). Use /data/tmp or set PI_CARGO_ALLOW_REPO_TARGET=1 explicitly."
        fi
        ;;
esac

CARGO_TARGET_DIR="$(resolve_dir "$CARGO_TARGET_DIR")"
TMPDIR="$(resolve_dir "$TMPDIR")"
export CARGO_TARGET_DIR TMPDIR

case "$CARGO_TARGET_DIR" in
    "$PROJECT_ROOT"/*)
        if [[ "${PI_CARGO_ALLOW_REPO_TARGET:-0}" != "1" ]]; then
            die "CARGO_TARGET_DIR is under the repo root ($CARGO_TARGET_DIR). Use /data/tmp or set PI_CARGO_ALLOW_REPO_TARGET=1 explicitly."
        fi
        ;;
esac

case "$CARGO_TARGET_DIR" in
    "$PROJECT_ROOT"/bd-*|"$PROJECT_ROOT"/bd-*/*)
        die "refusing bead-named repo-root target dir '$CARGO_TARGET_DIR'; use an absolute off-repo CARGO_TARGET_DIR"
        ;;
esac

write_cache_tag "$CARGO_TARGET_DIR"

HEADROOM_OK=1
HEADROOM_FAILURES=()

probe_headroom() {
    local label="$1"
    local path="$2"
    local disk_row avail_kb mount_point avail_mb inode_used_pct inode_free_pct

    disk_row="$(df -Pk "$path" | awk 'NR==2 {print $4 "|" $6}')"
    [[ -n "$disk_row" ]] || die "unable to read disk stats for $label path '$path'"

    avail_kb="${disk_row%%|*}"
    mount_point="${disk_row#*|}"
    avail_mb=$((avail_kb / 1024))

    inode_used_pct="$(df -Pi "$path" | awk 'NR==2 {gsub(/%/, "", $5); print $5}')"
    [[ -n "$inode_used_pct" ]] || inode_used_pct=100
    inode_free_pct=$((100 - inode_used_pct))

    echo "[cargo-headroom] $label mount=$mount_point free=${avail_mb}MB inode_free=${inode_free_pct}% path=$path"

    if (( avail_mb < MIN_FREE_MB )); then
        HEADROOM_FAILURES+=("$label mount '$mount_point' has ${avail_mb}MB free (< ${MIN_FREE_MB}MB required)")
        return 1
    fi
    if (( inode_free_pct < MIN_INODE_FREE_PCT )); then
        HEADROOM_FAILURES+=("$label mount '$mount_point' has ${inode_free_pct}% free inodes (< ${MIN_INODE_FREE_PCT}% required)")
        return 1
    fi
}

probe_headroom "cargo_target" "$CARGO_TARGET_DIR" || HEADROOM_OK=0
probe_headroom "tmp" "$TMPDIR" || HEADROOM_OK=0

echo "[cargo-headroom] runner=$RUNNER cargo_target=$CARGO_TARGET_DIR tmp=$TMPDIR"

if (( HEADROOM_OK == 0 )); then
    emit_admission_decision \
        "backoff" \
        "none" \
        "insufficient_headroom" \
        "blocked" \
        "$(cargo_command_string "${HEADROOM_FAILURES[@]}")" \
        "$@"
    printf '[cargo-headroom] ERROR: %s\n' "${HEADROOM_FAILURES[@]}" >&2
    exit 2
fi

run_with_rch() {
    exec env \
        RCH_FORCE_REMOTE="${RCH_FORCE_REMOTE:-true}" \
        CARGO_TARGET_DIR="$CARGO_TARGET_DIR" \
        TMPDIR="$TMPDIR" \
        rch exec -- cargo "$@"
}

run_local_cargo() {
    exec env CARGO_TARGET_DIR="$CARGO_TARGET_DIR" TMPDIR="$TMPDIR" cargo "$@"
}

COMMAND_CLASS="heavy"
if is_safe_local_command "$@"; then
    COMMAND_CLASS="safe_local"
fi

case "$RUNNER" in
    rch)
        if ! check_rch_health; then
            emit_admission_decision "backoff" "none" "rch_unavailable" "$COMMAND_CLASS" "$RCH_DETAIL" "$@"
            exit 2
        fi
        emit_admission_decision "allow" "rch" "rch_available" "$COMMAND_CLASS" "$RCH_DETAIL" "$@"
        if (( ADMIT_ONLY == 1 )); then
            exit 0
        fi
        run_with_rch "$@"
        ;;
    auto)
        if check_rch_health; then
            emit_admission_decision "allow" "rch" "rch_available" "$COMMAND_CLASS" "$RCH_DETAIL" "$@"
            if (( ADMIT_ONLY == 1 )); then
                exit 0
            fi
            run_with_rch "$@"
        fi
        if [[ "$COMMAND_CLASS" == "safe_local" ]]; then
            emit_admission_decision "degraded" "local" "safe_local_command" "$COMMAND_CLASS" "$RCH_DETAIL" "$@"
            if (( ADMIT_ONLY == 1 )); then
                exit 0
            fi
            run_local_cargo "$@"
        fi
        if [[ "$ALLOW_LOCAL_FALLBACK" == "1" ]]; then
            emit_admission_decision "degraded" "local" "explicit_local_fallback" "$COMMAND_CLASS" "$RCH_DETAIL" "$@"
            if (( ADMIT_ONLY == 1 )); then
                exit 0
            fi
            run_local_cargo "$@"
        fi
        emit_admission_decision "backoff" "none" "rch_unavailable" "$COMMAND_CLASS" "$RCH_DETAIL" "$@"
        exit 2
        ;;
    local)
        emit_admission_decision "allow" "local" "explicit_local_runner" "$COMMAND_CLASS" "not_checked" "$@"
        if (( ADMIT_ONLY == 1 )); then
            exit 0
        fi
        run_local_cargo "$@"
        ;;
esac

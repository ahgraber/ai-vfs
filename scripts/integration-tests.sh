#!/usr/bin/env bash
# shellcheck disable=SC2310,SC2312,SC2329
# SC2329: cleanup/stack_down/stop_vm are invoked via trap, which shellcheck
#         cannot trace. SC2310/SC2312: probe helpers are deliberately used in
#         condition contexts; they contain only [[ ]]/command -v and are safe
#         with errexit disabled in that scope.
#
# Script: integration-tests.sh
# Summary: Spin up the integration-test stack, run pytest, then tear down.
#
# Usage:
#   scripts/integration-tests.sh [--init] [--keep-up] [--stop-vm] [--help] [-- <pytest-args>...]
#
# Quick-start:
#   scripts/integration-tests.sh                     # run all integration tests
#   scripts/integration-tests.sh -- -k s3            # only S3 tests
#   scripts/integration-tests.sh --keep-up           # leave containers running after
#   scripts/integration-tests.sh --init              # allow podman machine init if missing
#   scripts/integration-tests.sh --stop-vm           # also stop VM after teardown
#
# Notes:
# - Target shell: bash (Bash-first mode; arrays, [[ ]], (( )) are used)
# - ShellCheck: shellcheck -x scripts/integration-tests.sh
# - Does NOT require Nix; works in any shell that has podman-or-docker + uv.
# - Engine resolution mirrors flake.nix containerShellHook order (podman → colima → docker).

set -o errexit
set -o nounset
set -o pipefail

# ERR trap: print the failing command and line number so set -e never dies silently.
# Probes inside if-conditions do NOT trigger ERR (bash does not fire ERR for
# commands used as a condition), so intentional non-fatal probes are unaffected.
# Uses printf directly (not log_error) so it works before function definitions.
trap 'printf "[ERROR] command failed at line %s: %s\n" "${LINENO}" "${BASH_COMMAND}" >&2' ERR

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/tests/integration/docker-compose.yaml"

# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------
FLAG_INIT=0       # allow `podman machine init` if no machine exists
FLAG_KEEP_UP=0    # do not tear down the stack on exit
FLAG_STOP_VM=0    # stop the VM (podman machine/colima) on exit
PYTEST_ARGS=()    # extra args forwarded to pytest after --

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
log_info()  { printf '[INFO]  %s\n' "$*" >&2; }
log_warn()  { printf '[WARN]  %s\n' "$*" >&2; }
log_error() { printf '[ERROR] %s\n' "$*" >&2; }
die()       { log_error "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
  cat <<USAGE
Usage: ${SCRIPT_NAME} [OPTIONS] [-- <pytest-args>...]

Spin up the ai-vfs integration stack (Postgres, Mongo, MinIO), run the
integration tests, and tear down the stack.  Everything after '--' is
forwarded to pytest verbatim.

Options:
  --init      Allow 'podman machine init' when no Podman VM exists.
              Without this flag the script prints guidance and exits.
  --keep-up   Leave containers running after the test run (skip 'down -v').
  --stop-vm   After teardown, also stop the container VM
              (podman machine stop, or colima stop).  Ignored on Linux.
  --help      Show this message and exit.

Examples:
  ${SCRIPT_NAME}
  ${SCRIPT_NAME} -- -k s3_blob
  ${SCRIPT_NAME} --keep-up -- -x -v
  ${SCRIPT_NAME} --init --stop-vm
USAGE
}

# ---------------------------------------------------------------------------
# Argument parsing  (long-opts only; '--' separates pytest args)
# ---------------------------------------------------------------------------
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --init)     FLAG_INIT=1; shift ;;
      --keep-up)  FLAG_KEEP_UP=1; shift ;;
      --stop-vm)  FLAG_STOP_VM=1; shift ;;
      --help|-h)  usage; exit 0 ;;
      --)
        shift
        PYTEST_ARGS=("$@")
        return 0
        ;;
      *)
        log_error "Unknown option: $1"
        usage
        exit 2
        ;;
    esac
  done
}

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------
_uname_s() { uname -s; }
is_darwin() { [[ "$(_uname_s)" == "Darwin" ]]; }

# ---------------------------------------------------------------------------
# Engine / compose resolution
# ---------------------------------------------------------------------------
# These are set by resolve_engine:
ENGINE=""      # "podman" | "docker"
COMPOSE=""     # full compose command string (array stored below)
VM_TYPE=""     # "podman" | "colima" | "" (for --stop-vm)
COMPOSE_CMD=() # array form used for all invocations

# Discover the podman machine socket path via 'podman machine inspect'.
# Returns an empty string when podman is absent, the machine does not exist,
# or the inspect output is empty (machine stopped — socket field is null).
_podman_sock_path() {
  local path
  path="$(podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}' 2>/dev/null || true)"
  # Strip whitespace; path is empty when the machine is stopped.
  path="${path// /}"
  printf '%s' "${path}"
}

# Check if a podman machine is currently running (socket present + inspect ok).
_podman_machine_running() {
  local sock
  sock="$(_podman_sock_path)"
  [[ -n "${sock}" ]] && [[ -S "${sock}" ]]
}

# Export socket vars for a running podman machine.
_use_podman_socket() {
  local sock
  sock="$(_podman_sock_path)"
  if [[ -z "${sock}" ]]; then
    die "podman machine socket path is empty — machine may not be running."
  fi
  export DOCKER_HOST="unix://${sock}"
  export CONTAINER_HOST="unix://${sock}"
  log_info "Using podman machine socket: ${DOCKER_HOST}"
}

# Check if colima's default docker socket exists and colima is installed.
_colima_socket() { printf '%s' "${HOME}/.colima/default/docker.sock"; }
_colima_running() {
  local csock
  csock="$(_colima_socket)"
  command -v colima >/dev/null 2>&1 \
    && [[ -S "${csock}" ]]
}

# Export socket for colima.
_use_colima_socket() {
  local sock
  sock="$(_colima_socket)"
  export DOCKER_HOST="unix://${sock}"
  log_info "Using colima docker socket: ${DOCKER_HOST}"
}

# Resolve which compose subcommand or binary is available for an engine.
_resolve_compose_cmd() {
  local engine="$1"   # "podman" or "docker"
  if [[ "${engine}" == "podman" ]]; then
    if podman compose version >/dev/null 2>&1; then
      COMPOSE_CMD=(podman compose)
    elif command -v podman-compose >/dev/null 2>&1; then
      COMPOSE_CMD=(podman-compose)
    elif command -v docker-compose >/dev/null 2>&1; then
      # docker-compose also works against a podman socket
      COMPOSE_CMD=(docker-compose)
    else
      die "No compose command found. Install docker-compose or podman-compose."
    fi
  else
    # docker — prefer standalone binaries when the docker CLI is absent (e.g.
    # colima running but docker client not on PATH outside the devshell).
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
      COMPOSE_CMD=(docker compose)
    elif command -v docker-compose >/dev/null 2>&1; then
      COMPOSE_CMD=(docker-compose)
    elif command -v podman-compose >/dev/null 2>&1; then
      COMPOSE_CMD=(podman-compose)
    else
      die "No compose command found. Install 'docker compose' plugin or docker-compose."
    fi
  fi
  COMPOSE="${COMPOSE_CMD[*]}"
  log_info "Compose command: ${COMPOSE}"
}

resolve_engine() {
  # Step 0: honour pre-set socket env vars (mirrors flake.nix: "if already set, skip").
  if [[ -n "${DOCKER_HOST:-}" || -n "${CONTAINER_HOST:-}" ]]; then
    log_info "DOCKER_HOST/CONTAINER_HOST already set; using pre-configured engine."
    # Determine ENGINE for compose selection heuristic.
    local socket="${DOCKER_HOST:-${CONTAINER_HOST:-}}"
    if [[ "${socket}" == *podman* ]]; then
      ENGINE="podman"
    else
      ENGINE="docker"
    fi
    _resolve_compose_cmd "${ENGINE}"
    return 0
  fi

  # On Linux the daemon is assumed local; skip VM logic entirely.
  if ! is_darwin; then
    log_info "Linux: using local container daemon."
    if command -v podman >/dev/null 2>&1; then
      ENGINE="podman"
    elif command -v docker >/dev/null 2>&1; then
      ENGINE="docker"
    else
      die "No container engine found. Install podman or docker."
    fi
    _resolve_compose_cmd "${ENGINE}"
    return 0
  fi

  # macOS: mirror flake.nix shellHook resolution order.

  # Early hint: if neither podman nor docker is on PATH but nix is, the user is
  # likely outside the devshell — surface that before attempting anything else.
  if ! command -v podman >/dev/null 2>&1 && ! command -v docker >/dev/null 2>&1 \
      && command -v nix >/dev/null 2>&1; then
    log_warn "Neither podman nor docker found on PATH, but nix is installed."
    log_warn "You are likely outside the project devshell. Run: nix develop"
    log_warn "(Continuing to check for other engines...)"
  fi

  # Step 1: running podman machine socket.
  if _podman_machine_running; then
    ENGINE="podman"
    VM_TYPE="podman"
    _use_podman_socket
    _resolve_compose_cmd "${ENGINE}"
    return 0
  fi

  # Step 2: podman installed but machine exists-but-stopped, or no machine at all.
  if command -v podman >/dev/null 2>&1; then
    ENGINE="podman"
    VM_TYPE="podman"
    if podman machine inspect >/dev/null 2>&1; then
      # Machine exists but is not running — start it.
      log_info "Podman machine is stopped; starting it..."
      podman machine start
      # Re-discover the socket path now that the machine has started, and wait
      # up to 30 s for the socket to appear.  Use i=$((i+1)) (not ((i++))) to
      # avoid a set -e false-exit when i transitions through 0 (the expression
      # value of i++ is the pre-increment value, which is 0/falsy on first pass).
      local i=0
      while ! _podman_machine_running; do
        i=$((i + 1))
        if [[ "${i}" -ge 30 ]]; then
          die "Timed out waiting for podman machine socket after 'podman machine start'."
        fi
        sleep 1
      done
      _use_podman_socket
    else
      # No machine at all.
      if [[ "${FLAG_INIT}" == "1" ]]; then
        log_info "No podman machine found; running 'podman machine init'..."
        podman machine init
        log_info "Starting podman machine..."
        podman machine start
        local i=0
        while ! _podman_machine_running; do
          i=$((i + 1))
          if [[ "${i}" -ge 60 ]]; then
            die "Timed out waiting for podman machine socket after init+start."
          fi
          sleep 1
        done
        _use_podman_socket
      else
        log_info "No podman machine configured."
        log_info "  To create one: podman machine init && podman machine start"
        log_info "  Or rerun with: ${SCRIPT_NAME} --init"
        log_info "  Falling through to check colima..."
        # Fall through: maybe colima is available.
        ENGINE=""
      fi
    fi
    if [[ -n "${ENGINE}" ]]; then
      _resolve_compose_cmd "${ENGINE}"
      return 0
    fi
  fi

  # Step 3: colima docker socket.
  # Colima's docker runtime requires the docker CLI client binary on PATH.
  # If it is absent (e.g. running outside the nix devshell), warn and skip so
  # we can still reach Step 4 or the final-failure message.
  if command -v colima >/dev/null 2>&1; then
    if ! command -v docker >/dev/null 2>&1; then
      log_warn "Colima is installed but the docker CLI client is not on PATH."
      log_warn "Colima's docker runtime requires the docker binary to function."
      log_warn "Enter the nix devshell (nix develop) — it now provides it."
      log_warn "Or: brew install docker"
      log_warn "Skipping colima; falling through to next resolution step..."
    else
      ENGINE="docker"
      VM_TYPE="colima"
      if _colima_running; then
        _use_colima_socket
      else
        log_info "Colima is installed but not running; starting the default profile..."
        colima start
        # Wait for the socket (up to 60 s).
        local i=0
        while ! _colima_running; do
          i=$((i + 1))
          if [[ "${i}" -ge 60 ]]; then
            die "Timed out waiting for colima docker socket after 'colima start'."
          fi
          sleep 1
        done
        _use_colima_socket
      fi
      _resolve_compose_cmd "${ENGINE}"
      return 0
    fi
  fi

  # Step 4: plain docker.
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    ENGINE="docker"
    log_info "Using system docker daemon."
    _resolve_compose_cmd "${ENGINE}"
    return 0
  fi

  # Nothing worked.
  cat >&2 <<SETUP
[ERROR] No working container engine found.

  Recommended: enter the project devshell — it provides podman AND the docker
  client, so both the primary (podman) and fallback (colima) paths work:

      nix develop
      podman machine init    # one-time VM setup (or: ${SCRIPT_NAME} --init)
      podman machine start

  Manual alternatives on macOS:
    Colima (needs docker CLI client on PATH):
      brew install colima docker && colima start

    Docker Desktop:
      https://www.docker.com/products/docker-desktop

  Linux:
    sudo apt-get install docker.io podman   # or equivalent

SETUP
  exit 1
}

# ---------------------------------------------------------------------------
# Stack management
# ---------------------------------------------------------------------------
stack_up() {
  log_info "Starting integration stack..."
  # Purge any stale volumes before bringing services up.
  # POSTGRES_PASSWORD and similar init-time env vars only apply at first initdb; a stale
  # anonymous volume from a previous run with different credentials causes auth mismatches.
  "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" down -v --remove-orphans 2>/dev/null || true
  # Probe for --wait support.
  if "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" up --help 2>&1 | grep -q -- '--wait'; then
    "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" up -d --wait
  else
    "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" up -d
    wait_for_healthy
  fi
  log_info "Stack is up."
}

wait_for_healthy() {
  local timeout=120
  local elapsed=0
  log_info "Waiting for all services to become healthy (timeout ${timeout}s)..."
  while true; do
    # Count services still not in (healthy) state.
    local unhealthy
    unhealthy="$("${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" ps 2>/dev/null \
      | tail -n +2 \
      | { grep -v "(healthy)" || true; } \
      | { grep -v "^$" || true; } \
      | wc -l)"
    unhealthy="${unhealthy// /}"  # trim whitespace
    if [[ "${unhealthy}" == "0" ]]; then
      return 0
    fi
    if (( elapsed >= timeout )); then
      log_error "Services did not become healthy within ${timeout}s."
      "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" ps >&2
      return 1
    fi
    sleep 3
    (( elapsed += 3 ))
  done
}

stack_down() {
  log_info "Tearing down integration stack..."
  "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" down -v || true
}

# ---------------------------------------------------------------------------
# MinIO bucket setup (idempotent)
# ---------------------------------------------------------------------------
setup_minio_bucket() {
  local bucket="${AIVFS_TEST_S3_BUCKET:-aivfs-test}"
  log_info "Setting up MinIO bucket '${bucket}' (idempotent)..."
  # mc alias set is safe to re-run; mc mb -p ignores already-exists.
  "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" exec minio \
    mc alias set local http://localhost:9000 minioadmin minioadmin \
    >/dev/null 2>&1 || true
  "${COMPOSE_CMD[@]}" -f "${COMPOSE_FILE}" exec minio \
    mc mb -p "local/${bucket}" \
    >/dev/null 2>&1 || true
  log_info "MinIO bucket '${bucket}' ready."
}

# ---------------------------------------------------------------------------
# Env var exports (only if not already set, so users can override)
# ---------------------------------------------------------------------------
export_test_env() {
  if [[ -z "${AIVFS_TEST_POSTGRES_DSN:-}" ]]; then
    export AIVFS_TEST_POSTGRES_DSN="postgresql://aivfs:aivfs@localhost:5432/aivfs"
    log_info "Exported AIVFS_TEST_POSTGRES_DSN=${AIVFS_TEST_POSTGRES_DSN}"
  fi
  if [[ -z "${AIVFS_TEST_MONGO_URI:-}" ]]; then
    export AIVFS_TEST_MONGO_URI="mongodb://localhost:27017/aivfs"
    log_info "Exported AIVFS_TEST_MONGO_URI=${AIVFS_TEST_MONGO_URI}"
  fi
  if [[ -z "${AIVFS_TEST_S3_BUCKET:-}" ]]; then
    export AIVFS_TEST_S3_BUCKET="aivfs-test"
    log_info "Exported AIVFS_TEST_S3_BUCKET=${AIVFS_TEST_S3_BUCKET}"
  fi
  if [[ -z "${AWS_ACCESS_KEY_ID:-}" ]]; then
    export AWS_ACCESS_KEY_ID="minioadmin"
    log_info "Exported AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}"
  fi
  if [[ -z "${AWS_SECRET_ACCESS_KEY:-}" ]]; then
    export AWS_SECRET_ACCESS_KEY="minioadmin"
    log_info "Exported AWS_SECRET_ACCESS_KEY=<redacted>"
  fi
  if [[ -z "${AWS_ENDPOINT_URL_S3:-}" ]]; then
    export AWS_ENDPOINT_URL_S3="http://localhost:9000"
    log_info "Exported AWS_ENDPOINT_URL_S3=${AWS_ENDPOINT_URL_S3}"
  fi
  if [[ -z "${AWS_REGION:-}" ]]; then
    export AWS_REGION="us-east-1"
    log_info "Exported AWS_REGION=${AWS_REGION}"
  fi
}

# ---------------------------------------------------------------------------
# VM teardown (--stop-vm, macOS only)
# ---------------------------------------------------------------------------
stop_vm() {
  if ! is_darwin; then return 0; fi
  case "${VM_TYPE}" in
    podman)
      log_info "Stopping podman machine..."
      podman machine stop || true
      ;;
    colima)
      log_info "Stopping colima..."
      colima stop || true
      ;;
    *)
      log_info "--stop-vm: no VM to stop (VM_TYPE='${VM_TYPE}')."
      ;;
  esac
}

# ---------------------------------------------------------------------------
# Cleanup trap
# ---------------------------------------------------------------------------
# COMPOSE_CMD may not yet be populated if we fail during resolve_engine.
# Guard with a check.
cleanup() {
  local status=$?
  if [[ "${FLAG_KEEP_UP}" == "0" && ${#COMPOSE_CMD[@]} -gt 0 ]]; then
    stack_down
  fi
  if [[ "${FLAG_STOP_VM}" == "1" ]]; then
    stop_vm
  fi
  exit "${status}"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  parse_args "$@"

  # Verify compose file exists before doing anything heavy.
  if [[ ! -f "${COMPOSE_FILE}" ]]; then
    die "Compose file not found: ${COMPOSE_FILE}"
  fi

  resolve_engine
  stack_up
  setup_minio_bucket
  export_test_env

  log_info "Running integration tests..."
  # cd to repo root so uv picks up pyproject.toml regardless of caller CWD.
  cd "${REPO_ROOT}"
  pytest_exit=0
  uv run pytest tests/integration -q "${PYTEST_ARGS[@]+"${PYTEST_ARGS[@]}"}" || pytest_exit=$?

  log_info "pytest exited with status ${pytest_exit}."
  # Exit with pytest's exit code; the trap handles cleanup.
  exit "${pytest_exit}"
}

main "$@"

# shellcheck shell=bash
# Bash integration and completion for Agent Dashboard CLI (`agt`)
# Source in ~/.bashrc:
#   source /path/to/agent-dashboard/tracker/interfaces/cli/bash/agent-tracker.bash

agt() {
  local url="${AGENT_DASHBOARD_URL:-${AGENT_TRACKER_URL:-http://127.0.0.1:8080}}"
  local py="${AGENT_TRACKER_PYTHON:-python3}"
  AGENT_TRACKER_URL="${url}" AGENT_DASHBOARD_URL="${url}" "${py}" -m tracker.interfaces.cli "$@"
}

_agt_fetch_session_ids() {
  local base_url="${AGENT_DASHBOARD_URL:-${AGENT_TRACKER_URL:-http://127.0.0.1:8080}}"
  base_url="${base_url%/}"

  local payload=""
  if command -v curl >/dev/null 2>&1; then
    payload="$(curl -s --connect-timeout 1 --max-time 1 "${base_url}/api/sessions" 2>/dev/null || true)"
  fi
  [ -z "${payload}" ] && return 0

  local py="${AGENT_TRACKER_PYTHON:-python3}"
  if command -v "${py}" >/dev/null 2>&1; then
    "${py}" -c 'import json, sys
try:
    sessions = json.load(sys.stdin)
    if isinstance(sessions, list):
        for s in sessions:
            sid = s.get("session_id")
            if sid:
                print(sid)
except Exception:
    pass' <<< "${payload}" 2>/dev/null
  fi
}

_agt_completions() {
  local cur prev subcmd="" i word
  COMPREPLY=()
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev=""
  if [ "${COMP_CWORD}" -gt 0 ]; then
    prev="${COMP_WORDS[COMP_CWORD - 1]}"
  fi

  local subcommands="list tail inspect serve"
  local statuses="INITIALIZING RUNNING WAITING_USER COMPLETED ERROR"

  for ((i = 1; i < COMP_CWORD; i++)); do
    word="${COMP_WORDS[i]}"
    case "${word}" in
      list|tail|inspect|serve)
        subcmd="${word}"
        break
        ;;
    esac
  done

  if [ -z "${subcmd}" ]; then
    local IFS=$' \t\n'
    COMPREPLY=($(compgen -W "${subcommands}" -- "${cur}"))
    return 0
  fi

  case "${prev}" in
    --status)
      local IFS=$' \t\n'
      COMPREPLY=($(compgen -W "${statuses}" -- "${cur}"))
      return 0
      ;;
    --url|--db|--limit|--port|--host|--db-path|--brain-dir|--poll-interval)
      return 0
      ;;
  esac

  case "${subcmd}" in
    list)
      if [[ "${cur}" == -* ]]; then
        COMPREPLY=($(compgen -W "--status --limit --json --url --db --no-color --help" -- "${cur}"))
      fi
      ;;
    tail)
      if [[ "${cur}" == -* ]]; then
        COMPREPLY=($(compgen -W "--no-follow --limit --json --url --db --no-color --help" -- "${cur}"))
      else
        local ids
        ids="$(_agt_fetch_session_ids)"
        local IFS=$'\n'
        COMPREPLY=($(compgen -W "${ids}" -- "${cur}"))
      fi
      ;;
    inspect)
      if [[ "${cur}" == -* ]]; then
        COMPREPLY=($(compgen -W "--limit --json --url --db --no-color --help" -- "${cur}"))
      else
        local ids
        ids="$(_agt_fetch_session_ids)"
        local IFS=$'\n'
        COMPREPLY=($(compgen -W "${ids}" -- "${cur}"))
      fi
      ;;
    serve)
      if [[ "${cur}" == -* ]]; then
        COMPREPLY=($(compgen -W "--port --host --db-path --brain-dir --poll-interval --url --db --no-color --help" -- "${cur}"))
      fi
      ;;
  esac
  return 0
}

complete -F _agt_completions agt

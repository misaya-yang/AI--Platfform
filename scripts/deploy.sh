#!/usr/bin/env bash
#
# deploy.sh — deploy one or more services to the production server.
#
# Wraps today's manual sequence (push → ssh pull → docker build → up → verify)
# into one command. Does NOT hide the steps — everything it runs is echoed first.
#
# Usage:
#   scripts/deploy.sh <service> [<service> …]
#
# Examples:
#   scripts/deploy.sh gateway
#   scripts/deploy.sh gateway assistant-service
#   scripts/deploy.sh mcp-docgen-server gateway          # MCP server + gateway together
#   scripts/deploy.sh --dry-run gateway                  # print what would happen, don't run
#
# Read DEPLOY.md and the memory file reference_server_deployment.md first.
# This script is a convenience, not a replacement for understanding what
# you're deploying.

set -euo pipefail

# --------------------------------------------------------------------------
# Config — hardcoded because this script only talks to one server
# --------------------------------------------------------------------------

SSH_KEY="${AI_GATEWAY_SSH_KEY:-$HOME/Desktop/密钥/ai-test.pem}"
SSH_HOST="${AI_GATEWAY_HOST:-ubuntu@52.65.136.42}"
REMOTE_REPO="/opt/deploy/ai-gateway"
REMOTE_COMPOSE_DIR="/opt/deploy"
BRANCH="${AI_GATEWAY_BRANCH:-dev}"
LOCAL_REMOTE="${AI_GATEWAY_LOCAL_REMOTE:-gitlab}"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
  shift
fi

if [[ $# -eq 0 ]]; then
  cat >&2 <<'EOF'
Error: no services specified.

Usage: scripts/deploy.sh [--dry-run] <service> [<service> …]

Common service lists:
  gateway                                         # Block 1 only
  assistant-service                               # Block 2
  mcp-docgen-server                               # Block 3
  gateway assistant-service                       # code under src/services/assistant/
  gateway frontend                                # code + UI bundle

Full service names: see docker-compose.yml.
EOF
  exit 2
fi

SERVICES="$*"

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

run() {
  printf '\033[90m$ %s\033[0m\n' "$*"
  if [[ $DRY_RUN -eq 0 ]]; then
    eval "$@"
  fi
}

remote() {
  # Execute on the server. Pass the command as a single string.
  run ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no "$SSH_HOST" "\"$*\""
}

heading() {
  printf '\n\033[1;34m── %s ──\033[0m\n' "$*"
}

# --------------------------------------------------------------------------
# Pre-flight
# --------------------------------------------------------------------------

heading "Pre-flight checks"

# Local: uncommitted changes?
if [[ -n "$(git status --porcelain)" ]]; then
  printf '\033[1;33m! local working tree is dirty — commit or stash before deploying:\033[0m\n' >&2
  git status --short >&2
  exit 1
fi

# Local: current branch
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "$BRANCH" ]]; then
  printf '\033[1;33m! on branch "%s", deploy branch is "%s" — switch or override AI_GATEWAY_BRANCH.\033[0m\n' \
    "$CURRENT_BRANCH" "$BRANCH" >&2
  exit 1
fi

# --------------------------------------------------------------------------
# 0. Push
# --------------------------------------------------------------------------

heading "Push to $LOCAL_REMOTE/$BRANCH"
run git push "$LOCAL_REMOTE" "$BRANCH"

# --------------------------------------------------------------------------
# 1. Server: pull + check working tree
# --------------------------------------------------------------------------

heading "Server: pull latest code"
remote "cd $REMOTE_REPO && \
        DIRTY=\$(git status --porcelain | head -1); \
        if [ -n \"\$DIRTY\" ]; then \
          echo '⚠  server working tree has WIP — stashing as wip-deploy-\$(date +%Y%m%d-%H%M)'; \
          git stash push -u -m wip-deploy-\$(date +%Y%m%d-%H%M); \
        fi; \
        git pull origin $BRANCH"

# --------------------------------------------------------------------------
# 2. Server: build
# --------------------------------------------------------------------------

heading "Server: build [$SERVICES]"
remote "cd $REMOTE_COMPOSE_DIR && docker compose build $SERVICES"

# --------------------------------------------------------------------------
# 3. Server: up
# --------------------------------------------------------------------------

heading "Server: up -d [$SERVICES]"
remote "cd $REMOTE_COMPOSE_DIR && docker compose up -d $SERVICES"

# --------------------------------------------------------------------------
# 4. Verify
# --------------------------------------------------------------------------

heading "Post-deploy verification"

# Wait a little for healthchecks to tick.
[[ $DRY_RUN -eq 0 ]] && sleep 8

remote "sudo systemctl is-active nginx"
remote "curl -sf http://127.0.0.1:8080/health"
remote "cd $REMOTE_COMPOSE_DIR && docker compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}' | grep -E '$(printf '%s|' $SERVICES)frontend|gateway' | head -20"

printf '\n\033[1;32m✓ deploy complete\033[0m\n'
printf 'Next: smoke-test the feature via https://gateway.hejazfs.com.au\n'

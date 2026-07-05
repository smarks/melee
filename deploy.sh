#!/bin/bash
# Blue-Green Deployment Script for Melee
#
# Usage: ./deploy.sh [--force] | ./deploy.sh rollback | ./deploy.sh status
#   --force: deploy even if no new commits are detected
#
# Mirrors the house blue/green interface used by the other origamisoftware.com
# apps (orge, tarmar-studio). Melee is a plain pip/venv Django app on sqlite, so
# this is the lighter-weight variant:
#   1. Pull latest code
#   2. Sync the venv from requirements.txt
#   3. Run the test suite
#   4. Migrate + collect static
#   5. Restart the inactive (blue or green) systemd unit and health-check it
#   6. Flip the nginx upstream to the new environment
#   7. Leave the old environment running for instant rollback

set -e

APP_DIR="/home/sam/dev/melee"

BLUE_PORT=9072
GREEN_PORT=9073
VENV="/home/sam/dev/melee/.venv"
UPSTREAM_CONF="/etc/nginx/conf.d/melee-upstream.conf"
STATE_FILE="/home/sam/dev/melee/.deploy-state"

# A substring that only the real, working homepage renders. Django's generic
# production error pages (DEBUG=False) use "<title>Server Error (500)</title>"
# / "<title>Not Found</title>", so requiring "<title>Melee" proves a view
# actually ran and rendered the board template rather than erroring out.
HEALTH_MARKER='<title>Melee'

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[DEPLOY]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

get_active() {
    if [ -f "$STATE_FILE" ]; then
        cat "$STATE_FILE"
    else
        echo "blue"
    fi
}

get_inactive() {
    if [ "$(get_active)" = "blue" ]; then
        echo "green"
    else
        echo "blue"
    fi
}

get_port() {
    if [ "$1" = "blue" ]; then
        echo "$BLUE_PORT"
    else
        echo "$GREEN_PORT"
    fi
}

# One health probe against an environment's local port. Returns 0 ONLY if the
# app serves a genuinely working page: final HTTP status 200 AND the homepage
# marker in the body.
#
# Two headers matter (#306):
#   Host: melee.origamisoftware.com  - DJANGO_ALLOWED_HOSTS is that host, so a
#     bare 127.0.0.1 request returns 400 (DisallowedHost) and never passes.
#   X-Forwarded-Proto: https         - production sets SECURE_SSL_REDIRECT=True
#     with SECURE_PROXY_SSL_HEADER=(HTTP_X_FORWARDED_PROTO, https). nginx adds
#     this header on real traffic; without it SecurityMiddleware 301-redirects
#     to https BEFORE any view/DB/template runs. The old probe used `curl -sf`
#     with no header, and `-f` treats that 301 as success - so a deploy that
#     boots gunicorn but 500s at request time (prod-only config error, dead DB,
#     bad data) passed the check and took traffic. Sending the header makes the
#     request "secure", so the actual index view runs and must return a 200.
#
# We deliberately do NOT follow redirects (-L): the redirect target is
# https://melee.origamisoftware.com/, which is the PUBLIC (currently active)
# site, not this inactive port. Following it would probe production and mask a
# broken new environment. A healthy env returns 200 directly given the header;
# anything that still redirects is treated as unhealthy.
probe_once() {
    local port=$1
    local response status body

    # Capture body + HTTP status in one request.
    response=$(curl -sS \
        -H "Host: melee.origamisoftware.com" \
        -H "X-Forwarded-Proto: https" \
        -w '\n%{http_code}' \
        "http://127.0.0.1:$port/" 2>/dev/null) || return 1

    status=${response##*$'\n'}
    body=${response%$'\n'*}

    [ "$status" = "200" ] || return 1
    case "$body" in
        *"$HEALTH_MARKER"*) return 0 ;;
        *) return 1 ;;
    esac
}

# Bring up an environment and confirm it actually serves a working page before
# we trust it. Retries because gunicorn needs a moment to bind after restart.
verify_environment() {
    local env=$1
    local port
    port=$(get_port "$env")
    local max_attempts=10
    local attempt=1

    log "Waiting for $env environment on port $port..."
    while [ $attempt -le $max_attempts ]; do
        if probe_once "$port"; then
            log "$env environment is serving HTTP 200 with expected content on port $port."
            return 0
        fi
        log "Attempt $attempt/$max_attempts - waiting..."
        sleep 2
        attempt=$((attempt + 1))
    done

    error "$env environment failed the health check (no HTTP 200 with expected content) on port $port"
}

# Point the nginx upstream at the given environment and reload.
switch_to() {
    local env=$1
    local port
    port=$(get_port "$env")

    log "Switching traffic to $env (port $port)..."
    sudo tee "$UPSTREAM_CONF" > /dev/null << EOF
# Active backend for Melee blue-green deployment.
# Managed by deploy.sh - do not edit manually.
# Current: $env (port $port)
upstream melee_backend {
    server 127.0.0.1:$port;
}
EOF

    sudo nginx -t || error "Nginx config test failed!"
    sudo systemctl reload nginx
    echo "$env" > "$STATE_FILE"
    log "Traffic now routed to $env"
}

main() {
    cd "$APP_DIR"

    local force=false
    [ "$1" = "--force" ] && force=true

    local active inactive
    active=$(get_active)
    inactive=$(get_inactive)

    log "Current active: $active"
    log "Will deploy to: $inactive"
    echo ""

    log "Fetching from origin..."
    git fetch

    local local_rev remote_rev
    local_rev=$(git rev-parse HEAD)
    remote_rev=$(git rev-parse origin/main)

    if [ "$local_rev" = "$remote_rev" ] && [ "$force" = false ]; then
        log "Already up to date. Use --force to redeploy anyway."
        exit 0
    fi

    if [ "$local_rev" != "$remote_rev" ]; then
        log "Changes detected - pulling..."
        git pull
    fi

    # Create the venv on first deploy, then keep it in sync with requirements.
    if [ ! -x "$VENV/bin/python" ]; then
        log "Creating virtualenv at $VENV..."
        python3 -m venv "$VENV"
    fi
    log "Syncing dependencies..."
    "$VENV/bin/pip" install --upgrade pip > /dev/null
    "$VENV/bin/pip" install -r requirements.txt

    log "Running tests..."
    "$VENV/bin/pytest" -q
    log "Tests passed!"
    echo ""

    # .env supplies DJANGO_SECRET_KEY / DJANGO_DEBUG=0 / DJANGO_ALLOWED_HOSTS.
    if [ -f .env ]; then
        log "Loading .env..."
        set -a
        # shellcheck disable=SC1091
        source .env
        set +a
    else
        warn ".env not found - relying on systemd EnvironmentFile at runtime."
    fi

    log "Running migrations..."
    "$VENV/bin/python" manage.py migrate --noinput

    log "Collecting static files..."
    "$VENV/bin/python" manage.py collectstatic --noinput

    log "Deploying to $inactive environment..."
    sudo cp "melee-$inactive.service" "/etc/systemd/system/melee-$inactive.service"
    sudo systemctl daemon-reload
    sudo systemctl restart "melee-$inactive"

    sleep 3
    verify_environment "$inactive"
    switch_to "$inactive"

    echo ""
    log "Deployment complete!"
    log "Active environment: $inactive"
    log "Rollback available: $active (run: ./deploy.sh rollback)"
}

rollback() {
    local active inactive
    active=$(get_active)
    inactive=$(get_inactive)

    log "Rolling back from $active to $inactive..."
    verify_environment "$inactive"
    switch_to "$inactive"
    log "Rollback complete! Now running on $inactive"
}

status() {
    local active inactive
    active=$(get_active)
    inactive=$(get_inactive)

    echo "Melee Blue-Green Deployment Status"
    echo "=================================="
    echo ""
    echo "Active:   $active (port $(get_port "$active"))"
    echo "Standby:  $inactive (port $(get_port "$inactive"))"
    echo ""
    echo "Service status:"
    systemctl is-active "melee-blue" 2>/dev/null && echo "  melee-blue: running" || echo "  melee-blue: stopped"
    systemctl is-active "melee-green" 2>/dev/null && echo "  melee-green: running" || echo "  melee-green: stopped"
}

# Only dispatch when executed directly. When sourced (e.g. by the health-check
# test suite) we just want the function definitions, not a live deploy.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    case "${1:-}" in
        rollback) rollback ;;
        status) status ;;
        *) main "$@" ;;
    esac
fi

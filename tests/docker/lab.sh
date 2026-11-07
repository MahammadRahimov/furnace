#!/usr/bin/env bash
# Furnace Docker test lab: runs the README steps for docker/docker-compose.lab.yml exactly as a user
# would (one host, leader + follower + client), then checks replication and a failover.
# Only documentation addresses are used (203.0.113.0/24). Nothing real is touched.
#
#   tests/docker/lab.sh            build furnace:0.10.0 from this repository and run the lab test
#   KEEP=1 tests/docker/lab.sh     leave the lab running afterwards
#
# Needs: docker with the compose plugin.
set -Eeuo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: do not rewrite /paths

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROOT_D="$(cd "$ROOT" && (pwd -W 2>/dev/null || pwd))"   # the path docker sees (Windows path in Git Bash)
IMAGE=furnace:0.10.0
LAB=(docker compose -f "$ROOT_D/docker/docker-compose.lab.yml")
SHARED_IP=203.0.113.10
WORK="$(mktemp -d)"
PASSED=0
FAILED=0

step() { printf '\n### %s\n' "$*"; }
pass() { printf '  PASS  %s\n' "$*"; PASSED=$((PASSED + 1)); }
fail() { printf '  FAIL  %s\n' "$*"; FAILED=$((FAILED + 1)); }
check() { local name="$1"; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
wait_for() {  # wait_for SECONDS COMMAND...: until the command succeeds
    local seconds="$1" _
    shift
    for _ in $(seq 1 "$seconds"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
    return 1
}
ready() { docker logs "$1" 2>&1 | grep -q "furnace: Ready."; }
owns_shared_ip() { docker exec "$1" ip -4 addr show eth0 | grep -q "inet $SHARED_IP/"; }
client() { "${LAB[@]}" exec -T client "$@"; }
edl() { client curl -sS --max-time 10 -k -u "paloalto:$EDL_PW" "https://$SHARED_IP/edl/$1"; }
api() {  # api METHOD PATH JSON -> "BODY HTTP_CODE"
    client curl -sS --max-time 10 -k -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
        -X "$1" -d "$3" -w ' %{http_code}' "https://$SHARED_IP$2" | tr -d '\n'
}

teardown() {
    rm -rf "$WORK"
    if [ "${KEEP:-0}" = "1" ]; then
        echo "KEEP=1: the lab is still running. Remove it with: docker compose -f docker/docker-compose.lab.yml down -v"
        return
    fi
    "${LAB[@]}" down -v >/dev/null 2>&1 || true
}
trap teardown EXIT

step "Build the image furnace:0.10.0"
docker build -q -f "$ROOT_D/docker/Dockerfile" --build-arg VERSION=0.10.0 -t "$IMAGE" "$ROOT_D" >/dev/null
check "the image was built" bash -c 'docker image inspect "$1" >/dev/null' _ "$IMAGE"
"${LAB[@]}" down -v >/dev/null 2>&1 || true

step "README step: start the leader"
"${LAB[@]}" up -d leader client >/dev/null 2>&1
check "the leader is ready" wait_for 90 ready furnace-leader
check "the leader owns the shared IP" wait_for 20 owns_shared_ip furnace-leader
check "the follower bundle is there" docker exec furnace-leader test -f /etc/furnace/furnace-peer-bundle.tar.gz

step "README step: copy the bundle to the follower, then start it"
"${LAB[@]}" create follower >/dev/null 2>&1
(cd "$WORK" && docker cp furnace-leader:/etc/furnace/furnace-peer-bundle.tar.gz bundle.tar.gz &&
    docker cp bundle.tar.gz furnace-follower:/etc/furnace/furnace-peer-bundle.tar.gz)
"${LAB[@]}" up -d >/dev/null 2>&1
rm -f "$WORK/bundle.tar.gz"
check "the follower is ready" wait_for 90 ready furnace-follower
check "it took the secrets from the bundle" bash -c 'docker logs furnace-follower 2>&1 | grep -q "taken from the leader.s bundle"'
check "the follower does not own the shared IP" bash -c '! docker exec furnace-follower ip -4 addr show eth0 | grep -q "inet $1/"' _ "$SHARED_IP"

step "README step: the first token and the list login"
out="$(docker exec furnace-leader furnace token add soar --allow all --expires-days 0)"
TOKEN="$(grep -o 'fur_[A-Za-z0-9_-]*' <<< "$out" | head -n1)"
out="$(docker exec furnace-leader furnace edl-user add paloalto)"
EDL_PW="$(tail -n 3 <<< "$out" | tr -d ' \r' | grep -E '^[A-Za-z0-9_-]{24,}$' | head -n1)"
check "a token and a list password were made" bash -c '[[ "$1" == fur_* && ${#2} -ge 24 ]]' _ "$TOKEN" "$EDL_PW"

step "README step: try it from the client box"
check "the empty list downloads through the shared IP with the login" client curl -fsS -k -o /dev/null -u "paloalto:$EDL_PW" "https://$SHARED_IP/edl/blocklist.txt"
check "/health answers only the servers themselves (403 for the client)" bash -c '[ "$("${@}" curl -s -k -o /dev/null -w "%{http_code}" "https://203.0.113.10/health")" = 403 ]' _ "${LAB[@]}" exec -T client
r="$(api POST /api/v1/ips '{"ip":"198.51.100.7","reason":"lab test"}')"
check "the client blocks an IP (201)" grep -q ' 201$' <<< "$r"
check "the IP is in blocklist.txt" grep -qx 198.51.100.7 <<< "$(edl blocklist.txt)"
check "the follower gets it within 60 seconds" \
    wait_for 60 docker exec furnace-follower grep -qx 198.51.100.7 /var/lib/furnace-edl/blocklist.txt

step "README step: failover"
docker stop furnace-leader >/dev/null
check "the follower takes the shared IP" wait_for 20 owns_shared_ip furnace-follower
check "the list still works through the shared IP" grep -qx 198.51.100.7 <<< "$(edl blocklist.txt)"
r="$(api POST /api/v1/ips '{"ip":"198.51.100.8","reason":"after failover"}')"
check "the client can still block IPs (201)" grep -q ' 201$' <<< "$r"
docker start furnace-leader >/dev/null
check "the old leader comes back and gets the new IP within 90 seconds" \
    wait_for 90 docker exec furnace-leader grep -qx 198.51.100.8 /var/lib/furnace-edl/blocklist.txt
check "it does not take the shared IP back" bash -c '! docker exec furnace-leader ip -4 addr show eth0 | grep -q "inet $1/"' _ "$SHARED_IP"

printf '\n%s passed, %s failed (docker lab)\n' "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ]

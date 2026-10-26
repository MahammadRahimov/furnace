#!/usr/bin/env bash
# Furnace integration test with Docker: two servers, a fake firewall and a fake XSOAR.
# Only documentation addresses are used (192.0.2.0/24). Nothing real is touched.
#
#   tests/integration/run.sh [22.04|24.04]     run the test (default 24.04)
#   KEEP=1 tests/integration/run.sh            leave the containers running afterwards
#
# Needs: docker, privileged containers (systemd, keepalived and ufw need them),
# and internet access from the containers (apt and pip).
set -Eeuo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: do not rewrite /paths

UBUNTU="${1:-24.04}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=furnace-test
IMAGE="furnace-test-ubuntu:${UBUNTU}"
LEADER=furnace-leader
FOLLOWER=furnace-follower
FW=furnace-fw
XSOAR=furnace-xsoar
LEADER_IP=192.0.2.11
FOLLOWER_IP=192.0.2.12
SHARED_IP=192.0.2.10
FW_IP=192.0.2.50
XSOAR_IP=192.0.2.60
TEST_IP=198.51.100.77
PASSED=0
FAILED=0

step() { printf '\n### %s\n' "$*"; }
pass() { printf '  PASS  %s\n' "$*"; PASSED=$((PASSED + 1)); }
fail() { printf '  FAIL  %s\n' "$*"; FAILED=$((FAILED + 1)); }
check() { local name="$1"; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
on() { local box="$1"; shift; docker exec "$box" bash -c "$*"; }

remove_lab() {
    docker rm -f "$LEADER" "$FOLLOWER" "$FW" "$XSOAR" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}
teardown() {
    if [ "${KEEP:-0}" = "1" ]; then
        echo "KEEP=1: containers are still running. Remove them with: docker rm -f $LEADER $FOLLOWER $FW $XSOAR; docker network rm $NET"
        return
    fi
    remove_lab
}
trap teardown EXIT

step "Build the test image (Ubuntu $UBUNTU with systemd)"
BUILD_DIR="$(cd "$ROOT/tests/integration" && (pwd -W 2>/dev/null || pwd))"  # pwd -W: Windows path in Git Bash
docker build -q --build-arg "UBUNTU=$UBUNTU" -t "$IMAGE" "$BUILD_DIR" >/dev/null
remove_lab
docker network create --subnet 192.0.2.0/24 --gateway 192.0.2.1 "$NET" >/dev/null

start_box() {
    docker run -d --name "$1" --hostname "$1" --network "$NET" --ip "$2" \
        --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw "$IMAGE" >/dev/null
}
start_box "$LEADER" "$LEADER_IP"
start_box "$FOLLOWER" "$FOLLOWER_IP"
start_box "$FW" "$FW_IP"
start_box "$XSOAR" "$XSOAR_IP"
for box in "$LEADER" "$FOLLOWER" "$FW" "$XSOAR"; do
    for _ in $(seq 1 30); do
        state="$(docker exec "$box" systemctl is-system-running 2>/dev/null || true)"
        case "$state" in running|degraded) break ;; esac
        sleep 1
    done
done

copy_project() {
    docker exec "$1" mkdir -p /root/furnace
    tar -C "$ROOT" --exclude=.git --exclude=__pycache__ --exclude=.pytest_cache -cf - . \
        | docker exec -i "$1" tar -xf - -C /root/furnace
}

write_config() {
    local box="$1" role="$2" this="$3" peer="$4"
    docker exec -i "$box" bash -c 'cat > /root/furnace/config.env' <<EOF
ROLE=$role
THIS_IP=$this
PEER_IP=$peer
SHARED_IP=$SHARED_IP
SHARED_IP_PREFIX=24
INTERFACE=eth0
XSOAR_IP=$XSOAR_IP
FIREWALL_IP=$FW_IP
ARCHIVE_AFTER_DAYS=30
KEEPALIVED_PASSWORD=Tst1pass
VRRP_ROUTER_ID=51
USE_HTTPS=yes
MAX_IPS=50000
MAX_DOMAINS=50000
SSH_ALLOW_FROM=any
HOST_FIREWALL=ufw
EOF
}

copy_project "$LEADER"
copy_project "$FOLLOWER"
write_config "$LEADER" leader "$LEADER_IP" "$FOLLOWER_IP"
write_config "$FOLLOWER" follower "$FOLLOWER_IP" "$LEADER_IP"

step "install.sh --check on the leader (must change nothing)"
on "$LEADER" "cd /root/furnace && ./install.sh --check"
check "--check created no folders" on "$LEADER" "! test -e /etc/furnace && ! test -e /opt/furnace"

step "Bad settings are refused with a clear message"
on "$LEADER" "cd /root/furnace && sed 's/^ROLE=.*/ROLE=boss/' config.env > bad.env"
out="$(on "$LEADER" "cd /root/furnace && ./install.sh --config bad.env 2>&1" || true)"
check "wrong ROLE refused" grep -q "ROLE must be 'leader' or 'follower'" <<< "$out"
on "$LEADER" "cd /root/furnace && rm -f bad.env"

step "Install the leader"
on "$LEADER" "cd /root/furnace && ./install.sh" </dev/null
check "leader owns the shared IP" on "$LEADER" "ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'"
check "tokens file readable only by furnace" on "$LEADER" "[ \"\$(stat -c '%U %a' /etc/furnace/tokens.env)\" = 'furnace 400' ]"
check "no token in the install log" on "$LEADER" "! grep -q -f <(cut -d= -f2 /etc/furnace/tokens.env) /var/log/furnace-install.log"
check "cleanup timer runs on the leader" on "$LEADER" "systemctl is-active --quiet furnace-cleanup.timer"

step "Install the follower (bundle copied from the leader)"
docker cp "$LEADER:/root/furnace-peer-bundle.tar.gz" ./furnace-peer-bundle.tar.gz
docker cp ./furnace-peer-bundle.tar.gz "$FOLLOWER:/root/furnace/furnace-peer-bundle.tar.gz"
rm -f ./furnace-peer-bundle.tar.gz
on "$FOLLOWER" "cd /root/furnace && ./install.sh" </dev/null
check "follower does not own the shared IP" on "$FOLLOWER" "! ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'"
check "bundle deleted on the follower" on "$FOLLOWER" "! test -e /root/furnace/furnace-peer-bundle.tar.gz"
check "same certificate on both" [ "$(on "$LEADER" 'sha256sum < /etc/furnace/tls/furnace.crt')" = "$(on "$FOLLOWER" 'sha256sum < /etc/furnace/tls/furnace.crt')" ]
check "same tokens on both" [ "$(on "$LEADER" 'sha256sum < /etc/furnace/tokens.env')" = "$(on "$FOLLOWER" 'sha256sum < /etc/furnace/tokens.env')" ]
check "cleanup timer stopped on the follower" on "$FOLLOWER" "! systemctl is-active --quiet furnace-cleanup.timer"

TOKEN="$(on "$LEADER" "grep ^FURNACE_BLOCK_TOKEN= /etc/furnace/tokens.env | cut -d= -f2-")"
for box in "$FW" "$XSOAR"; do
    docker cp "$LEADER:/etc/furnace/tls/furnace.crt" ./furnace.crt
    docker cp ./furnace.crt "$box:/root/furnace.crt"
    rm -f ./furnace.crt
done
# The lab uses test addresses for everything. Allow them with the root-owned self-test
# flag (valid 10 minutes), the same way install.sh does, so the API is never restarted.
lab_allow_test_ranges() {
    for box in "$LEADER" "$FOLLOWER"; do on "$box" "touch /run/furnace-state/selftest"; done
}
lab_allow_test_ranges

api() {  # api METHOD PATH [JSON] -> prints "HTTP_CODE BODY", called from the XSOAR box
    local data=()
    [ -n "${3:-}" ] && data=(-H 'Content-Type: application/json' -d "$3")
    docker exec "$XSOAR" curl -sS --cacert /root/furnace.crt -H "Authorization: Bearer $TOKEN" \
        -X "$1" "${data[@]}" -w ' %{http_code}' "https://$SHARED_IP$2" | tr -d '\n'
}
edl() { docker exec "$FW" curl -sS --cacert /root/furnace.crt "https://$SHARED_IP/edl/$1"; }
code_from() { docker exec "$1" curl -s -o /dev/null -w '%{http_code}' --cacert /root/furnace.crt "$2"; }

step "API and EDL through the shared IP"
r="$(api POST /api/v1/ips "{\"ip\":\"$TEST_IP\",\"reason\":\"integration test\",\"added_by\":\"tester\",\"incident_id\":\"1\"}")"
check "XSOAR adds an IP (201)" grep -q ' 201$' <<< "$r"
check "the firewall sees the IP in blocklist.txt" grep -qx "$TEST_IP" <<< "$(edl blocklist.txt)"
r="$(api POST /api/v1/domains '{"domain":"bad.example.com","added_by":"tester"}')"
check "XSOAR adds a domain (201)" grep -q ' 201$' <<< "$r"
check "the firewall sees the domain in domains.txt" grep -qx "bad.example.com" <<< "$(edl domains.txt)"
check "the firewall may not use the API (403)" [ "$(code_from "$FW" "https://$SHARED_IP/api/v1/ips/$TEST_IP")" = "403" ]
check "XSOAR may not read the EDL (403)" [ "$(code_from "$XSOAR" "https://$SHARED_IP/edl/blocklist.txt")" = "403" ]
check "the archive CSV is not served" [ "$(code_from "$FW" "https://$SHARED_IP/edl/archive.csv")" = "404" ]
check "/health is not open to the firewall" [ "$(code_from "$FW" "https://$SHARED_IP/health")" = "403" ]

step "Replication to the follower (waits up to 90 seconds)"
ok=0
for _ in $(seq 1 90); do
    if on "$FOLLOWER" "grep -qx $TEST_IP /var/lib/furnace-edl/blocklist.txt"; then ok=1; break; fi
    sleep 1
done
check "the follower has the IP in its blocklist.txt" [ "$ok" = 1 ]
check "the follower has the domain" on "$FOLLOWER" "grep -qx bad.example.com /var/lib/furnace-edl/domains.txt"
r="$(docker exec "$XSOAR" curl -sS --cacert /root/furnace.crt -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"ip":"198.51.100.78"}' -w ' %{http_code}' "https://$FOLLOWER_IP/api/v1/ips" | tr -d '\n')"
check "the follower refuses changes (503 not_leader)" grep -q 'not_leader.* 503$' <<< "$r"

step "A second install run keeps data and tokens"
before_tokens="$(on "$LEADER" 'sha256sum < /etc/furnace/tokens.env')"
on "$LEADER" "cd /root/furnace && ./install.sh" </dev/null
lab_allow_test_ranges
check "tokens unchanged" [ "$before_tokens" = "$(on "$LEADER" 'sha256sum < /etc/furnace/tokens.env')" ]
check "the leader still owns the shared IP (no failover during upgrade)" on "$LEADER" "ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'"
check "the IP is still blocked" grep -qx "$TEST_IP" <<< "$(edl blocklist.txt)"
check "furnace-admin works" on "$LEADER" "furnace-admin stats | grep -q 'active ips *1'"

step "Failover: stop keepalived on the leader"
lab_allow_test_ranges
on "$LEADER" "/opt/furnace/bin/furnace-failover-test" && pass "failover test script (machine down)" || fail "failover test script (machine down)"
check "the follower now owns the shared IP" on "$FOLLOWER" "ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'"
check "the cleanup timer moved to the follower" on "$FOLLOWER" "systemctl is-active --quiet furnace-cleanup.timer"
check "the old leader stopped its cleanup timer" on "$LEADER" "! systemctl is-active --quiet furnace-cleanup.timer"
check "the firewall still gets the list" grep -qx "$TEST_IP" <<< "$(edl blocklist.txt)"
r="$(api POST /api/v1/ips '{"ip":"198.51.100.79","added_by":"tester"}')"
check "XSOAR can add on the new leader (201)" grep -q ' 201$' <<< "$r"
ok=0
for _ in $(seq 1 90); do
    if on "$LEADER" "grep -qx 198.51.100.79 /var/lib/furnace-edl/blocklist.txt"; then ok=1; break; fi
    sleep 1
done
check "replication now runs the other way" [ "$ok" = 1 ]

step "Failover: break the web service on the new leader (health check)"
on "$FOLLOWER" "/opt/furnace/bin/furnace-failover-test --web" && pass "failover test script (web service down)" || fail "failover test script (web service down)"
check "the shared IP is back on the first server" on "$LEADER" "ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'"

step "Uninstall (keeps data) on the follower"
on "$FOLLOWER" "/opt/furnace/uninstall.sh --yes"
check "program removed" on "$FOLLOWER" "! test -e /opt/furnace"
check "database kept" on "$FOLLOWER" "test -f /var/lib/furnace/furnace.db"

printf '\n%s passed, %s failed (Ubuntu %s)\n' "$PASSED" "$FAILED" "$UBUNTU"
[ "$FAILED" -eq 0 ]

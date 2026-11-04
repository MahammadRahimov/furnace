#!/usr/bin/env bash
# Furnace Docker test: the real image on two "hosts", a fake list reader and a fake caller.
# It builds the image and runs it like docker-compose.yml does, but on a test network:
# no --privileged, only NET_ADMIN and NET_RAW (for keepalived), two volumes per host.
# Only documentation addresses are used (198.51.100.0/24, so it can run next to a lab on 192.0.2.0/24).
# Nothing real is touched.
#
#   tests/docker/run.sh            build the image and run the test
#   KEEP=1 tests/docker/run.sh     leave the containers running afterwards
#
# Needs: docker. No internet after the image is built.
set -Eeuo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: do not rewrite /paths

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE=furnace-test-docker
NET=furnace-dtest
LEADER=furnace-d-leader
FOLLOWER=furnace-d-follower
CLIENT=furnace-d-client
LEADER_IP=198.51.100.11
FOLLOWER_IP=198.51.100.12
SHARED_IP=198.51.100.10
CLIENT_IP=198.51.100.50
TEST_IP=198.51.100.77
PERM_IP=203.0.113.77
LATER_IP=198.51.100.88
VOLUMES=(furnace-d-leader-etc furnace-d-leader-data furnace-d-follower-etc furnace-d-follower-data)
PASSED=0
FAILED=0

step() { printf '\n### %s\n' "$*"; }
pass() { printf '  PASS  %s\n' "$*"; PASSED=$((PASSED + 1)); }
fail() { printf '  FAIL  %s\n' "$*"; FAILED=$((FAILED + 1)); }
check() { local name="$1"; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
on() { local box="$1"; shift; docker exec "$box" bash -c "$*"; }

remove_lab() {
    docker rm -f "$LEADER" "$FOLLOWER" "$CLIENT" >/dev/null 2>&1 || true
    docker volume rm "${VOLUMES[@]}" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}
teardown() {
    if [ "${KEEP:-0}" = "1" ]; then
        echo "KEEP=1: containers are still running. Remove them with: docker rm -f $LEADER $FOLLOWER $CLIENT;" \
             "docker volume rm ${VOLUMES[*]}; docker network rm $NET"
        return
    fi
    remove_lab
}
trap teardown EXIT

# One Furnace host: NAME IP ROLE PEER_IP [extra docker options]. Created, not started.
create_host() {
    local name="$1" ip="$2" role="$3" peer="$4"
    shift 4
    docker create --name "$name" --hostname "$name" --network "$NET" --ip "$ip" \
        --cap-add NET_ADMIN --cap-add NET_RAW --stop-timeout 20 \
        -e FURNACE_ROLE="$role" -e FURNACE_THIS_IP="$ip" -e FURNACE_PEER_IP="$peer" \
        -e FURNACE_SHARED_IP="$SHARED_IP" -e FURNACE_LISTS_ALLOW_FROM="$CLIENT_IP" \
        -v "$name-etc:/etc/furnace" -v "$name-data:/var/lib/furnace" "$@" "$IMAGE" >/dev/null
}
# The lab uses test addresses for everything. Allow them with the root-owned flag (valid 10 minutes).
allow_test_ranges() { on "$1" "touch /run/furnace-state/selftest"; }
wait_ready() {  # wait_ready BOX: until the container says Ready (or stops)
    local _
    for _ in $(seq 1 60); do
        docker logs "$1" 2>&1 | grep -q "furnace: Ready." && return 0
        [ "$(docker inspect -f '{{.State.Running}}' "$1")" = "true" ] || return 1
        sleep 1
    done
    return 1
}
wait_for() {  # wait_for SECONDS COMMAND...: until the command succeeds
    local seconds="$1" _
    shift
    for _ in $(seq 1 "$seconds"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
    return 1
}
role_of() { on "$1" "curl -fsS --cacert /etc/furnace/tls/furnace.crt https://127.0.0.1/health" | grep -o '"role": *"[a-z]*"' | grep -o '[a-z]*"$' | tr -d '"'; }
owns_shared_ip() { on "$1" "ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'"; }
in_list() { on "$1" "grep -qx '$2' /var/lib/furnace-edl/blocklist.txt"; }

api() {  # api METHOD PATH [JSON] -> prints "BODY HTTP_CODE", from the client box
    local data=()
    [ -n "${3:-}" ] && data=(-H 'Content-Type: application/json' -d "$3")
    docker exec "$CLIENT" curl -sS --max-time 10 --cacert /root/furnace.crt -H "Authorization: Bearer $TOKEN" \
        -X "$1" "${data[@]}" -w ' %{http_code}' "https://$SHARED_IP$2" | tr -d '\n'
}
edl() { docker exec "$CLIENT" curl -sS --max-time 10 --cacert /root/furnace.crt -u "paloalto:$EDL_PW" "https://$SHARED_IP/edl/$1"; }
code() { docker exec "$CLIENT" curl -s --max-time 10 -o /dev/null -w '%{http_code}' --cacert /root/furnace.crt "$@"; }

step "Build the image"
BUILD_ROOT="$(cd "$ROOT" && (pwd -W 2>/dev/null || pwd))"  # pwd -W: Windows path in Git Bash
docker build -q -f "$BUILD_ROOT/docker/Dockerfile" --build-arg VERSION=test -t "$IMAGE" "$BUILD_ROOT" >/dev/null
check "the image was built" bash -c 'docker image inspect "$1" >/dev/null' _ "$IMAGE"
CTMP="$(mktemp -d)"
cp "$ROOT/docker/docker-compose.yml" "$CTMP/" && cp "$ROOT/docker/.env.example" "$CTMP/.env"
CTMP_D="$(cd "$CTMP" && (pwd -W 2>/dev/null || pwd))"  # the path docker sees (Windows path in Git Bash)
check "docker-compose.yml with .env.example is valid" docker compose --project-directory "$CTMP_D" -f "$CTMP_D/docker-compose.yml" config -q
out="$(docker compose --project-directory "$CTMP_D" -f "$CTMP_D/docker-compose.yml" config 2>&1)"
check "the settings in .env reach the container" bash -c 'grep -q "FURNACE_ROLE: leader" <<< "$1" && grep -q "FURNACE_SHARED_IP: 192.0.2.10" <<< "$1"' _ "$out"
rm -f "$CTMP/.env"
check "without .env docker compose refuses to start" bash -c '! docker compose --project-directory "$1" -f "$1/docker-compose.yml" config -q 2>/dev/null' _ "$CTMP_D"
rm -rf "$CTMP"
check "docker-compose.lab.yml is valid" docker compose -f "$BUILD_ROOT/docker/docker-compose.lab.yml" config -q
remove_lab
docker network create --subnet 198.51.100.0/24 --gateway 198.51.100.1 "$NET" >/dev/null
docker run -d --name "$CLIENT" --network "$NET" --ip "$CLIENT_IP" --entrypoint sleep "$IMAGE" infinity >/dev/null
check "the image runs as a plain container (not privileged)" \
    [ "$(docker inspect -f '{{.HostConfig.Privileged}}' "$CLIENT")" = "false" ]
check "Flask and gunicorn come from the bundle" on "$CLIENT" "test -d /usr/lib/furnace/vendor/flask && test -d /usr/lib/furnace/vendor/gunicorn"

step "Wrong settings stop the container with a clear message"
docker run --name furnace-d-bad --network "$NET" --cap-add NET_ADMIN -e FURNACE_ROLE=boss \
    -e FURNACE_THIS_IP=192.0.2.99 -e FURNACE_PEER_IP=192.0.2.99 "$IMAGE" > /tmp/furnace-d-bad.log 2>&1 && rc=0 || rc=$?
docker rm -f furnace-d-bad >/dev/null 2>&1 || true
out="$(cat /tmp/furnace-d-bad.log)"
rm -f /tmp/furnace-d-bad.log
check "it stops (exit 2)" [ "$rc" = 2 ]
check "it names every wrong setting" bash -c 'grep -q "FURNACE_ROLE must be .leader. or .follower." <<< "$1" &&
    grep -q "FURNACE_SHARED_IP is not set" <<< "$1" && grep -q "FURNACE_PEER_IP must be the other host" <<< "$1"' _ "$out"
check "it explains the host network" grep -q "network_mode: host" <<< "$out"
out="$(docker run --rm --entrypoint furnace "$IMAGE" check 2>&1 || true)"
check "furnace check shows the problems too" grep -q "FURNACE_ROLE is not set" <<< "$out"

step "Start the leader"
create_host "$LEADER" "$LEADER_IP" leader "$FOLLOWER_IP"
docker start "$LEADER" >/dev/null
check "the leader is ready" wait_ready "$LEADER"
logs="$(docker logs "$LEADER" 2>&1)"
check "it made the certificate and the secrets" bash -c 'grep -q "Self-signed certificate made" <<< "$1" &&
    grep -q "Replication secret made" <<< "$1" && grep -q "Follower bundle:" <<< "$1"' _ "$logs"
check "the leader owns the shared IP" wait_for 20 owns_shared_ip "$LEADER"
check "the leader says leader" [ "$(role_of "$LEADER")" = "leader" ]
check "the key is readable by root only" on "$LEADER" "[ \"\$(stat -c '%U %a' /etc/furnace/tls/furnace.key)\" = 'root 600' ]"
check "the replication secret is readable by the furnace group only" \
    on "$LEADER" "[ \"\$(stat -c '%U %G %a' /etc/furnace/secrets/replica-secret)\" = 'root furnace 640' ]"
check "the API runs as the furnace user, not root" \
    on "$LEADER" "[ \"\$(ps -o user= -C gunicorn -C python3 | grep -c furnace)\" -ge 1 ] || pgrep -u furnace -f gunicorn >/dev/null"
docker cp "$LEADER:/etc/furnace/tls/furnace.crt" - | docker cp - "$CLIENT:/root/"

step "The follower needs the leader's bundle"
create_host "$FOLLOWER" "$FOLLOWER_IP" follower "$LEADER_IP"
docker start "$FOLLOWER" >/dev/null
wait_for 30 bash -c "[ \"\$(docker inspect -f '{{.State.Running}}' $FOLLOWER)\" = false ]" || true
check "without the bundle it stops and says what to do" \
    bash -c 'docker logs "$1" 2>&1 | grep -q "Copy furnace-peer-bundle.tar.gz from the leader"' _ "$FOLLOWER"
docker cp "$LEADER:/etc/furnace/furnace-peer-bundle.tar.gz" - | docker cp - "$FOLLOWER:/etc/furnace/"
docker start "$FOLLOWER" >/dev/null
check "with the bundle the follower is ready" wait_ready "$FOLLOWER"
check "it took the secrets from the bundle and deleted it" bash -c 'docker logs "$1" 2>&1 | grep -q "taken from the leader.s bundle" &&
    docker exec "$1" test ! -e /etc/furnace/furnace-peer-bundle.tar.gz' _ "$FOLLOWER"
check "same certificate on both" [ "$(on "$LEADER" 'sha256sum < /etc/furnace/tls/furnace.crt')" = "$(on "$FOLLOWER" 'sha256sum < /etc/furnace/tls/furnace.crt')" ]
check "the follower does not own the shared IP" bash -c '! docker exec "$1" ip -4 addr show eth0 | grep -q "inet $2/"' _ "$FOLLOWER" "$SHARED_IP"
check "the follower says follower" [ "$(role_of "$FOLLOWER")" = "follower" ]

step "First API token and list login with docker exec (shown once, not in the log)"
out="$(docker exec "$LEADER" furnace token add api --allow all --expires-days 0)"
TOKEN="$(grep -o 'fur_[A-Za-z0-9_-]*' <<< "$out" | head -n1)"
out="$(docker exec "$LEADER" furnace edl-user add paloalto)"
EDL_PW="$(tail -n 3 <<< "$out" | tr -d ' \r' | grep -E '^[A-Za-z0-9_-]{24,}$' | head -n1)"
check "a token and a list password were made" bash -c '[[ "$1" == fur_* && ${#2} -ge 24 ]]' _ "$TOKEN" "$EDL_PW"
check "no secret in the docker logs" bash -c '! docker logs "$1" 2>&1 | grep -qF -e "$2" -e "$3"' _ "$LEADER" "$TOKEN" "$EDL_PW"
allow_test_ranges "$LEADER"
allow_test_ranges "$FOLLOWER"

step "API and lists through the shared IP"
r="$(api POST /api/v1/ips "{\"ip\":\"$TEST_IP\",\"reason\":\"docker test\"}")"
check "the caller adds an IP (201)" grep -q ' 201$' <<< "$r"
r="$(api POST /api/v1/ips "{\"ip\":\"$PERM_IP\",\"permanent\":true}")"
check "the caller adds a permanent IP (201)" grep -Eq '"permanent": ?true.* 201$' <<< "$r"
r="$(api POST /api/v1/domains '{"domain":"bad.example.com"}')"
check "the caller adds a domain (201)" grep -q ' 201$' <<< "$r"
check "the reader sees the IP in blocklist.txt" grep -qx "$TEST_IP" <<< "$(edl blocklist.txt)"
check "the reader sees the domain in domains.txt" grep -qx "bad.example.com" <<< "$(edl domains.txt)"
r="$(api POST /api/v1/urls '{"url":"https://Bad.Example.com/Phish/","reason":"phishing"}')"
check "the caller adds a URL (201)" grep -q ' 201$' <<< "$r"
check "the reader sees the URL in urls.txt" grep -qx "bad.example.com/phish/" <<< "$(edl urls.txt)"
r="$(api POST /api/v1/whitelist/domains '{"domain":"github.io"}')"
check "a shared platform cannot be whitelisted (Public Suffix List in the image, 422)" \
    grep -q 'whitelist_not_allowed.* 422$' <<< "$r"
check "the local refuse file was made" on "$LEADER" "[ -f /etc/furnace/whitelist-refuse.txt ]"
check "the list without a login: 401" [ "$(code "https://$SHARED_IP/edl/blocklist.txt")" = 401 ]
check "a wrong token: 401" [ "$(code -H 'Authorization: Bearer fur_wrong' "https://$SHARED_IP/api/v1/ips/$TEST_IP")" = 401 ]
check "the database is never served: 404" [ "$(code -u "paloalto:$EDL_PW" "https://$SHARED_IP/furnace.db")" = 404 ]
check "only the other host may send a snapshot: 403" \
    [ "$(code -X POST --data-binary x "https://$FOLLOWER_IP/replica/snapshot")" = 403 ]
check "a snapshot with a wrong secret is refused (401)" [ "$(on "$LEADER" \
    "curl -s -o /dev/null -w '%{http_code}' --cacert /etc/furnace/tls/furnace.crt -H 'Authorization: Bearer wrong' \
     --data-binary x https://$FOLLOWER_IP/replica/snapshot")" = 401 ]

step "Replication over HTTPS (every 20 seconds)"
check "the follower gets the IPs, the domain and the URL within 60 seconds" \
    wait_for 60 bash -c "docker exec $FOLLOWER grep -qx '$PERM_IP' /var/lib/furnace-edl/blocklist.txt && docker exec $FOLLOWER grep -qx bad.example.com/phish/ /var/lib/furnace-edl/urls.txt &&
                         docker exec $FOLLOWER grep -qx 'bad.example.com' /var/lib/furnace-edl/domains.txt"
check "the follower has the token and the list login" \
    on "$FOLLOWER" "furnace token list | grep -q '^api ' && furnace edl-user list | grep -q paloalto"
check "the leader logs the replication once, not every 20 seconds" \
    [ "$(docker logs "$LEADER" 2>&1 | grep -c 'furnace: replication')" -le 2 ]
check "furnace status is fine on the leader" grep -q "Replication: ok sent" <<< "$(docker exec "$LEADER" furnace status)"
check "furnace status is fine on the follower" on "$FOLLOWER" "furnace status >/dev/null"

step "A stopped process is started again"
on "$LEADER" "pkill -o -x nginx"
check "nginx runs again within 10 seconds" wait_for 10 bash -c "docker exec $LEADER furnace status | grep -Eq 'nginx +running +\(restarted 1 times\)'"

step "Failover: docker stop on the leader"
start=$(date +%s)
docker stop "$LEADER" >/dev/null
took=$(( $(date +%s) - start ))
check "the leader stopped cleanly in less than 20 seconds (${took}s)" bash -c '[ "$1" -lt 20 ] &&
    [ "$(docker inspect -f "{{.State.ExitCode}}" "$2")" = 0 ]' _ "$took" "$LEADER"
check "the follower takes the shared IP" wait_for 20 owns_shared_ip "$FOLLOWER"
check "the follower is now the leader" wait_for 10 bash -c "[ \"\$(docker exec $FOLLOWER curl -fsS --cacert /etc/furnace/tls/furnace.crt https://127.0.0.1/health | grep -o 'leader')\" = leader ]"
check "the lists still work through the shared IP" grep -qx "$TEST_IP" <<< "$(edl blocklist.txt)"
r="$(api POST /api/v1/ips "{\"ip\":\"$LATER_IP\",\"reason\":\"after failover\"}")"
check "the caller can still add IPs (201)" grep -q ' 201$' <<< "$r"

step "The old leader comes back: it stays the standby and gets the new data"
docker start "$LEADER" >/dev/null
check "the old leader is ready again" wait_for 60 bash -c "docker logs --since 2m $LEADER 2>&1 | grep -q 'furnace: Ready.'"
allow_test_ranges "$LEADER"
sleep 8
check "it does not take the shared IP back (nopreempt)" bash -c '! docker exec "$1" ip -4 addr show eth0 | grep -q "inet $2/"' _ "$LEADER" "$SHARED_IP"
check "its data is still there (volumes)" in_list "$LEADER" "$TEST_IP"
check "it gets the IP added after the failover within 60 seconds" wait_for 60 in_list "$LEADER" "$LATER_IP"
check "the shared IP is on one host only" bash -c '! docker exec "$1" ip -4 addr show eth0 | grep -q "inet $3/" ||
    ! docker exec "$2" ip -4 addr show eth0 | grep -q "inet $3/"' _ "$LEADER" "$FOLLOWER" "$SHARED_IP"

printf '\n%s passed, %s failed (docker)\n' "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ]

#!/usr/bin/env bash
# Furnace Docker test: FURNACE_KEEPALIVED=external (the host's own keepalived moves the shared IP).
#
# Each "host" is a small container that owns the network and runs keepalived, as the real host would.
# The Furnace container shares that network (like network_mode: host) and gets NO extra rights.
# The host's /run/furnace is a volume that both see (the container as /run/furnace-host).
# Only documentation addresses are used (198.51.100.0/24). Nothing real is touched.
#
#   tests/docker/external.sh            build the image and run the test
#   KEEP=1 tests/docker/external.sh     leave the containers running afterwards
set -Eeuo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: do not rewrite /paths

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE=furnace-test-docker
NET=furnace-xtest
HOST_A=furnace-x-hosta
HOST_B=furnace-x-hostb
FA=furnace-x-a
FB=furnace-x-b
CLIENT=furnace-x-client
A_IP=198.51.100.21
B_IP=198.51.100.22
SHARED_IP=198.51.100.20
CLIENT_IP=198.51.100.50
VOLUMES=(furnace-x-a-etc furnace-x-a-data furnace-x-a-run furnace-x-b-etc furnace-x-b-data furnace-x-b-run)
PASSED=0
FAILED=0

step() { printf '\n### %s\n' "$*"; }
pass() { printf '  PASS  %s\n' "$*"; PASSED=$((PASSED + 1)); }
fail() { printf '  FAIL  %s\n' "$*"; FAILED=$((FAILED + 1)); }
check() { local name="$1"; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
on() { local box="$1"; shift; docker exec "$box" bash -c "$*"; }

remove_lab() {
    docker rm -f "$FA" "$FB" "$HOST_A" "$HOST_B" "$CLIENT" >/dev/null 2>&1 || true
    docker volume rm "${VOLUMES[@]}" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}
teardown() {
    if [ "${KEEP:-0}" = "1" ]; then
        echo "KEEP=1: containers are still running. Remove them with: docker rm -f $FA $FB $HOST_A $HOST_B $CLIENT;" \
             "docker volume rm ${VOLUMES[*]}; docker network rm $NET"
        return
    fi
    remove_lab
}
trap teardown EXIT

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
# A "host": owns the IP and the network card, may change the network (for keepalived), has /run/furnace.
host_box() {  # host_box NAME IP RUN_VOLUME
    docker run -d --name "$1" --hostname "$1" --network "$NET" --ip "$2" --cap-add NET_ADMIN --cap-add NET_RAW \
        -v "$3:/run/furnace" --entrypoint sleep "$IMAGE" infinity >/dev/null
}
# Furnace on that host: the same settings on both, external keepalived, no extra rights.
furnace_box() {  # furnace_box NAME HOST_BOX VOLUME_PREFIX [extra docker options]
    local name="$1" host="$2" prefix="$3"
    shift 3
    docker create --name "$name" --network "container:$host" --stop-timeout 20 \
        -e FURNACE_LEADER_IP="$A_IP" -e FURNACE_FOLLOWER_IP="$B_IP" -e FURNACE_SHARED_IP="$SHARED_IP" \
        -e FURNACE_KEEPALIVED=external -e FURNACE_LISTS_ALLOW_FROM="$CLIENT_IP" \
        -v "$prefix-etc:/etc/furnace" -v "$prefix-data:/var/lib/furnace" -v "$prefix-run:/run/furnace-host" \
        "$@" "$IMAGE" >/dev/null
}
# What the user does on the host, with the text that "furnace host-keepalived" prints.
setup_host_keepalived() {  # setup_host_keepalived HOST_BOX FURNACE_BOX
    docker exec "$2" furnace host-keepalived notify | docker exec -i "$1" sh -c 'cat > /usr/local/sbin/furnace-notify'
    docker exec "$2" furnace host-keepalived health-check | docker exec -i "$1" sh -c 'cat > /usr/local/sbin/furnace-health-check'
    docker exec "$1" chmod 755 /usr/local/sbin/furnace-notify /usr/local/sbin/furnace-health-check
    { printf 'global_defs {\n    script_user root\n    enable_script_security\n}\n\n'
      docker exec "$2" furnace host-keepalived config; } | docker exec -i "$1" sh -c 'cat > /etc/keepalived/keepalived.conf'
    docker exec -d "$1" keepalived --dont-fork --log-console --use-file /etc/keepalived/keepalived.conf
}
owns_shared_ip() { on "$1" "ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'"; }
host_role() { on "$1" "cat /run/furnace/role 2>/dev/null"; }
role_of() { on "$1" "curl -fsS --cacert /etc/furnace/tls/furnace.crt https://127.0.0.1/health" | grep -o '"role": *"[a-z]*"' | grep -o '[a-z]*"$' | tr -d '"'; }
is_role() { [ "$(role_of "$1")" = "$2" ]; }
is_host_role() { [ "$(host_role "$1")" = "$2" ]; }
edl_code() { docker exec "$CLIENT" curl -sk --max-time 5 -o /dev/null -w '%{http_code}' "https://$SHARED_IP/edl/blocklist.txt"; }

step "Build the image"
BUILD_ROOT="$(cd "$ROOT" && (pwd -W 2>/dev/null || pwd))"  # pwd -W: Windows path in Git Bash
docker build -q -f "$BUILD_ROOT/docker/Dockerfile" --build-arg VERSION=test -t "$IMAGE" "$BUILD_ROOT" >/dev/null
check "the image was built" bash -c 'docker image inspect "$1" >/dev/null' _ "$IMAGE"
remove_lab
docker network create --subnet 198.51.100.0/24 --gateway 198.51.100.1 "$NET" >/dev/null
docker run -d --name "$CLIENT" --network "$NET" --ip "$CLIENT_IP" --entrypoint sleep "$IMAGE" infinity >/dev/null

step "Host A: Furnace with FURNACE_KEEPALIVED=external, no keepalived on the host yet"
host_box "$HOST_A" "$A_IP" furnace-x-a-run
furnace_box "$FA" "$HOST_A" furnace-x-a
docker start "$FA" >/dev/null
check "the leader is ready" wait_ready "$FA"
check "the container has no extra rights (no NET_ADMIN)" \
    [ "$(docker inspect -f '{{.HostConfig.CapAdd}}' "$FA")" = "[]" ]
check "no keepalived runs in the container" bash -c '! docker top "$1" | grep -q keepalived' _ "$FA"
check "the log says the host's keepalived decides" \
    bash -c 'docker logs "$1" 2>&1 | grep -q "the HOST.s keepalived decides"' _ "$FA"
out="$(docker exec "$FA" furnace status 2>&1)" && rc=0 || rc=$?
check "status warns: no role from the host's keepalived yet" \
    bash -c '[ "$1" != 0 ] && grep -q "no role from the host.s keepalived yet" <<< "$2"' _ "$rc" "$out"
check "without the host's keepalived it works as the follower" is_role "$FA" follower
out="$(docker exec "$FA" furnace host-keepalived)"
check "host-keepalived explains the steps and shows the block" bash -c 'grep -q "Do this on BOTH hosts" <<< "$1" &&
    grep -q "vrrp_instance FURNACE" <<< "$1" && grep -q "priority 150" <<< "$1" && grep -q "semanage fcontext" <<< "$1"' _ "$out"

step "The host's keepalived on host A takes the shared IP"
setup_host_keepalived "$HOST_A" "$FA"
check "host A owns the shared IP" wait_for 30 owns_shared_ip "$HOST_A"
check "the host's notify script wrote 'leader'" wait_for 10 is_host_role "$HOST_A" leader
check "Furnace on host A is the leader" wait_for 10 is_role "$FA" leader
out="$(docker exec "$FA" furnace status 2>&1 || true)"
check "status shows the host's keepalived and no warning" \
    bash -c 'grep -q "the host.s own (FURNACE_KEEPALIVED=external)" <<< "$1" && ! grep -q "no role from" <<< "$1"' _ "$out"
check "the client reaches the lists on the shared IP (401 = the list login)" [ "$(edl_code)" = 401 ]

step "Host B: the follower joins with a code, its host's keepalived stays backup"
CODE="$(docker exec "$FA" furnace join-code | grep -oE 'FJ1(-[A-Z2-7]{4}){8}' | head -n1)"
host_box "$HOST_B" "$B_IP" furnace-x-b-run
furnace_box "$FB" "$HOST_B" furnace-x-b -e FURNACE_JOIN_CODE="$CODE"
docker start "$FB" >/dev/null
check "the follower is ready (secrets fetched with the join code)" wait_ready "$FB"
out="$(docker exec "$FB" furnace host-keepalived config)"
check "the follower's block has priority 100 and the same password" bash -c 'grep -q "priority 100" <<< "$1" &&
    [ "$(grep auth_pass <<< "$1")" = "$(docker exec "$2" furnace host-keepalived config | grep auth_pass)" ]' _ "$out" "$FA"
setup_host_keepalived "$HOST_B" "$FB"
check "host B's notify script wrote 'follower'" wait_for 20 is_host_role "$HOST_B" follower
check "host A keeps the shared IP" owns_shared_ip "$HOST_A"
check "host B does not have the shared IP" bash -c '! docker exec "$1" ip -4 addr show eth0 | grep -q "inet $2/"' _ "$HOST_B" "$SHARED_IP"
check "Furnace on host B is the follower" is_role "$FB" follower
check "replication runs from A to B" \
    wait_for 60 bash -c 'docker exec "$1" furnace status 2>/dev/null | grep -q "^Replication: ok"' _ "$FA"

step "The Furnace container on host A stops (host A stays up): the host's health check moves the shared IP"
docker stop "$FA" >/dev/null
check "host B owns the shared IP within 40 seconds" wait_for 40 owns_shared_ip "$HOST_B"
check "host B's notify script wrote 'leader'" wait_for 10 is_host_role "$HOST_B" leader
check "host A's notify script wrote 'follower'" wait_for 10 is_host_role "$HOST_A" follower
check "Furnace on host B is the leader" wait_for 10 is_role "$FB" leader
check "the client still reaches the lists on the shared IP" wait_for 10 bash -c '[ "$(docker exec "$1" curl -sk --max-time 5 -o /dev/null -w "%{http_code}" "https://$2/edl/blocklist.txt")" = 401 ]' _ "$CLIENT" "$SHARED_IP"

step "Furnace on host A starts again: no flapping (nopreempt), it is the follower"
docker start "$FA" >/dev/null
check "Furnace on host A is ready again" wait_for 60 bash -c 'docker exec "$1" curl -fsS --cacert /etc/furnace/tls/furnace.crt https://127.0.0.1/health' _ "$FA"
sleep 15
check "host B keeps the shared IP" owns_shared_ip "$HOST_B"
check "Furnace on host A is the follower" is_role "$FA" follower

step "Host B goes down completely: host A takes over"
docker stop "$FB" >/dev/null
docker stop "$HOST_B" >/dev/null
check "host A owns the shared IP" wait_for 40 owns_shared_ip "$HOST_A"
check "Furnace on host A is the leader" wait_for 10 is_role "$FA" leader
check "the client reaches the lists on the shared IP" wait_for 10 bash -c '[ "$(docker exec "$1" curl -sk --max-time 5 -o /dev/null -w "%{http_code}" "https://$2/edl/blocklist.txt")" = 401 ]' _ "$CLIENT" "$SHARED_IP"

printf '\n%s passed, %s failed (docker, external keepalived)\n' "$PASSED" "$FAILED"
[ "$FAILED" -eq 0 ]

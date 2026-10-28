#!/usr/bin/env bash
# Furnace integration test with Docker: two servers, a fake firewall (reads the lists) and a fake SOAR (calls the API).
# It builds the .deb, installs it like a user would, and answers the setup questions.
# Only documentation addresses are used (192.0.2.0/24). Nothing real is touched.
#
#   tests/integration/run.sh [SYSTEM]     run the test on SYSTEM (default ubuntu:24.04)
#   KEEP=1 tests/integration/run.sh       leave the containers running afterwards
#
# SYSTEM is a Docker image name: ubuntu:22.04, ubuntu:24.04, debian:12, debian:13 (.deb), or
# almalinux:8, almalinux:9, almalinux:10, rockylinux:9, oraclelinux:9, fedora:42, amazonlinux:2023 (.rpm).
# A plain version like 22.04 means ubuntu:22.04.
#
# Needs: docker, privileged containers (systemd, keepalived, ufw and firewalld need them),
# and internet access from the containers (apt).
set -Eeuo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: do not rewrite /paths

SYSTEM="${1:-ubuntu:24.04}"
[[ "$SYSTEM" == *:* ]] || SYSTEM="ubuntu:$SYSTEM"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=furnace-test
IMAGE="furnace-test-${SYSTEM/:/-}"
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
case "$SYSTEM" in
    ubuntu:*|debian:*) KIND=deb; DOCKERFILE=Dockerfile; FWTOOL=ufw; NGINX_CONF=/etc/nginx/sites-enabled/furnace; NGINX_GROUP=www-data ;;
    *) KIND=rpm; DOCKERFILE=Dockerfile.rpm; FWTOOL=firewalld; NGINX_CONF=/etc/nginx/conf.d/furnace.conf; NGINX_GROUP=nginx ;;
esac
PKG="$ROOT/dist/furnace-test.$KIND"
FPY=/usr/lib/furnace/bin/furnace-python   # Python 3.9+, also where python3 is older (RHEL 8)

# The package tools of each family.
pkg_install()   { if [ "$KIND" = deb ]; then on "$1" "apt-get update -qq >/dev/null && apt-get install -y -qq /root/furnace.$KIND 2>&1";
                  else on "$1" "dnf install -y /root/furnace.$KIND 2>&1 || { sleep 15; dnf install -y /root/furnace.$KIND 2>&1; }"; fi; }  # one retry: mirrors can time out
pkg_reinstall() { if [ "$KIND" = deb ]; then on "$1" "apt-get install -y -qq --reinstall /root/furnace.$KIND 2>&1";
                  else on "$1" "dnf reinstall -y /root/furnace.$KIND 2>&1"; fi; }
pkg_has()       { if [ "$KIND" = deb ]; then echo "dpkg -s $1 2>/dev/null | grep -q '^Status: install ok installed'";
                  else echo "rpm -q $1 >/dev/null 2>&1"; fi; }
pkg_remove()    { if [ "$KIND" = deb ]; then on "$1" "apt-get remove -y -qq furnace >/dev/null 2>&1";
                  else on "$1" "dnf remove -y furnace 2>&1 | grep -v '^  ' | tail -n 12"; fi; }
# The host firewall rules, one per line (ufw status or firewalld rich rules).
fw_rules() { if [ "$FWTOOL" = ufw ]; then on "$1" "ufw status"; else on "$1" "firewall-cmd --list-rich-rules"; fi; }
fw_web_open_to_all() {
    if [ "$FWTOOL" = ufw ]; then grep -E '^443/tcp +ALLOW +Anywhere' <<< "$1" | grep -q 'furnace web'
    else grep -qx 'rule port port="443" protocol="tcp" accept' <<< "$1"; fi
}
PASSED=0
FAILED=0

step() { printf '\n### %s\n' "$*"; }
pass() { printf '  PASS  %s\n' "$*"; PASSED=$((PASSED + 1)); }
fail() { printf '  FAIL  %s\n' "$*"; FAILED=$((FAILED + 1)); }
check() { local name="$1"; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
on() { local box="$1"; shift; docker exec "$box" bash -c "$*"; }
put() { docker exec -i "$1" bash -c "cat > '$2'"; }   # put BOX PATH < local data
copy_between() { docker exec "$1" cat "$2" | put "$3" "$4"; }

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

step "Build the test image ($SYSTEM with systemd) and the .deb package"
BUILD_DIR="$(cd "$ROOT/tests/integration" && (pwd -W 2>/dev/null || pwd))"  # pwd -W: Windows path in Git Bash
docker build -q --load --build-arg "BASE=$SYSTEM" -f "$BUILD_DIR/$DOCKERFILE" -t "$IMAGE" "$BUILD_DIR" >/dev/null
mkdir -p "$ROOT/dist"
if [ "$KIND" = deb ]; then
    tar -C "$ROOT" --exclude=.git --exclude=__pycache__ --exclude=.pytest_cache --exclude=dist --exclude=build/vendor-test -cf - . \
        | docker run -i --rm "$IMAGE" bash -c \
            'mkdir /src && tar -xf - -C /src && /src/packaging/build-deb.sh /out >/dev/null && cat /out/*.deb' > "$PKG"
else
    tar -C "$ROOT" --exclude=.git --exclude=__pycache__ --exclude=.pytest_cache --exclude=dist --exclude=build/vendor-test -cf - . \
        | docker run -i --rm almalinux:9 bash -c \
            'dnf install -y -q rpm-build python3 >/dev/null 2>&1; mkdir /src && tar -xf - -C /src && /src/packaging/build-rpm.sh /out >/dev/null && cat /out/*.rpm' > "$PKG"
fi
check "the package was built" test -s "$PKG"
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

step "Install the package on both servers (apt or dnf install ./furnace.$KIND)"
for box in "$LEADER" "$FOLLOWER"; do
    put "$box" "/root/furnace.$KIND" < "$PKG"
    put "$box" /root/answer.py < "$ROOT/tests/integration/answer.py"
    out="$(pkg_install "$box")" || printf '%s\n' "$out" | tail -n 20   # show why, then the check below fails
    check "$box: the package and its dependencies are installed" on "$box" "$(pkg_has furnace)"
    check "$box: the install tells the user to run setup" grep -q "sudo furnace setup" <<< "$out"
done
check "the furnace command works" on "$LEADER" "furnace version | grep -qx '[0-9.]*'"
check "Flask and gunicorn come from the package, not from the system" \
    on "$LEADER" "! $(pkg_has python3-flask) && ! $(pkg_has python3-gunicorn) && test -d /usr/lib/furnace/vendor/flask"
check "the install starts nothing before setup" on "$LEADER" "! systemctl is-active --quiet furnace-api && ! test -e /etc/furnace/setup.conf"
out="$(on "$LEADER" "furnace status 2>&1" || true)"
check "status says that setup is still needed" grep -q "not set up yet" <<< "$out"

step "setup --check and bad settings"
put "$LEADER" /root/leader.env <<EOF
ROLE=leader
PEER_IP=$FOLLOWER_IP
SHARED_IP=$SHARED_IP
XSOAR_IP=$XSOAR_IP
FIREWALL_IP=$FW_IP
EOF
# (the old setting names on purpose: they must still work)
on "$LEADER" "furnace setup --check --config /root/leader.env"
check "--check created no secrets and no settings" on "$LEADER" "! test -e /var/lib/furnace/furnace.db && ! test -e /etc/furnace/setup.conf"
on "$LEADER" "sed 's/^ROLE=.*/ROLE=boss/' /root/leader.env > /root/bad.env"
out="$(on "$LEADER" "furnace setup --config /root/bad.env 2>&1" || true)"
check "wrong ROLE refused" grep -q "ROLE must be 'leader' or 'follower'" <<< "$out"
on "$FOLLOWER" "printf 'ROLE=follower\n' > /root/follower.env"
out="$(on "$FOLLOWER" "furnace setup --config /root/follower.env 2>&1" || true)"
check "a follower without the leader bundle is refused" grep -q "needs furnace-peer-bundle.tar.gz" <<< "$out"

step "Set up the leader by answering the questions"
TAB=$'\t'
put "$LEADER" /root/answers.txt <<EOF
leader or the follower?${TAB}
Network card name [${TAB}$LEADER_IP
Network card name [${TAB}
This server's IP [${TAB}
The other Furnace server's IP:${TAB}999.1.1.1
The other Furnace server's IP:${TAB}$FOLLOWER_IP
Shared IP:${TAB}$SHARED_IP
Network prefix${TAB}
Who may call the API [any]:${TAB}$XSOAR_IP
Who may download the lists [any]:${TAB}$FW_IP
Most entries per list${TAB}
Days an IP stays${TAB}
VRRP router ID (1-255)${TAB}
SSH allowed from${TAB}
own firewall ($FWTOOL)?${TAB}
Token name [api]:${TAB}soar
Box number, or Enter when done:${TAB}5
Box number, or Enter when done:${TAB}4
Box number, or Enter when done:${TAB}
Token expires after how many days${TAB}
Token expires after how many days${TAB}abc
Token expires after how many days${TAB}365
Set up Furnace with these settings?${TAB}y
EOF
out="$(on "$LEADER" "$FPY /root/answer.py /root/answers.txt -- furnace setup" 2>&1)" && rc=0 || rc=$?
printf '%s\n' "$out"
check "leader setup finished with the self-test passed" [ "$rc" = 0 ]
check "a wrong answer is explained and asked again" grep -q "PEER_IP '999.1.1.1' is not a valid IPv4 address" <<< "$out"
check "an IP as the network card is explained" grep -q "is an IP address, not a network card name" <<< "$out"
check "a wrong box number is explained" grep -q "Type 1, 2, 3 or 4" <<< "$out"
check "the token expiry has no default and is checked" grep -q "from 0 to 3650 (0 = never)" <<< "$out"
check "the settings show the first token" grep -q "First API token *soar (blocklist:read, blocklist:write, whitelist:read; expires after 365 days)" <<< "$out"
check "the new secrets were shown once" grep -q "NEW SECRETS (shown only once" <<< "$out"
secret_after() { grep -A1 -F "$1" <<< "$out" | tail -n1 | tr -d ' \r'; }
TOKEN="$(secret_after "API token 'soar'.")"
EDL_PW="$(secret_after "List login (for the device that downloads the lists): user 'lists', password:")"
check "one API token and an EDL password were made" \
    bash -c '[[ "$1" == fur_* && ${#2} -ge 32 ]]' _ "$TOKEN" "$EDL_PW"
check "the token has the chosen permissions and expiry" \
    on "$LEADER" "furnace token list | grep -Eq '^soar +read\+write +read +20[0-9-]{8} \(in 365 days\) +never '"
check "setup made only this one token" [ "$(on "$LEADER" "furnace token list | tail -n +2 | wc -l")" = 1 ]
out_wl="$(on "$LEADER" "furnace token add wl-admin --allow whitelist:write --expires-days 0")"
WL_TOKEN="$(grep -o 'fur_[A-Za-z0-9_-]*' <<< "$out_wl")"
check "a whitelist token from the command line (write brings read)" \
    grep -q "Permissions: blocklist -, whitelist read+write. Expires: never." <<< "$out_wl"
check "the network card and IP were found automatically" on "$LEADER" "grep -qx 'INTERFACE=eth0' /etc/furnace/setup.conf && grep -qx 'THIS_IP=$LEADER_IP' /etc/furnace/setup.conf"
check "a keepalived password was created" on "$LEADER" "grep -Eqx 'KEEPALIVED_PASSWORD=[A-Za-z0-9]{8}' /etc/furnace/setup.conf"
check "the saved settings are readable by root only" on "$LEADER" "[ \"\$(stat -c '%U %a' /etc/furnace/setup.conf)\" = 'root 600' ]"
check "leader owns the shared IP" on "$LEADER" "ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'"
check "no tokens file any more" on "$LEADER" "! test -e /etc/furnace/tokens.env"
check "no secret in the setup log" on "$LEADER" "! grep -qF -e '$TOKEN' -e '$WL_TOKEN' -e '$EDL_PW' /var/log/furnace-setup.log"
check "no secret in the database" on "$LEADER" "! sqlite3 /var/lib/furnace/furnace.db .dump | grep -qF -e '$TOKEN' -e '$EDL_PW'"
check "the EDL users file has a SHA-512 hash, readable by nginx only" \
    on "$LEADER" "grep -q '^lists:\\\$6\\\$' /var/lib/furnace-auth/edl-users && [ \"\$(stat -c '%U %G %a' /var/lib/furnace-auth/edl-users)\" = 'furnace $NGINX_GROUP 640' ]"
check "cleanup timer runs on the leader" on "$LEADER" "systemctl is-active --quiet furnace-cleanup.timer"

step "Set up the follower (bundle copied from the leader, only two questions)"
copy_between "$LEADER" /root/furnace-peer-bundle.tar.gz "$FOLLOWER" /root/furnace-peer-bundle.tar.gz
put "$FOLLOWER" /root/answers.txt <<EOF
leader or the follower?${TAB}follower
Network card name [${TAB}
Set up Furnace with these settings?${TAB}y
EOF
out="$(on "$FOLLOWER" "$FPY /root/answer.py /root/answers.txt -- furnace setup" 2>&1)" && rc=0 || rc=$?
printf '%s\n' "$out"
check "follower setup finished with the self-test passed" [ "$rc" = 0 ]
check "follower took its settings from the bundle" on "$FOLLOWER" "grep -qx 'THIS_IP=$FOLLOWER_IP' /etc/furnace/setup.conf && grep -qx 'PEER_IP=$LEADER_IP' /etc/furnace/setup.conf && grep -qx 'API_ALLOW_FROM=$XSOAR_IP' /etc/furnace/setup.conf"
check "same keepalived password on both" [ "$(on "$LEADER" 'grep ^KEEPALIVED_PASSWORD= /etc/furnace/setup.conf')" = "$(on "$FOLLOWER" 'grep ^KEEPALIVED_PASSWORD= /etc/furnace/setup.conf')" ]
check "follower does not own the shared IP" on "$FOLLOWER" "! ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'"
check "bundle deleted on the follower" on "$FOLLOWER" "! test -e /root/furnace-peer-bundle.tar.gz"
check "same certificate on both" [ "$(on "$LEADER" 'sha256sum < /etc/furnace/tls/furnace.crt')" = "$(on "$FOLLOWER" 'sha256sum < /etc/furnace/tls/furnace.crt')" ]
check "cleanup timer stopped on the follower" on "$FOLLOWER" "! systemctl is-active --quiet furnace-cleanup.timer"

for box in "$FW" "$XSOAR"; do
    copy_between "$LEADER" /etc/furnace/tls/furnace.crt "$box" /root/furnace.crt
done
# The lab uses test addresses for everything. Allow them with the root-owned self-test
# flag (valid 10 minutes), the same way setup does, so the API is never restarted.
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
edl() { docker exec "$FW" curl -sS --cacert /root/furnace.crt -u "lists:$EDL_PW" "https://$SHARED_IP/edl/$1"; }
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
check "the firewall needs the EDL login (401)" [ "$(code_from "$FW" "https://$SHARED_IP/edl/blocklist.txt")" = "401" ]
check "a wrong EDL password is refused (401)" \
    [ "$(docker exec "$FW" curl -s -o /dev/null -w '%{http_code}' --cacert /root/furnace.crt -u lists:wrong "https://$SHARED_IP/edl/blocklist.txt")" = "401" ]
r="$(docker exec "$XSOAR" curl -sS --cacert /root/furnace.crt -H "Authorization: Bearer $WL_TOKEN" -w ' %{http_code}' "https://$SHARED_IP/api/v1/ips/$TEST_IP" | tr -d '\n')"
check "the whitelist token may not use block calls (401)" grep -q ' 401$' <<< "$r"
r="$(api GET /api/v1/whitelist/ips)"
check "the soar token may read the whitelist (200)" grep -q ' 200$' <<< "$r"
r="$(api POST /api/v1/whitelist/ips '{"entry":"198.51.100.200"}')"
check "but not change it (401, names the permission)" grep -q 'whitelist write permission.* 401$' <<< "$r"
check "the last use of the token is shown" on "$LEADER" "furnace token list | grep -Eq '^soar .* 20[0-9-]{8} [0-9:]{5} +[0-9a-f]{8}$'"

step "API tokens, expiry and the wrong-token lockout"
out="$(on "$LEADER" "furnace token add lab 2>&1" || true)"
check "token add without a terminal needs the options" grep -q "Give --allow and --expires-days" <<< "$out"
out="$(on "$LEADER" "furnace token add lab --allow all --expires-days 30")"
LAB_TOKEN="$(grep -o 'fur_[A-Za-z0-9_-]*' <<< "$out")"
check "furnace token add shows a new token" [ -n "$LAB_TOKEN" ]
check "furnace token list never shows a token" on "$LEADER" "furnace token list | grep -q '^lab ' && ! furnace token list | grep -q fur_"
lab_code() {
    docker exec "$XSOAR" curl -sS --cacert /root/furnace.crt -H "Authorization: Bearer $LAB_TOKEN" -w ' %{http_code}' \
        "https://$SHARED_IP/api/v1/ips/$TEST_IP" | tr -d '\n'
}
check "the lab token works" grep -q ' 200$' <<< "$(lab_code)"
on "$LEADER" "runuser -u furnace -- sqlite3 /var/lib/furnace/furnace.db \"UPDATE api_tokens SET expires_at='2026-01-01T00:00:00Z' WHERE name='lab'\""
check "an expired token is refused with a clear message (401)" grep -q 'expired on 2026-01-01.* 401$' <<< "$(lab_code)"
check "furnace status warns about the expired token" on "$LEADER" "furnace status | grep -q \"WARNING: the API token 'lab' has expired\""
check "token rotate gives a new value for 30 days again" \
    on "$LEADER" "furnace token rotate lab | grep -q 'Expires: 20[0-9-]* (in 30 days)'"
check "token edit changes the permissions" \
    on "$LEADER" "furnace token edit lab --allow blocklist:read | grep -q 'blocklist read, whitelist -'"
for n in $(seq 1 10); do
    docker exec "$XSOAR" curl -s -o /dev/null --cacert /root/furnace.crt -H "Authorization: Bearer fur_wrong$n" \
        "https://$SHARED_IP/api/v1/ips/$TEST_IP"
done
check "after 10 wrong tokens even a good token is locked (429)" \
    [ "$(docker exec "$XSOAR" curl -s -o /dev/null -w '%{http_code}' --cacert /root/furnace.crt -H "Authorization: Bearer $TOKEN" "https://$SHARED_IP/api/v1/ips/$TEST_IP")" = "429" ]
check "furnace lockouts shows XSOAR" on "$LEADER" "furnace lockouts | grep -q '^$XSOAR_IP '"
check "furnace unlock frees it" on "$LEADER" "furnace unlock $XSOAR_IP | grep -q 'Unlocked 1'"
r="$(api GET "/api/v1/ips/$TEST_IP")"
check "XSOAR works again after the unlock" grep -q ' 200$' <<< "$r"
check "furnace token remove" on "$LEADER" "furnace token remove lab"

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
check "the follower got the tokens and the EDL login" \
    [ "$(on "$LEADER" 'furnace token list; furnace edl-user list')" = "$(on "$FOLLOWER" 'furnace token list; furnace edl-user list')" ]
check "the follower refuses token changes" on "$FOLLOWER" "furnace token add late 2>&1 | grep -q 'Run this on the leader'"

step "The furnace command: status and import"
check "status is fine on the leader" on "$LEADER" "furnace status"
check "status is fine on the follower" on "$FOLLOWER" "furnace status"
on "$LEADER" "printf '198.51.100.90\n' > /root/import.txt && chmod 600 /root/import.txt"
check "import from a file only root can read" on "$LEADER" "furnace import-ips /root/import.txt"
check "the imported IP is served" grep -qx "198.51.100.90" <<< "$(edl blocklist.txt)"

step "Package upgrade (apt install --reinstall) keeps data and tokens"
before_tokens="$(on "$LEADER" 'furnace token list; furnace edl-user list')"
out="$(pkg_reinstall "$LEADER")"
printf '%s\n' "$out" | tail -n 5
lab_allow_test_ranges
check "the upgrade ran setup with the saved settings" grep -q "self-test passed" <<< "$out"
check "the upgrade made no new secrets" bash -c '! grep -q "NEW SECRETS" <<< "$1"' _ "$out"
check "no secrets file after the upgrade" on "$LEADER" "! test -e /root/furnace-new-secrets.txt"
check "tokens and EDL login unchanged" [ "$before_tokens" = "$(on "$LEADER" 'furnace token list; furnace edl-user list')" ]
check "the old token still works" grep -q ' 200$' <<< "$(api GET "/api/v1/ips/$TEST_IP")"
check "the leader still owns the shared IP (no failover during upgrade)" on "$LEADER" "ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'"
check "the IP is still blocked" grep -qx "$TEST_IP" <<< "$(edl blocklist.txt)"
check "furnace stats works" on "$LEADER" "furnace stats | grep -q 'active ips *2'"

step "Failover: stop keepalived on the leader"
lab_allow_test_ranges
on "$LEADER" "furnace failover-test" && pass "failover test (machine down)" || fail "failover test (machine down)"
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

step "No IP limits: API_ALLOW_FROM=any and LISTS_ALLOW_FROM=any (token and login only)"
for box in "$LEADER" "$FOLLOWER"; do
    on "$box" "sed -i -e 's/^API_ALLOW_FROM=.*/API_ALLOW_FROM=any/' -e 's/^LISTS_ALLOW_FROM=.*/LISTS_ALLOW_FROM=any/' /etc/furnace/setup.conf"
    out="$(on "$box" "furnace setup --upgrade 2>&1")" && rc=0 || rc=$?
    check "$box: setup with 'any' passed its self-test" [ "$rc" = 0 ]
done
lab_allow_test_ranges
rules="$(fw_rules "$FOLLOWER")"
check "$FWTOOL opens the web port to everyone" fw_web_open_to_all "$rules"
check "the old per-IP web rules are gone" bash -c '! grep -q "$1" <<< "$2"' _ "$XSOAR_IP" "$rules"
check "XSOAR may read the EDL now, with the login" [ "$(docker exec "$XSOAR" curl -s -o /dev/null -w '%{http_code}' --cacert /root/furnace.crt -u "lists:$EDL_PW" "https://$SHARED_IP/edl/blocklist.txt")" = "200" ]
check "but not without the login (401)" [ "$(code_from "$XSOAR" "https://$SHARED_IP/edl/blocklist.txt")" = "401" ]
r="$(docker exec "$FW" curl -sS --cacert /root/furnace.crt -H "Authorization: Bearer $TOKEN" -w ' %{http_code}' "https://$SHARED_IP/api/v1/ips/$TEST_IP" | tr -d '\n')"
check "the firewall box may use the API now, with the token" grep -q ' 200$' <<< "$r"
check "but not without a token (401)" [ "$(code_from "$FW" "https://$SHARED_IP/api/v1/ips/$TEST_IP")" = "401" ]
check "/health stays closed (403)" [ "$(code_from "$FW" "https://$SHARED_IP/health")" = "403" ]

step "Failover: break the web service on the new leader (health check)"
on "$FOLLOWER" "furnace failover-test --web" && pass "failover test (web service down)" || fail "failover test (web service down)"
check "the shared IP is back on the first server" on "$LEADER" "ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'"

step "Uninstall on the follower: removing the package keeps the data"
pkg_remove "$FOLLOWER"
check "program removed" on "$FOLLOWER" "! test -e /usr/lib/furnace && ! command -v furnace"
check "services stopped" on "$FOLLOWER" "! systemctl is-active --quiet furnace-api && ! systemctl is-active --quiet keepalived"
check "nginx site removed" on "$FOLLOWER" "! test -e $NGINX_CONF"
check "the Furnace firewall rules are removed" bash -c '! grep -qiE "furnace|vrrp|port=\"443\"" <<< "$1"' _ "$(fw_rules "$FOLLOWER")"
check "database and EDL logins kept" on "$FOLLOWER" "test -f /var/lib/furnace/furnace.db && test -f /var/lib/furnace-auth/edl-users"
if [ "$KIND" = deb ]; then
    on "$FOLLOWER" "apt-get purge -y -qq furnace >/dev/null 2>&1"
    check "purge deleted the data and the users" on "$FOLLOWER" "! test -e /var/lib/furnace && ! test -e /var/lib/furnace-auth && ! test -e /etc/furnace && ! id furnace >/dev/null 2>&1"
fi
check "the leader keeps working" grep -qx "$TEST_IP" <<< "$(edl blocklist.txt)"

printf '\n%s passed, %s failed (%s)\n' "$PASSED" "$FAILED" "$SYSTEM"
[ "$FAILED" -eq 0 ]

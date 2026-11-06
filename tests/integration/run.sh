#!/usr/bin/env bash
# Furnace integration test with Docker: two servers, a fake firewall (reads the lists) and a fake SOAR (calls the API).
# It builds the .deb, installs it like a user would, and answers the setup questions.
# Only documentation addresses are used (192.0.2.0/24). Nothing real is touched.
#
#   tests/integration/run.sh [SYSTEM]     run the test on SYSTEM (default ubuntu:24.04)
#   KEEP=1 tests/integration/run.sh       leave the containers running afterwards
#   NET_PREFIX=10.99.0 tests/integration/run.sh   another /24 for the test network (default 192.0.2), when
#                                          192.0.2.0/24 is already used by another Docker network
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
NET_PREFIX="${NET_PREFIX:-192.0.2}"
LEADER_IP=$NET_PREFIX.11
FOLLOWER_IP=$NET_PREFIX.12
SHARED_IP=$NET_PREFIX.10
FW_IP=$NET_PREFIX.50
XSOAR_IP=$NET_PREFIX.60
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
docker network create --subnet "$NET_PREFIX.0/24" --gateway "$NET_PREFIX.1" "$NET" >/dev/null

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
# The new IP names: ONE file for both servers, each finds its role from its own IP.
SAME_ENV="LEADER_IP=$LEADER_IP
FOLLOWER_IP=$FOLLOWER_IP
SHARED_IP=$SHARED_IP"
put "$LEADER" /root/same.env <<< "$SAME_ENV"
put "$FOLLOWER" /root/same.env <<< "$SAME_ENV"
out="$(on "$LEADER" "furnace setup --check --config /root/same.env 2>&1" || true)"
check "the same file: the leader finds it is the leader" grep -q "Settings: role leader, this server $LEADER_IP" <<< "$out"
out="$(on "$FOLLOWER" "furnace setup --config /root/same.env 2>&1" || true)"
check "the same file: the follower finds it is the follower (and needs the leader bundle)" \
    grep -q "needs furnace-peer-bundle.tar.gz" <<< "$out"
put "$LEADER" /root/neither.env <<< "LEADER_IP=$NET_PREFIX.98
FOLLOWER_IP=$NET_PREFIX.99
SHARED_IP=$SHARED_IP"
out="$(on "$LEADER" "furnace setup --check --config /root/neither.env 2>&1" || true)"
check "neither IP on this server is explained" grep -q "This server has neither $NET_PREFIX.98 (LEADER_IP)" <<< "$out"
put "$LEADER" /root/mixed.env <<< "ROLE=leader
LEADER_IP=$LEADER_IP
PEER_IP=$FOLLOWER_IP
SHARED_IP=$SHARED_IP"
out="$(on "$LEADER" "furnace setup --check --config /root/mixed.env 2>&1" || true)"
check "old and new IP names together are refused" grep -q "not both" <<< "$out"

step "Set up the leader by answering the questions"
TAB=$'\t'
put "$LEADER" /root/answers.txt <<EOF
leader or the follower?${TAB}
Network card name [${TAB}$LEADER_IP
Network card name [${TAB}
This server's IP (the leader) [${TAB}
The follower server's IP:${TAB}999.1.1.1
The follower server's IP:${TAB}$FOLLOWER_IP
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
check "the host firewall was off: setup asked, then turned it on" grep -q "was off: turned it on" <<< "$out"
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
check "the network card and IP were found automatically" on "$LEADER" "grep -qx 'INTERFACE=eth0' /etc/furnace/setup.conf && grep -qx 'LEADER_IP=$LEADER_IP' /etc/furnace/setup.conf && ! grep -q '^THIS_IP=' /etc/furnace/setup.conf"
check "the certificate is valid for itself and has no extension twice" on "$LEADER" \
    "openssl verify -CAfile /etc/furnace/tls/furnace.crt /etc/furnace/tls/furnace.crt >/dev/null && ! openssl x509 -in /etc/furnace/tls/furnace.crt -noout -text | grep -oE '^ +X509v3 [A-Za-z ]+:' | sort | uniq -d | grep -q ."
check "a keepalived password was created" on "$LEADER" "grep -Eqx 'KEEPALIVED_PASSWORD=[A-Za-z0-9]{8}' /etc/furnace/setup.conf"
check "the saved settings are readable by root only" on "$LEADER" "[ \"\$(stat -c '%U %a' /etc/furnace/setup.conf)\" = 'root 600' ]"
check "leader owns the shared IP" on "$LEADER" "ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'"
check "no tokens file any more" on "$LEADER" "! test -e /etc/furnace/tokens.env"
check "no secret in the setup log" on "$LEADER" "! grep -qF -e '$TOKEN' -e '$WL_TOKEN' -e '$EDL_PW' /var/log/furnace-setup.log"
check "no secret in the database" on "$LEADER" "! sqlite3 /var/lib/furnace/furnace.db .dump | grep -qF -e '$TOKEN' -e '$EDL_PW'"
check "the EDL users file has a SHA-512 hash, readable by nginx only" \
    on "$LEADER" "grep -q '^lists:\\\$6\\\$' /var/lib/furnace-auth/edl-users && [ \"\$(stat -c '%U %G %a' /var/lib/furnace-auth/edl-users)\" = 'furnace $NGINX_GROUP 640' ]"
check "cleanup timer runs on the leader" on "$LEADER" "systemctl is-active --quiet furnace-cleanup.timer"

step "Set up the follower (bundle copied from the leader: the role, the card and turning on the host firewall)"
copy_between "$LEADER" /root/furnace-peer-bundle.tar.gz "$FOLLOWER" /root/furnace-peer-bundle.tar.gz
put "$FOLLOWER" /root/answers.txt <<EOF
leader or the follower?${TAB}follower
Network card name [${TAB}
own firewall ($FWTOOL)?${TAB}
Set up Furnace with these settings?${TAB}y
EOF
out="$(on "$FOLLOWER" "$FPY /root/answer.py /root/answers.txt -- furnace setup" 2>&1)" && rc=0 || rc=$?
printf '%s\n' "$out"
check "follower setup finished with the self-test passed" [ "$rc" = 0 ]
check "the follower asked before it turned on its host firewall" grep -q "Turn on and set up this server's own firewall ($FWTOOL)?" <<< "$out"
check "follower took its settings from the bundle" on "$FOLLOWER" "grep -qx 'ROLE=follower' /etc/furnace/setup.conf && grep -qx 'FOLLOWER_IP=$FOLLOWER_IP' /etc/furnace/setup.conf && grep -qx 'LEADER_IP=$LEADER_IP' /etc/furnace/setup.conf && grep -qx 'API_ALLOW_FROM=$XSOAR_IP' /etc/furnace/setup.conf"
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
r="$(api POST /api/v1/ips "{\"ip\":\"$TEST_IP\",\"reason\":\"integration test\",\"added_by\":\"tester\"}")"
check "XSOAR adds an IP (201)" grep -q ' 201$' <<< "$r"
check "the firewall sees the IP in blocklist.txt" grep -qx "$TEST_IP" <<< "$(edl blocklist.txt)"
r="$(api POST /api/v1/domains '{"domain":"bad.example.com","added_by":"tester"}')"
check "XSOAR adds a domain (201)" grep -q ' 201$' <<< "$r"
check "the firewall sees the domain in domains.txt" grep -qx "bad.example.com" <<< "$(edl domains.txt)"
PERM_IP=203.0.113.77
r="$(api POST /api/v1/ips "{\"ip\":\"$PERM_IP\",\"reason\":\"known C2\",\"permanent\":true}")"
check "XSOAR adds a permanent IP (201, permanent)" grep -Eq '"permanent": ?true.* 201$' <<< "$r"
r="$(api POST /api/v1/ips "{\"ip\":\"$PERM_IP\",\"permanent\":\"yes\"}")"
check "permanent must be true or false (422)" grep -q 'invalid_permanent.* 422$' <<< "$r"
check "furnace stats counts the permanent IP" on "$LEADER" "furnace stats | grep -Eq '^permanent ips +1$'"
r="$(api DELETE "/api/v1/ips/$PERM_IP")"
check "XSOAR unblocks the permanent IP (200)" grep -q ' 200$' <<< "$r"
check "the unblocked IP left blocklist.txt" bash -c "! grep -qx '$PERM_IP' <<< \"\$1\"" _ "$(edl blocklist.txt)"
r="$(api GET "/api/v1/ips/$PERM_IP")"
check "the unblocked IP is still in the archive (200, archive)" grep -Eq '"list": ?"archive".* 200$' <<< "$r"
check "furnace export archive lists it" on "$LEADER" "furnace export archive | grep -q '^ip,$PERM_IP,'"
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

step "Bulk upload of a blocklist file through the API"
bulk() {  # bulk PATH QUERY TEXT -> prints "BODY HTTP_CODE"
    printf '%b' "$3" | docker exec -i "$XSOAR" curl -sS --cacert /root/furnace.crt -H "Authorization: Bearer $TOKEN" \
        -H 'Content-Type: text/plain' --data-binary @- -w ' %{http_code}' "https://$SHARED_IP$1?$2" | tr -d '\n'
}
FILE='ip,date,reason\n203.0.113.90\n203.0.113.91,,scanner\n203.0.113.91\n10.0.0.9\n'
r="$(bulk /api/v1/ips/bulk dry_run=true "$FILE")"
check "a dry run shows what would happen (2 added, 3 skipped)" grep -Eq '"dry_run": ?true.*"added": ?2.*"skipped": ?3.* 200$' <<< "$r"
check "the dry run changed nothing" bash -c "! grep -qx '203.0.113.90' <<< \"\$1\"" _ "$(edl blocklist.txt)"
r="$(bulk /api/v1/ips/bulk "added_by=bulk-test" "$FILE")"
check "the file is uploaded (200, 2 added)" grep -Eq '"added": ?2.* 200$' <<< "$r"
check "the skipped lines are named" bash -c 'grep -q duplicate_in_file <<< "$1" && grep -q private_ip <<< "$1"' _ "$r"
check "the uploaded IPs are in blocklist.txt" bash -c 'grep -qx 203.0.113.90 <<< "$1" && grep -qx 203.0.113.91 <<< "$1"' _ "$(edl blocklist.txt)"
r="$(bulk /api/v1/domains/bulk "" 'bulk1.example.com\nbulk2.example.org,malware\n')"
check "a domain file is uploaded (200, 2 added)" grep -Eq '"added": ?2.* 200$' <<< "$r"
r="$(docker exec "$XSOAR" curl -sS --cacert /root/furnace.crt -H "Authorization: Bearer $WL_TOKEN" -H 'Content-Type: text/plain' \
     --data-binary '203.0.113.92' -w ' %{http_code}' "https://$SHARED_IP/api/v1/ips/bulk" | tr -d '\n')"
check "the whitelist token may not upload a blocklist (401)" grep -q ' 401$' <<< "$r"
for ip in 203.0.113.90 203.0.113.91; do api DELETE "/api/v1/ips/$ip" >/dev/null; done
for d in bulk1.example.com bulk2.example.org; do api DELETE "/api/v1/domains/$d" >/dev/null; done

step "URL blocklist"
r="$(api POST /api/v1/urls '{"url":"https://Bad.Example.com/Phish/","reason":"phishing"}')"
check "XSOAR adds a URL; https:// is removed (201)" grep -Eq '"url": ?"bad.example.com/phish/".* 201$' <<< "$r"
check "the firewall sees the URL in urls.txt" grep -qx "bad.example.com/phish/" <<< "$(edl urls.txt)"
r="$(api POST /api/v1/urls '{"url":"*.com/"}')"
check "a too wide wildcard is refused (422)" grep -q 'wildcard_too_wide.* 422$' <<< "$r"
r="$(bulk /api/v1/urls/bulk "" 'url\n*.evil.example.org malware\nhttp://bad.example.net/x\n')"
check "a URL file is uploaded (200, 2 added)" grep -Eq '"added": ?2.* 200$' <<< "$r"
check "the uploaded URLs are in urls.txt" bash -c 'grep -qx "\*.evil.example.org/" <<< "$1" && grep -qx bad.example.net/x <<< "$1"' _ "$(edl urls.txt)"
docker exec "$XSOAR" curl -sS --cacert /root/furnace.crt -H "Authorization: Bearer $TOKEN" -X DELETE \
    "https://$SHARED_IP/api/v1/urls?url=bad.example.net/x" >/dev/null
check "a URL is removed (gone from urls.txt)" bash -c '! grep -qx bad.example.net/x <<< "$1"' _ "$(edl urls.txt)"
check "furnace export urls lists them" on "$LEADER" "furnace export urls | grep -q '^bad.example.com/phish/,'"

step "Domain whitelist: subdomains and shared platforms"
wl() {  # wl METHOD PATH [JSON] with the whitelist token -> prints "BODY HTTP_CODE"
    local data=()
    [ -n "${3:-}" ] && data=(-H 'Content-Type: application/json' -d "$3")
    docker exec "$XSOAR" curl -sS --cacert /root/furnace.crt -H "Authorization: Bearer $WL_TOKEN" \
        -X "$1" "${data[@]}" -w ' %{http_code}' "https://$SHARED_IP$2" | tr -d '\n'
}
r="$(wl POST /api/v1/whitelist/domains '{"domain":"github.io"}')"
check "a shared platform cannot be whitelisted (422)" grep -q 'whitelist_not_allowed.* 422$' <<< "$r"
check "setup made the local refuse file" on "$LEADER" "[ -f /etc/furnace/whitelist-refuse.txt ]"
api POST /api/v1/domains '{"domain":"mail.wl.example.com"}' >/dev/null
api POST /api/v1/urls '{"url":"www.wl.example.com/x"}' >/dev/null
r="$(wl POST /api/v1/whitelist/domains '{"domain":"wl.example.com"}')"
check "whitelisting a domain removes its blocked subdomains and URLs (201)" \
    grep -Eq '"removed_from_blocklist": ?\["mail.wl.example.com"\].*"urls_removed_from_blocklist": ?\["www.wl.example.com/x"\].* 201$' <<< "$r"
r="$(api POST /api/v1/domains '{"domain":"new.wl.example.com"}')"
check "a new subdomain of it is refused (422 whitelisted)" grep -q '"whitelisted".* 422$' <<< "$r"
wl DELETE /api/v1/whitelist/domains/wl.example.com >/dev/null
check "the test entries are removed again" bash -c "! grep -qx '203.0.113.90' <<< \"\$1\"" _ "$(edl blocklist.txt)"

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
check "replication runs every 20 seconds" \
    on "$LEADER" "[ \"\$(systemctl show furnace-replicate.timer -p TimersMonotonic --value | grep -o 'OnUnitActiveUSec=[^ ;]*')\" = OnUnitActiveUSec=20s ]"
# Wait for the LAST change (the URL, the removed token), not only the first: a copy every 20 seconds can land
# between two changes (found in the 0.8.0 round).
ok=0
for i in $(seq 1 90); do
    if on "$FOLLOWER" "grep -qx $TEST_IP /var/lib/furnace-edl/blocklist.txt && grep -qx bad.example.com/phish/ /var/lib/furnace-edl/urls.txt" \
        && [ "$(on "$LEADER" 'furnace token list; furnace edl-user list')" = "$(on "$FOLLOWER" 'furnace token list; furnace edl-user list')" ]; then
        ok=1; break
    fi
    sleep 1
done
echo "  everything replicated after about $i seconds"
check "the follower has the IP in its blocklist.txt" [ "$ok" = 1 ]
check "the follower has the domain" on "$FOLLOWER" "grep -qx bad.example.com /var/lib/furnace-edl/domains.txt"
check "the follower has the URL" on "$FOLLOWER" "grep -qx bad.example.com/phish/ /var/lib/furnace-edl/urls.txt"
check "the follower has the archive record" \
    on "$FOLLOWER" "[ \"\$(sqlite3 /var/lib/furnace/furnace.db \"SELECT COUNT(*) FROM ip_records WHERE ip='$PERM_IP'\")\" = 1 ]"
r="$(docker exec "$XSOAR" curl -sS --cacert /root/furnace.crt -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"ip":"198.51.100.78"}' -w ' %{http_code}' "https://$FOLLOWER_IP/api/v1/ips" | tr -d '\n')"
check "the follower refuses changes (503 not_leader)" grep -q 'not_leader.* 503$' <<< "$r"
check "the follower got the tokens and the EDL login" \
    [ "$(on "$LEADER" 'furnace token list; furnace edl-user list')" = "$(on "$FOLLOWER" 'furnace token list; furnace edl-user list')" ]
check "the follower refuses token changes" on "$FOLLOWER" "furnace token add late 2>&1 | grep -q 'Run this on the leader'"

step "Replication goes over HTTPS (SSH only as the fallback for an older follower)"
check "both servers have the same replication secret, readable by the furnace group only" bash -c '
    [ "$(docker exec "$1" sha256sum < /etc/furnace/secrets/replica-secret)" = "$(docker exec "$2" sha256sum < /etc/furnace/secrets/replica-secret)" ] &&
    [ "$(docker exec "$1" stat -c "%U %G %a" /etc/furnace/secrets/replica-secret)" = "root furnace 640" ] &&
    [ "$(docker exec "$1" stat -c "%a" /etc/furnace/tls/furnace.key)" = 600 ]' _ "$LEADER" "$FOLLOWER"
last_result() { on "$LEADER" "cat /var/lib/furnace/replica/last-result 2>/dev/null" || true; }
wait_result() {  # wait_result PATTERN: until the leader's last replication result matches (up to 90 s)
    local _
    for _ in $(seq 1 90); do grep -Eq "$1" <<< "$(last_result)" && return 0; sleep 1; done
    return 1
}
check "the leader sends over HTTPS" wait_result ' ok over HTTPS: sent'
check "status shows the replication" bash -c 'grep -q "Replication: ok over HTTPS" <<< "$1"' _ "$(on "$LEADER" "furnace status")"
check "only the other server may send a snapshot (403)" \
    [ "$(code_from "$XSOAR" "https://$FOLLOWER_IP/replica/snapshot")" = "403" ]
# An older follower (0.7) has no HTTPS replication: take it away for a moment, the leader must use SSH.
on "$FOLLOWER" "sed -i 's/^FURNACE_REPLICA_SECRET_FILE=/#OFF_FURNACE_REPLICA_SECRET_FILE=/' /etc/furnace/furnace.env && systemctl restart furnace-api"
check "with an older follower the leader falls back to SSH" wait_result ' ok over SSH \(HTTPS: .*older Furnace'
api POST /api/v1/ips '{"ip":"198.51.100.79","reason":"over ssh"}' >/dev/null
check "and the follower still gets the changes" bash -c 'for _ in $(seq 1 90); do
    docker exec "$1" grep -qx 198.51.100.79 /var/lib/furnace-edl/blocklist.txt && exit 0; sleep 1; done; exit 1' _ "$FOLLOWER"
check "status names the fallback" bash -c 'grep -q "SSH is the fallback" <<< "$1"' _ "$(on "$LEADER" "furnace status")"
on "$FOLLOWER" "sed -i 's/^#OFF_FURNACE_REPLICA_SECRET_FILE=/FURNACE_REPLICA_SECRET_FILE=/' /etc/furnace/furnace.env && systemctl restart furnace-api"
check "after the upgrade of the follower it is HTTPS again" wait_result ' ok over HTTPS: sent'
api DELETE /api/v1/ips/198.51.100.79 >/dev/null
# Let the next copy carry the delete before the failover below. Otherwise the new leader still has the IP
# (changes of the last 20 seconds are lost in a failover: the known window, README 10.1).
check "the follower got the delete too" bash -c 'for _ in $(seq 1 60); do
    docker exec "$1" grep -qx 198.51.100.79 /var/lib/furnace-edl/blocklist.txt || exit 0; sleep 1; done; exit 1' _ "$FOLLOWER"

step "The furnace command: status and import"
check "status is fine on the leader" on "$LEADER" "furnace status"
check "status is fine on the follower" on "$FOLLOWER" "furnace status"
on "$LEADER" "printf '198.51.100.90\n' > /root/import.txt && chmod 600 /root/import.txt"
check "import from a file only root can read" on "$LEADER" "furnace import-ips /root/import.txt"
check "the imported IP is served" grep -qx "198.51.100.90" <<< "$(edl blocklist.txt)"

step "Package upgrade (apt install --reinstall) keeps data and tokens"
before_tokens="$(on "$LEADER" 'furnace token list; furnace edl-user list')"
# Make the saved settings look like 0.9.0 (THIS_IP/PEER_IP): the upgrade must keep them and save the new names.
on "$LEADER" "sed -i -e 's/^LEADER_IP=/THIS_IP=/' -e 's/^FOLLOWER_IP=/PEER_IP=/' /etc/furnace/setup.conf"
out="$(pkg_reinstall "$LEADER")"
printf '%s\n' "$out" | tail -n 5
lab_allow_test_ranges
check "the upgrade ran setup with the saved settings" grep -q "self-test passed" <<< "$out"
check "settings from 0.9.0 (THIS_IP/PEER_IP) are saved with the new names" on "$LEADER" \
    "grep -qx 'LEADER_IP=$LEADER_IP' /etc/furnace/setup.conf && grep -qx 'FOLLOWER_IP=$FOLLOWER_IP' /etc/furnace/setup.conf && ! grep -q '^THIS_IP=' /etc/furnace/setup.conf"
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
# Right after the move the new leader can answer 503 for a moment (its role file is written by keepalived's
# notify script). Callers retry on 503 (docs/API.md section 14), so the test does too, for up to 15 seconds.
for i in $(seq 1 15); do
    r="$(api POST /api/v1/ips '{"ip":"198.51.100.79","added_by":"tester"}')"
    grep -q ' 503$' <<< "$r" || break
    [ "$i" = 1 ] && echo "  first answer: ${r: -160}"
    sleep 1
done
echo "  added on try $i"
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

step "A keepalived crash on the leader (kill -9) must not leave the shared IP on both servers"
check "keepalived has Restart=on-failure" [ "$(on "$LEADER" 'systemctl show -p Restart --value keepalived')" = "on-failure" ]
on "$LEADER" "pkill -9 -x keepalived"
# systemd starts keepalived again after 2 s. Which server then holds the shared IP depends on timing: the follower
# (3 missed adverts, about 3.6 s) or the restarted leader. Both are fine (found in the 0.8.0 round). What must hold:
# never both, then exactly one owner, and the role files agree with it. Checked every second; the timeline is logged.
both=0; stable=0; timeline=""; prev=""
for i in $(seq 1 40); do
    sleep 1
    l=0; f=0
    on "$LEADER" "ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'" && l=1
    on "$FOLLOWER" "ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'" && f=1
    [ "$l$f" = 11 ] && both=1
    timeline+=" ${i}s:L$l/F$f"
    # The end state: one owner for 8 seconds in a row (longer than a takeover), keepalived running again.
    if [ "$l$f" = 10 ] || [ "$l$f" = 01 ]; then
        [ "$l$f" = "$prev" ] && stable=$((stable + 1)) || stable=1
    else
        stable=0
    fi
    prev="$l$f"
    [ "$stable" -ge 8 ] && on "$LEADER" "systemctl is-active --quiet keepalived" && break
done
echo "  timeline (L = crashed leader, F = follower, 1 = owns the shared IP):$timeline"
if [ "$prev" = 01 ]; then OWNER=$FOLLOWER; OTHER=$LEADER; else OWNER=$LEADER; OTHER=$FOLLOWER; fi
echo "  the shared IP is on $OWNER"
check "the shared IP was never on both servers" [ "$both" = 0 ]
check "systemd started keepalived again on the crashed server" on "$LEADER" "systemctl is-active --quiet keepalived"
check "then exactly one server holds the shared IP" [ "$stable" -ge 8 ]
check "its role file says leader, the other one's says follower" \
    [ "$(on "$OWNER" 'cat /run/furnace-state/role'):$(on "$OTHER" 'cat /run/furnace-state/role')" = "leader:follower" ]
check "the firewall still gets the list" grep -qx "$TEST_IP" <<< "$(edl blocklist.txt)"

step "An expired certificate does not fail the health check (both servers share it)"
# On the server that holds the shared IP: if the health check failed there, nobody would hold it.
on "$OWNER" 'set -e; d=$(mktemp -d); cd "$d"; touch index.txt; echo 1000 > serial; mkdir certs
printf "[ca]\ndefault_ca=c\n[c]\ndir=%s\ndatabase=\$dir/index.txt\nnew_certs_dir=\$dir/certs\nserial=\$dir/serial\ndefault_md=sha256\npolicy=p\nx509_extensions=e\n[p]\ncommonName=supplied\n[e]\nsubjectAltName=IP:127.0.0.1\n" "$d" > ca.cnf
openssl req -new -key /etc/furnace/tls/furnace.key -subj "/CN=expired" -out r.csr
openssl ca -batch -config ca.cnf -selfsign -keyfile /etc/furnace/tls/furnace.key -in r.csr \
    -startdate 20250101000000Z -enddate 20250201000000Z -notext -out expired.crt 2>/dev/null
cp /etc/furnace/tls/furnace.crt /root/furnace.crt.keep
install -m 0644 expired.crt /etc/furnace/tls/furnace.crt; systemctl reload nginx; sleep 1; rm -rf "$d"'
check "the served certificate is expired" on "$OWNER" "! openssl s_client -connect 127.0.0.1:443 </dev/null 2>/dev/null | openssl x509 -noout -checkend 0"
check "the health check still passes" on "$OWNER" "/usr/lib/furnace/bin/furnace-health-check"
sleep 16
check "the server kept the shared IP (no FAULT)" on "$OWNER" "ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'"
on "$OWNER" "install -m 0644 /root/furnace.crt.keep /etc/furnace/tls/furnace.crt && rm -f /root/furnace.crt.keep && systemctl reload nginx"
check "the real certificate is back" grep -qx "$TEST_IP" <<< "$(edl blocklist.txt)"
# Give the shared IP back to the first server: the uninstall step below removes the second one.
[ "$OWNER" = "$FOLLOWER" ] && on "$FOLLOWER" "systemctl restart keepalived"
ok=0
for _ in $(seq 1 20); do
    sleep 1
    on "$LEADER" "ip -4 addr show eth0 | grep -q 'inet $SHARED_IP/'" && { ok=1; break; }
done
check "the shared IP is back on the first server" [ "$ok" = 1 ]

step "Uninstall on the follower: removing the package keeps the data"
pkg_remove "$FOLLOWER"
check "program removed" on "$FOLLOWER" "! test -e /usr/lib/furnace && ! command -v furnace"
check "services stopped" on "$FOLLOWER" "! systemctl is-active --quiet furnace-api && ! systemctl is-active --quiet keepalived"
check "nginx site removed" on "$FOLLOWER" "! test -e $NGINX_CONF"
check "keepalived restart drop-in removed" on "$FOLLOWER" "! test -e /etc/systemd/system/keepalived.service.d/furnace.conf"
check "the Furnace firewall rules are removed" bash -c '! grep -qiE "furnace|vrrp|port=\"443\"" <<< "$1"' _ "$(fw_rules "$FOLLOWER")"
check "database and EDL logins kept" on "$FOLLOWER" "test -f /var/lib/furnace/furnace.db && test -f /var/lib/furnace-auth/edl-users"
if [ "$KIND" = deb ]; then
    on "$FOLLOWER" "apt-get purge -y -qq furnace >/dev/null 2>&1"
    check "purge deleted the data and the users" on "$FOLLOWER" "! test -e /var/lib/furnace && ! test -e /var/lib/furnace-auth && ! test -e /etc/furnace && ! id furnace >/dev/null 2>&1"
fi
check "the leader keeps working" grep -qx "$TEST_IP" <<< "$(edl blocklist.txt)"

printf '\n%s passed, %s failed (%s)\n' "$PASSED" "$FAILED" "$SYSTEM"
[ "$FAILED" -eq 0 ]

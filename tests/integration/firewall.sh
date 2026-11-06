#!/usr/bin/env bash
# Furnace host firewall test: setup configures the ACTIVE host firewall (user decision 2026-11-05).
#
#   firewalld running             -> firewalld (also on Ubuntu, where ufw is installed too)
#   ufw active                    -> ufw
#   both active                   -> setup stops and explains; HOST_FIREWALL=none still works
#   none active                   -> the installed one, turned on only after a yes (or HOST_FIREWALL in the file)
#   package upgrade (--upgrade)   -> never stops, never turns a firewall on: it warns
#
#   tests/integration/firewall.sh [SYSTEM]   SYSTEM: ubuntu:22.04, ubuntu:24.04 (default), debian:12, debian:13
#   KEEP=1 ...                              leave the last container running
#   NET_PREFIX=10.99.1 ...                  another /24 (default 203.0.113)
#
# One leader per case, each in a fresh container made from one prepared image (Furnace + firewalld installed).
# Only documentation addresses are used. Needs docker and privileged containers.
set -Eeuo pipefail
export MSYS_NO_PATHCONV=1

SYSTEM="${1:-ubuntu:24.04}"
[[ "$SYSTEM" == *:* ]] || SYSTEM="ubuntu:$SYSTEM"
case "$SYSTEM" in ubuntu:*|debian:*) ;; *) echo "firewall.sh runs on the Debian family (ufw and firewalld side by side)."; exit 2 ;; esac
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NET=furnace-fwtest
IMAGE="furnace-test-${SYSTEM/:/-}"
READY="furnace-fwtest-${SYSTEM/:/-}"
BOX=furnace-fwbox
NET_PREFIX="${NET_PREFIX:-203.0.113}"
THIS_IP=$NET_PREFIX.11
PEER_IP=$NET_PREFIX.12
SHARED_IP=$NET_PREFIX.10
PKG="$ROOT/dist/furnace-test.deb"
FPY=/usr/lib/furnace/bin/furnace-python
TAB=$'\t'
PASSED=0
FAILED=0

step() { printf '\n### %s\n' "$*"; }
pass() { printf '  PASS  %s\n' "$*"; PASSED=$((PASSED + 1)); }
fail() { printf '  FAIL  %s\n' "$*"; FAILED=$((FAILED + 1)); }
check() { local name="$1"; shift; if "$@"; then pass "$name"; else fail "$name"; fi; }
nott() { ! "$@"; }
none_of() { local f; for f; do "$f" && return 1; done; return 0; }
all_of() { local f; for f; do "$f" || return 1; done; return 0; }
on() { docker exec "$BOX" bash -c "$*"; }
put() { docker exec -i "$BOX" bash -c "cat > '$1'"; }

remove_box() { docker rm -f "$BOX" >/dev/null 2>&1 || true; }
teardown() {
    if [ "${KEEP:-0}" = "1" ]; then echo "KEEP=1: $BOX is still running."; return; fi
    remove_box
    docker network rm "$NET" >/dev/null 2>&1 || true
}
trap teardown EXIT

wait_boot() {
    local state
    for _ in $(seq 1 40); do
        state="$(docker exec "$BOX" systemctl is-system-running 2>/dev/null || true)"
        case "$state" in running|degraded) return 0 ;; esac
        sleep 1
    done
}
fresh_box() {  # a new leader container from the prepared image
    remove_box
    docker run -d --name "$BOX" --hostname "$BOX" --network "$NET" --ip "$THIS_IP" \
        --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw "$READY" >/dev/null
    wait_boot
    put /root/answer.py < "$ROOT/tests/integration/answer.py"
    put /root/leader.env <<EOF
ROLE=leader
PEER_IP=$PEER_IP
SHARED_IP=$SHARED_IP
EOF
}
ufw_on() { on "ufw --force enable >/dev/null"; }
firewalld_on() { on "systemctl start firewalld && firewall-cmd --state >/dev/null"; }
ufw_is_active() { on "ufw status | grep -q '^Status: active'"; }
firewalld_is_active() { on "systemctl is-active --quiet firewalld"; }
ufw_has_furnace() { on "ufw status | grep -q furnace"; }
firewalld_has_furnace() { on "firewall-cmd --list-rich-rules | grep -q 'port=\"443\"' && firewall-cmd --list-rich-rules | grep -q vrrp"; }
not_set_up() { on "! test -e /etc/furnace/setup.conf"; }
saved_fw() { on "grep -qx 'HOST_FIREWALL=$1' /etc/furnace/setup.conf"; }
setup_config() {  # setup_config [EXTRA_LINE]: setup --config with leader.env (+ a line); sets OUT and RC
    on "cp /root/leader.env /root/this.env"
    [ -n "${1:-}" ] && on "echo '$1' >> /root/this.env"
    OUT="$(on "furnace setup --config /root/this.env 2>&1")" && RC=0 || RC=$?
    printf '%s\n' "$OUT" | grep -E 'ERROR|warning|firewall|Self-test passed|self-test passed' | sed 's/^/      | /' || true
}
setup_ask() {  # setup_ask FIREWALL_PROMPT ANSWER: the leader questions; sets OUT and RC
    put /root/answers.txt <<EOF
leader or the follower?${TAB}
Network card name [${TAB}
This server's IP [${TAB}
The other Furnace server's IP:${TAB}$PEER_IP
Shared IP:${TAB}$SHARED_IP
Network prefix${TAB}
Who may call the API [any]:${TAB}
Who may download the lists [any]:${TAB}
Most entries per list${TAB}
Days an IP stays${TAB}
VRRP router ID (1-255)${TAB}
SSH allowed from${TAB}
$1${TAB}$2
EOF
    if [ "${3:-}" = "full" ]; then
        put /root/more.txt <<EOF
Token name [api]:${TAB}
Box number, or Enter when done:${TAB}
Token expires after how many days${TAB}0
Set up Furnace with these settings?${TAB}y
EOF
        on "cat /root/more.txt >> /root/answers.txt"
    fi
    OUT="$(on "$FPY /root/answer.py /root/answers.txt -- furnace setup 2>&1")" && RC=0 || RC=$?
    printf '%s\n' "$OUT" | grep -E 'ERROR|warning|firewall|Stopped|self-test passed' | sed 's/^/      | /' || true
}

step "Build the package and one prepared image ($SYSTEM, Furnace and firewalld installed, both firewalls off)"
BUILD_DIR="$(cd "$ROOT/tests/integration" && (pwd -W 2>/dev/null || pwd))"
docker build -q --load --build-arg "BASE=$SYSTEM" -f "$BUILD_DIR/Dockerfile" -t "$IMAGE" "$BUILD_DIR" >/dev/null
mkdir -p "$ROOT/dist"
tar -C "$ROOT" --exclude=.git --exclude=__pycache__ --exclude=.pytest_cache --exclude=dist --exclude=build/vendor-test -cf - . \
    | docker run -i --rm "$IMAGE" bash -c \
        'mkdir /src && tar -xf - -C /src && /src/packaging/build-deb.sh /out >/dev/null && cat /out/*.deb' > "$PKG"
check "the package was built" test -s "$PKG"
docker network rm "$NET" >/dev/null 2>&1 || true
docker network create --subnet "$NET_PREFIX.0/24" --gateway "$NET_PREFIX.1" "$NET" >/dev/null
remove_box
docker run -d --name "$BOX" --hostname "$BOX" --network "$NET" --ip "$THIS_IP" \
    --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw "$IMAGE" >/dev/null
wait_boot
put /root/furnace.deb < "$PKG"
on "apt-get update -qq >/dev/null && apt-get install -y -qq /root/furnace.deb firewalld >/dev/null 2>&1"
on "systemctl disable --now firewalld >/dev/null 2>&1; ufw --force disable >/dev/null; rm -f /root/furnace.deb"
check "Furnace, ufw and firewalld are installed" on "command -v furnace && command -v ufw && command -v firewall-cmd >/dev/null"
docker commit "$BOX" "$READY" >/dev/null

step "1. Ubuntu/Debian with firewalld running (ufw installed, off): Furnace uses firewalld"
fresh_box
firewalld_on
setup_config
check "setup finished" [ "$RC" = 0 ]
check "firewalld has the Furnace rules (443 and VRRP)" firewalld_has_furnace
check "ufw stayed off (no second firewall turned on)" nott ufw_is_active
check "HOST_FIREWALL=firewalld was saved" saved_fw firewalld

step "1b. Package upgrade after ufw was turned on too (both active): warns, changes neither, does not stop"
ufw_on
OUT="$(on "furnace setup --upgrade 2>&1")" && RC=0 || RC=$?
check "the upgrade finished" [ "$RC" = 0 ]
check "the upgrade warns about two firewalls" grep -q "Two host firewalls are active (ufw and firewalld). Furnace changed neither" <<< "$OUT"
check "the firewalld rules are still there" firewalld_has_furnace
check "no Furnace rules were added to ufw" nott ufw_has_furnace
check "the saved choice is kept (firewalld)" saved_fw firewalld
on "ufw --force disable >/dev/null"

step "1c. Uninstall: removes the firewalld rules, firewalld stays on"
on "apt-get remove -y -qq furnace >/dev/null 2>&1"
check "no Furnace rules in firewalld" on "! firewall-cmd --list-rich-rules | grep -qE 'vrrp|port=\"443\"'"
check "firewalld is still running" firewalld_is_active

step "2. Both ufw and firewalld active: setup stops and explains"
fresh_box
ufw_on
firewalld_on
setup_config
check "setup --config stops" [ "$RC" != 0 ]
check "it says two firewalls are active and how to turn one off" \
    bash -c 'grep -q "Two host firewalls are active on this server: ufw and firewalld." <<< "$1" && grep -q "sudo systemctl disable --now firewalld" <<< "$1" && grep -q "HOST_FIREWALL=none" <<< "$1"' _ "$OUT"
check "nothing was set up" not_set_up
setup_ask "Go on without setting up the host firewall?" ""
check "the questions: the default answer (no) stops" [ "$RC" != 0 ]
check "it says nothing was changed" grep -q "Stopped. Nothing was changed. Turn one host firewall off" <<< "$OUT"
check "nothing was set up" not_set_up
check "no Furnace rules in ufw or firewalld" none_of ufw_has_furnace firewalld_has_furnace
setup_config "HOST_FIREWALL=none"
check "with HOST_FIREWALL=none setup goes on and finishes" [ "$RC" = 0 ]
check "the end of setup says to open the ports yourself" grep -q "Open port 443, SSH and VRRP (protocol 112) yourself" <<< "$OUT"
check "still no Furnace rules in ufw or firewalld" none_of ufw_has_furnace firewalld_has_furnace
check "both firewalls are still on" all_of ufw_is_active firewalld_is_active

step "3. Only ufw active (firewalld installed, off): Furnace uses ufw"
fresh_box
ufw_on
setup_config "HOST_FIREWALL=firewalld"
check "HOST_FIREWALL=firewalld is refused while ufw is the active one" \
    bash -c '[ "$1" != 0 ] && grep -q "the active host firewall on this server is ufw" <<< "$2"' _ "$RC" "$OUT"
check "nothing was set up" not_set_up
setup_config
check "setup finished" [ "$RC" = 0 ]
check "ufw has the Furnace rules" ufw_has_furnace
check "firewalld stayed off" nott firewalld_is_active
check "HOST_FIREWALL=ufw was saved" saved_fw ufw

step "4. No firewall active, settings file without HOST_FIREWALL: left alone, setup says so"
fresh_box
setup_config
check "setup finished" [ "$RC" = 0 ]
check "it says the firewall is off and how to let setup turn it on" grep -q "add HOST_FIREWALL=ufw to the settings file" <<< "$OUT"
check "ufw and firewalld are still off" none_of ufw_is_active firewalld_is_active
check "HOST_FIREWALL=none was saved" saved_fw none

step "5. No firewall active, the questions: answer no to turning it on"
fresh_box
setup_ask "Turn on and set up this server's own firewall (ufw)?" "no" full
check "setup finished" [ "$RC" = 0 ]
check "it asked before turning ufw on" grep -q "The host firewall ufw is installed but off" <<< "$OUT"
check "ufw and firewalld are still off" none_of ufw_is_active firewalld_is_active
check "HOST_FIREWALL=none was saved" saved_fw none

step "6. No firewall active, HOST_FIREWALL=ufw in the settings file: turned on and set up"
fresh_box
setup_config "HOST_FIREWALL=ufw"
check "setup finished" [ "$RC" = 0 ]
check "it says ufw was turned on" grep -q "ufw was off: turned it on" <<< "$OUT"
check "ufw is on with the Furnace rules" all_of ufw_is_active ufw_has_furnace
check "ufw has SSH open (no lock-out)" on "ufw status | grep -E '^22/tcp +ALLOW' | grep -q 'furnace ssh'"
check "firewalld stayed off" nott firewalld_is_active

step "6b. The user turns ufw off later: the package upgrade leaves it off and warns"
on "ufw --force disable >/dev/null"
OUT="$(on "furnace setup --upgrade 2>&1")" && RC=0 || RC=$?
check "the upgrade finished" [ "$RC" = 0 ]
check "it warns that ufw is off and leaves it off" grep -q "The host firewall ufw is off. Furnace leaves it off" <<< "$OUT"
check "ufw is still off" nott ufw_is_active

printf '\n%s passed, %s failed (%s)\n' "$PASSED" "$FAILED" "$SYSTEM"
[ "$FAILED" -eq 0 ]

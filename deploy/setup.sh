#!/usr/bin/env bash
# Furnace setup for the supported Linux systems (see SUPPORTED below). Run it as: sudo furnace setup
#
#   sudo furnace setup                 ask the questions, then configure and start everything
#   sudo furnace setup --config FILE   take the answers from a settings file (no questions)
#   sudo furnace setup --bundle FILE   the follower bundle from the leader (default: look in the usual places)
#   sudo furnace setup --check         show what setup would do, change nothing
#   sudo furnace setup --upgrade       use the saved answers again (the package upgrade runs this)
#
# Safe to run again: it never wipes the database and never changes the
# API tokens, the EDL logins or the certificate.
set -Eeuo pipefail
umask 022

LIB=/usr/lib/furnace
PY="$LIB/bin/furnace-python"   # Python 3.9+ with the bundled libraries, the same on every Linux
ETC=/etc/furnace
DATA=/var/lib/furnace
EDL=/var/lib/furnace-edl
AUTH=/var/lib/furnace-auth
SECRETS_FILE=/root/furnace-new-secrets.txt
INCOMING=/var/lib/furnace-incoming
SYNC_HOME=/var/lib/furnace-sync
STATE_DIR=/run/furnace-state
SETUP_CONF="$ETC/setup.conf"
LOG=/var/log/furnace-setup.log
BUNDLE_NAME=furnace-peer-bundle.tar.gz

MODE=ask
CONFIG=""
BUNDLE_ARG=""
CHECK=0
LOGGING=0
DONE=()
TODO=()
FAILED=0
NEW_SECRETS=()
UNPACK=""
BUNDLE=""
BUNDLE_ERR=""
HDR=""

# --------------------------------------------------------------------------- output

say()  { printf '\n==> %s  [%s]\n' "$*" "$(date +%T)"; }
ok()   { printf '    [ok] %s\n' "$*"; DONE+=("$*"); }
info() { printf '    %s\n' "$*"; }
warn() { printf '    [warning] %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
on_error() {
    printf '\nERROR: setup stopped at line %s. Nothing after this point was done.\n' "$1" >&2
    [ "$LOGGING" -eq 1 ] && printf 'See %s for details.\n' "$LOG" >&2
}
cleanup() {
    [ -n "$UNPACK" ] && rm -rf "$UNPACK"
    [ -n "$HDR" ] && rm -f "$HDR"
    rm -f "$STATE_DIR/selftest"
}
trap 'on_error $LINENO' ERR
trap cleanup EXIT

usage() {
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK=1 ;;
        --config) shift; [ $# -gt 0 ] || usage 2; CONFIG="$1"; MODE=config ;;
        --bundle) shift; [ $# -gt 0 ] || usage 2; BUNDLE_ARG="$1" ;;
        --upgrade) MODE=upgrade ;;
        -h|--help) usage 0 ;;
        *) echo "Unknown option: $1" >&2; usage 2 ;;
    esac
    shift
done

# --------------------------------------------------------------------------- basic checks

[ "$(id -u)" -eq 0 ] || die "Run setup as root: sudo furnace setup"

SUPPORTED="Ubuntu 22.04 and 24.04, Debian 12 and 13, RHEL / Rocky / AlmaLinux / Oracle Linux / CentOS Stream 8, 9
  and 10, Fedora 41 and newer, Amazon Linux 2023"
[ -r /etc/os-release ] || die "Cannot read /etc/os-release. Supported: $SUPPORTED."
# shellcheck source=/dev/null
. /etc/os-release
MAJOR="${VERSION_ID%%.*}"
FAMILY=""
case "${ID:-}" in
    ubuntu) case "${VERSION_ID:-}" in 22.04|24.04) FAMILY=debian ;; esac ;;
    debian) case "$MAJOR" in 12|13) FAMILY=debian ;; esac ;;
    rhel|rocky|almalinux|ol|centos) case "$MAJOR" in 8|9|10) FAMILY=redhat ;; esac ;;
    fedora) [[ "$MAJOR" =~ ^[0-9]+$ ]] && [ "$MAJOR" -ge 41 ] && FAMILY=redhat ;;
    amzn) [ "$MAJOR" = "2023" ] && FAMILY=redhat ;;
esac
[ -n "$FAMILY" ] || die "This is ${PRETTY_NAME:-an unknown system}. Supported: $SUPPORTED."

# Where the systems differ. Everything below uses these names.
if [ "$FAMILY" = "debian" ]; then
    NGINX_CONF=/etc/nginx/sites-available/furnace   # linked into sites-enabled
    NGINX_GROUP=www-data
    SSH_UNIT=ssh
else
    NGINX_CONF=/etc/nginx/conf.d/furnace.conf
    NGINX_GROUP=nginx
    SSH_UNIT=sshd
fi
# The host firewall. Furnace sets up the one that is ACTIVE, not the one that is only installed:
#   firewalld running -> firewalld; ufw active -> ufw; both active -> stop (every packet must pass both);
#   none active -> the installed one, turned on only after a yes; none installed -> leave it alone.
# FW_ACTIVE: ufw, firewalld, both or none. HOST_FW_TOOL: the tool setup offers (ufw, firewalld or none).
ufw_installed() { command -v ufw >/dev/null 2>&1; }
firewalld_installed() { command -v firewall-cmd >/dev/null 2>&1; }
# Active = running now OR switched on for the next boot. Starting firewalld wipes ufw's rules from the kernel,
# so "ufw status" says inactive although ufw is still on (ENABLED=yes) and loads again at the next boot.
ufw_active() {
    ufw_installed || return 1
    grep -q '^Status: active' <<< "$(ufw status 2>/dev/null || true)" && return 0
    grep -qE '^ENABLED=["]?yes' /etc/ufw/ufw.conf 2>/dev/null
}
firewalld_active() {
    firewalld_installed || return 1
    systemctl is-active --quiet firewalld || systemctl is-enabled --quiet firewalld 2>/dev/null
}
if ufw_active && firewalld_active; then FW_ACTIVE=both
elif firewalld_active; then FW_ACTIVE=firewalld
elif ufw_active; then FW_ACTIVE=ufw
else FW_ACTIVE=none
fi
case "$FW_ACTIVE" in
    ufw|firewalld) HOST_FW_TOOL="$FW_ACTIVE" ;;
    *)  # both or none active: the usual tool of this family first
        if [ "$FAMILY" = "debian" ] && ufw_installed; then HOST_FW_TOOL=ufw
        elif firewalld_installed; then HOST_FW_TOOL=firewalld
        elif ufw_installed; then HOST_FW_TOOL=ufw
        else HOST_FW_TOOL=none
        fi ;;
esac
FW_DEFAULTED=0   # config mode: 1 = HOST_FIREWALL is not in the settings file
FW_TURN_ON=0     # 1 = the chosen firewall is off now and setup turns it on (the user said yes)
FW_APPLY=""      # the firewall setup really configures (decide_host_firewall)

two_firewalls_text() {
    printf '%s\n' "Two host firewalls are active on this server: ufw and firewalld." \
        "  Every packet must pass both, so Furnace cannot open its ports in only one of them." \
        "  Turn one off, then run setup again:" \
        "    sudo ufw disable                          (keep firewalld)" \
        "    sudo systemctl disable --now firewalld    (keep ufw)"
}
FW_RULES="/etc/furnace/firewalld.rules"   # the firewalld rules Furnace added, so it can remove them again

if [ -d /opt/furnace/venv ]; then
    die "An old Furnace (installed with install.sh) is on this server.
  Remove it first. This keeps the database and the tokens:  sudo /opt/furnace/uninstall.sh
  Then run setup again."
fi

# --------------------------------------------------------------------------- settings

KEYS="ROLE LEADER_IP FOLLOWER_IP THIS_IP PEER_IP SHARED_IP SHARED_IP_PREFIX INTERFACE API_ALLOW_FROM LISTS_ALLOW_FROM ARCHIVE_AFTER_DAYS
KEEPALIVED_PASSWORD VRRP_ROUTER_ID USE_HTTPS MAX_IPS MAX_DOMAINS SSH_ALLOW_FROM HOST_FIREWALL REPLICATION_SSH_FALLBACK"
# USE_HTTPS is only read (old files): since 0.10.0 Furnace always uses HTTPS.
# The order the checks run in: later checks use the earlier values.
CHECK_ORDER="ROLE INTERFACE THIS_IP PEER_IP SHARED_IP SHARED_IP_PREFIX API_ALLOW_FROM LISTS_ALLOW_FROM ARCHIVE_AFTER_DAYS
KEEPALIVED_PASSWORD VRRP_ROUTER_ID MAX_IPS MAX_DOMAINS SSH_ALLOW_FROM HOST_FIREWALL REPLICATION_SSH_FALLBACK"
# Settings the follower takes from the leader. They must be the same on both servers.
SHARED_KEYS="SHARED_IP SHARED_IP_PREFIX API_ALLOW_FROM LISTS_ALLOW_FROM ARCHIVE_AFTER_DAYS KEEPALIVED_PASSWORD
VRRP_ROUTER_ID MAX_IPS MAX_DOMAINS REPLICATION_SSH_FALLBACK"
# The first API token. Used only while the leader has no token yet, so they are not saved in setup.conf.
TOKEN_KEYS="TOKEN_NAME TOKEN_ALLOW TOKEN_EXPIRES_DAYS"
# Used once, never saved: the follower's join code (made on the leader with: sudo furnace join-code).
ONCE_KEYS="JOIN_CODE"

for k in $KEYS $TOKEN_KEYS $ONCE_KEYS; do printf -v "CFG_$k" '%s' ""; done

trim() {
    local s="${1%$'\r'}"
    s="${s#"${s%%[![:space:]]*}"}"
    printf '%s' "${s%"${s##*[![:space:]]}"}"
}

# load_file FILE PREFIX: read KEY=VALUE lines into PREFIX_KEY variables.
load_file() {
    local file="$1" prefix="$2" line key value
    [ -f "$file" ] || die "The settings file $file does not exist."
    while IFS= read -r line || [ -n "$line" ]; do
        line="$(trim "$line")"
        [ -z "$line" ] && continue
        [ "${line:0:1}" = "#" ] && continue
        [[ "$line" == *=* ]] || die "Bad line in $file: '$line'. Use KEY=VALUE."
        key="$(trim "${line%%=*}")"
        value="$(trim "${line#*=}")"
        if [[ "$value" =~ ^\"(.*)\"$ ]] || [[ "$value" =~ ^\'(.*)\'$ ]]; then
            value="${BASH_REMATCH[1]}"
        fi
        case "$key" in   # the names before 0.3.1
            XSOAR_IP) key=API_ALLOW_FROM ;;
            FIREWALL_IP) key=LISTS_ALLOW_FROM ;;
        esac
        case " $(echo $KEYS $TOKEN_KEYS $ONCE_KEYS) " in
            *" $key "*) printf -v "${prefix}_$key" '%s' "$value" ;;
            *) die "Unknown setting '$key' in $file." ;;
        esac
    done < "$file"
}

# map_ips PREFIX LOCAL: turn LEADER_IP and FOLLOWER_IP (the same on both servers) into this server's
# THIS_IP and PEER_IP, which setup uses inside. LOCAL=1 (this server's own settings): the role is the
# one whose IP is on this server, so ROLE is only needed when one server has both. The old names
# THIS_IP and PEER_IP (before 0.10.0) still work; OLD_IP_NAMES=1 then, for a note at the end.
OLD_IP_NAMES=0
map_ips() {
    local p="$1" here="$2" l f r t pe mine=""
    l="${p}_LEADER_IP"; f="${p}_FOLLOWER_IP"; r="${p}_ROLE"; t="${p}_THIS_IP"; pe="${p}_PEER_IP"
    if [ -z "${!l}${!f}" ]; then
        [ "$p" = CFG ] && [ -n "${!t}${!pe}" ] && OLD_IP_NAMES=1
        return 0
    fi
    [ -z "${!t}${!pe}" ] || die "Use LEADER_IP and FOLLOWER_IP, or the old THIS_IP and PEER_IP, not both."
    [ -n "${!l}" ] && [ -n "${!f}" ] || die "Set both LEADER_IP and FOLLOWER_IP (the same on both servers)."
    [ "${!l}" != "${!f}" ] || die "LEADER_IP and FOLLOWER_IP must be the two different servers."
    if [ "$here" = 1 ]; then
        [ -n "$(card_of_ip "${!l}")" ] && mine="leader"
        [ -n "$(card_of_ip "${!f}")" ] && mine="${mine:+$mine }follower"
        if [ -z "${!r}" ]; then
            case "$mine" in
                leader|follower) printf -v "$r" '%s' "$mine" ;;
                "leader follower") die "This server has both LEADER_IP and FOLLOWER_IP. Add ROLE=leader or ROLE=follower." ;;
                *) die "This server has neither ${!l} (LEADER_IP) nor ${!f} (FOLLOWER_IP). Check the IPs. See: ip -br addr" ;;
            esac
        elif [ -z "$mine" ]; then
            msg="$(check_ROLE "${!r}")" || die "$msg"
            if [ "${!r}" = leader ]; then
                die "ROLE is leader, but LEADER_IP ${!l} is not on this server. See: ip -br addr"
            fi
            die "ROLE is follower, but FOLLOWER_IP ${!f} is not on this server. See: ip -br addr"
        elif [[ " $mine " != *" ${!r} "* ]]; then
            die "ROLE is ${!r}, but this server has the $mine's IP."
        fi
    fi
    case "${!r}" in
        leader) printf -v "$t" '%s' "${!l}"; printf -v "$pe" '%s' "${!f}" ;;
        follower) printf -v "$t" '%s' "${!f}"; printf -v "$pe" '%s' "${!l}" ;;
    esac
    printf -v "$l" '%s' ""; printf -v "$f" '%s' ""   # from here on only THIS_IP and PEER_IP are used
}

is_ipv4() {
    "$PY" - "$1" <<'PY' 2>/dev/null
import ipaddress, sys
ip = ipaddress.ip_address(sys.argv[1])
sys.exit(0 if ip.version == 4 and str(ip) == sys.argv[1] else 1)
PY
}

is_ip_or_cidr() {
    "$PY" - "$1" <<'PY' 2>/dev/null
import ipaddress, sys
ipaddress.ip_network(sys.argv[1], strict=True)
PY
}

selinux_on() { command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled; }

# ssh_peer_text TEXT: TEXT when the SSH backup copy is on (it needs SSH between the servers), else nothing.
ssh_peer_text() { if [ "$CFG_REPLICATION_SSH_FALLBACK" = "yes" ]; then printf '%s' "$1"; fi; }

web_sources_text() {
    if [ "$CFG_LISTS_ALLOW_FROM" = "any" ] || [ "$CFG_API_ALLOW_FROM" = "any" ]; then
        echo "any address"
    else
        echo "$CFG_LISTS_ALLOW_FROM, $CFG_API_ALLOW_FROM and $CFG_PEER_IP"
    fi
}

is_int_between() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge "$2" ] && [ "$1" -le "$3" ]; }

split_list() { echo "$1" | tr ',' ' '; }

# Network helpers, used for the default answers.
default_card() { ip -o -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit }}'; }
card_of_ip()   { ip -o -4 addr show 2>/dev/null | awk -v ip="$1" '{split($4, a, "/"); if (a[1] == ip) { print $2; exit }}'; }
primary_ip()   { ip -o -4 addr show dev "$1" primary 2>/dev/null | awk '{split($4, a, "/"); print a[1]; exit}'; }
prefix_of()    { ip -o -4 addr show dev "$1" 2>/dev/null | awk -v ip="$2" '{split($4, a, "/"); if (a[1] == ip) { print a[2]; exit }}'; }

# One check per setting. Each prints the problem and returns 1, or returns 0.
check_ROLE() { [[ "$1" =~ ^(leader|follower)$ ]] || { echo "ROLE must be 'leader' or 'follower', not '$1'."; return 1; }; }
check_INTERFACE() {
    local hint="Type the card name from the first column of 'ip -br addr' (for example eth0 or ens192), not an IP."
    [[ "$1" =~ ^[0-9.]+(/[0-9]+)?$ ]] && { echo "'$1' is an IP address, not a network card name. $hint"; return 1; }
    [[ "$1" =~ ^[A-Za-z0-9_.:-]{1,15}$ ]] || { echo "INTERFACE '$1' is not a network card name. $hint"; return 1; }
    ip link show "$1" >/dev/null 2>&1 || { echo "The network card '$1' does not exist on this server. $hint"; return 1; }
}
check_THIS_IP() {
    is_ipv4 "$1" || { echo "THIS_IP '$1' is not a valid IPv4 address."; return 1; }
    ip -o -4 addr show dev "$CFG_INTERFACE" | grep -qw "inet ${1}/[0-9]*" \
        || { echo "THIS_IP $1 is not set on the network card $CFG_INTERFACE. See: ip -br addr"; return 1; }
}
check_PEER_IP() {
    is_ipv4 "$1" || { echo "PEER_IP '$1' is not a valid IPv4 address."; return 1; }
    [ "$1" != "$CFG_THIS_IP" ] || { echo "PEER_IP must be the other server, not this server ($CFG_THIS_IP)."; return 1; }
}
check_SHARED_IP() {
    is_ipv4 "$1" || { echo "SHARED_IP '$1' is not a valid IPv4 address."; return 1; }
    [ "$1" != "$CFG_THIS_IP" ] && [ "$1" != "$CFG_PEER_IP" ] \
        || { echo "SHARED_IP must be a third address, not the IP of one of the servers."; return 1; }
}
check_SHARED_IP_PREFIX() { is_int_between "$1" 8 32 || { echo "SHARED_IP_PREFIX must be a number from 8 to 32."; return 1; }; }
check_ip_list() {  # NAME VALUE: 'any', or IPs and ranges separated by commas
    local item
    [ "$2" = "any" ] && return 0
    [ -n "$(split_list "$2" | tr -d ' ')" ] || { echo "$1 needs 'any' or at least one IP address."; return 1; }
    for item in $(split_list "$2"); do
        is_ip_or_cidr "$item" || { echo "$1 contains '$item'. Use 'any' or IP addresses."; return 1; }
    done
}
check_API_ALLOW_FROM() { check_ip_list API_ALLOW_FROM "$1"; }
check_LISTS_ALLOW_FROM() { check_ip_list LISTS_ALLOW_FROM "$1"; }
check_ARCHIVE_AFTER_DAYS() { is_int_between "$1" 1 3650 || { echo "ARCHIVE_AFTER_DAYS must be a number from 1 to 3650."; return 1; }; }
check_KEEPALIVED_PASSWORD() {
    [[ "$1" =~ ^[A-Za-z0-9._@%+=:!-]{6,8}$ ]] \
        || { echo "KEEPALIVED_PASSWORD must be 6 to 8 characters: letters, numbers or . _ @ % + = : ! -"; return 1; }
    [ "$1" != "Chg3Me!x" ] || { echo "KEEPALIVED_PASSWORD is still the example value. Choose your own or leave it empty."; return 1; }
}
check_VRRP_ROUTER_ID() { is_int_between "$1" 1 255 || { echo "VRRP_ROUTER_ID must be a number from 1 to 255."; return 1; }; }
check_REPLICATION_SSH_FALLBACK() {
    [[ "$1" =~ ^(yes|no)$ ]] || { echo "REPLICATION_SSH_FALLBACK must be 'yes' or 'no'."; return 1; }
}
check_MAX_IPS() { is_int_between "$1" 0 10000000 || { echo "MAX_IPS must be a number (0 means no limit)."; return 1; }; }
check_MAX_DOMAINS() { is_int_between "$1" 0 10000000 || { echo "MAX_DOMAINS must be a number (0 means no limit)."; return 1; }; }
check_SSH_ALLOW_FROM() {
    [ "$1" = "any" ] && return 0
    local item
    for item in $(split_list "$1"); do
        is_ip_or_cidr "$item" || { echo "SSH_ALLOW_FROM contains '$item'. Use 'any' or IP addresses."; return 1; }
    done
}
check_HOST_FIREWALL() {
    [[ "$1" =~ ^(ufw|firewalld|none)$ ]] || { echo "HOST_FIREWALL must be 'ufw', 'firewalld' or 'none'."; return 1; }
    [ "$1" = "none" ] && return 0
    "${1}_installed" && return 0
    if [ "$HOST_FW_TOOL" = "none" ]; then
        echo "HOST_FIREWALL is '$1', but no host firewall (ufw or firewalld) is installed here. Use 'none'."
    else
        echo "HOST_FIREWALL is '$1', but $1 is not installed here. Use '$HOST_FW_TOOL' or 'none'."
    fi
    return 1
}

# Decide what happens to the host firewall, after the settings are checked. Sets FW_APPLY and FW_TURN_ON.
# A package upgrade (--upgrade) never stops and never turns a firewall on: it leaves the firewall alone and says why.
fw_skip() { FW_APPLY=none; warn "$1"; FW_NONE_NOTE="$1"; }
decide_host_firewall() {
    local fw="$CFG_HOST_FIREWALL"
    FW_APPLY="$fw"
    FW_NONE_NOTE="Host firewall was not configured (HOST_FIREWALL=none). Open port $WEB_PORT, SSH and VRRP (protocol 112) yourself."
    if [ "$fw" = "none" ] && [ "$FW_ACTIVE" = "both" ]; then
        FW_NONE_NOTE="Two host firewalls are on (ufw and firewalld) and Furnace changed neither. Every packet must pass both: open port $WEB_PORT, SSH and VRRP (protocol 112) in both, or turn one off. Starting firewalld can leave ufw dropping everything, even SSH."
        warn "$FW_NONE_NOTE"
    fi
    [ "$fw" = "none" ] && return 0
    if [ "$FW_ACTIVE" = "both" ]; then
        [ "$MODE" = "upgrade" ] && { fw_skip "Two host firewalls are active (ufw and firewalld). Furnace changed neither. Turn one off, then run: sudo furnace setup"; return 0; }
        die "$(two_firewalls_text)
  Or set HOST_FIREWALL=none in the settings file and open the ports yourself."
    fi
    if [ "$FW_ACTIVE" != "none" ] && [ "$FW_ACTIVE" != "$fw" ]; then
        [ "$MODE" = "upgrade" ] && { fw_skip "The active host firewall is $FW_ACTIVE now, but Furnace was set up with $fw. Furnace did not change the host firewall. Choose again with: sudo furnace setup"; return 0; }
        die "HOST_FIREWALL is '$fw', but the active host firewall on this server is $FW_ACTIVE. Use '$FW_ACTIVE' or 'none'."
    fi
    if [ "$FW_ACTIVE" = "none" ]; then
        if [ "$MODE" = "upgrade" ]; then
            fw_skip "The host firewall $fw is off. Furnace leaves it off and did not change it. To let Furnace turn it on: sudo furnace setup"
        elif [ "$FW_DEFAULTED" -eq 1 ]; then
            CFG_HOST_FIREWALL=none
            FW_APPLY=none
            FW_NONE_NOTE="The host firewall $fw is off, so Furnace left it alone. To let setup turn it on and set it up, add HOST_FIREWALL=$fw to the settings file. Or open port $WEB_PORT, SSH and VRRP (protocol 112) yourself."
        else
            FW_TURN_ON=1
        fi
    fi
}

check_TOKEN_NAME() {
    [[ "${1,,}" =~ ^[a-z0-9][a-z0-9._-]{0,39}$ ]] \
        || { echo "The token name has 1 to 40 characters: letters, numbers, dot, dash or underscore."; return 1; }
}
check_TOKEN_ALLOW() {
    local p
    [ -n "$(split_list "$1" | tr -d ' ')" ] || { echo "TOKEN_ALLOW needs at least one permission."; return 1; }
    for p in $(split_list "$1"); do
        case "${p,,}" in
            all|blocklist:read|blocklist:write|whitelist:read|whitelist:write) ;;
            *) echo "TOKEN_ALLOW: unknown permission '$p'. Use blocklist:read, blocklist:write, whitelist:read, whitelist:write or all."
               return 1 ;;
        esac
    done
}
check_TOKEN_EXPIRES_DAYS() {
    is_int_between "$1" 0 3650 || { echo "The token expiry must be a number of days from 0 to 3650 (0 = never)."; return 1; }
}

validate_all() {
    local k var msg
    for k in $CHECK_ORDER $TOKEN_KEYS; do
        var="CFG_$k"
        [ -n "${!var}" ] || die "The setting $k is empty."
        msg="$("check_$k" "${!var}")" || die "$msg"
    done
}

new_password() {
    "$PY" -c 'import secrets, string; a = string.ascii_letters + string.digits; print("".join(secrets.choice(a) for _ in range(8)))'
}

# Fill in what is still empty. Cards and IPs are found on this server.
fill_defaults() {
    : "${CFG_SHARED_IP_PREFIX:=24}" "${CFG_VRRP_ROUTER_ID:=51}" "${CFG_REPLICATION_SSH_FALLBACK:=$SSH_FALLBACK_DEFAULT}"
    : "${CFG_MAX_IPS:=50000}" "${CFG_MAX_DOMAINS:=50000}" "${CFG_SSH_ALLOW_FROM:=any}"
    : "${CFG_HOST_FIREWALL:=$HOST_FW_TOOL}" "${CFG_ARCHIVE_AFTER_DAYS:=90}" "${CFG_API_ALLOW_FROM:=any}" "${CFG_LISTS_ALLOW_FROM:=any}"
    : "${CFG_TOKEN_NAME:=api}" "${CFG_TOKEN_ALLOW:=all}" "${CFG_TOKEN_EXPIRES_DAYS:=0}"
    if [ -z "$CFG_INTERFACE" ]; then
        if [ -n "$CFG_THIS_IP" ]; then CFG_INTERFACE="$(card_of_ip "$CFG_THIS_IP")"; else CFG_INTERFACE="$(default_card)"; fi
    fi
    if [ -z "$CFG_THIS_IP" ] && [ -n "$CFG_INTERFACE" ]; then CFG_THIS_IP="$(primary_ip "$CFG_INTERFACE")"; fi
    if [ -z "$CFG_KEEPALIVED_PASSWORD" ] && [ "$CFG_ROLE" = "leader" ]; then CFG_KEEPALIVED_PASSWORD="$(new_password)"; fi
    return 0
}

# API tokens and EDL logins are in the database (replicated), so they are not part of these.
have_secrets() { [ -f "$ETC/tls/furnace.crt" ] && [ -f "$ETC/ssh/replica_key" ]; }

token_count() { sqlite3 -readonly "$DATA/furnace.db" "SELECT COUNT(*) FROM api_tokens" 2>/dev/null || echo 0; }

# Setup makes the first API token only on a leader that has none yet.
needs_first_token() { [ "$CFG_ROLE" = "leader" ] && [ "$(token_count)" -eq 0 ]; }

# --------------------------------------------------------------------------- follower bundle

find_bundle() {
    local place home=""
    [ -n "${SUDO_USER:-}" ] && home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    for place in "$BUNDLE_ARG" "$PWD/$BUNDLE_NAME" "${home:+$home/$BUNDLE_NAME}" "/root/$BUNDLE_NAME" "/tmp/$BUNDLE_NAME"; do
        [ -n "$place" ] && [ -f "$place" ] && { echo "$place"; return 0; }
    done
    return 1
}

# open_bundle FILE: unpack it, check it, and read the leader's settings into B_*.
# Returns 1 with the problem in BUNDLE_ERR when the file is not a usable bundle.
open_bundle() {
    local f
    [ -n "$UNPACK" ] && rm -rf "$UNPACK"
    UNPACK="$(mktemp -d)"
    if ! (umask 077; tar -xzf "$1" -C "$UNPACK" 2>/dev/null); then
        BUNDLE_ERR="$1 is not a Furnace bundle (it cannot be unpacked)."
        return 1
    fi
    for f in tls/furnace.crt tls/furnace.key ssh/replica_key ssh/replica_key.pub setup.conf; do
        if [ ! -f "$UNPACK/$f" ]; then
            BUNDLE_ERR="The bundle $1 is incomplete ($f is missing). Make it again with a current Furnace on the leader."
            return 1
        fi
    done
    for k in $KEYS; do printf -v "B_$k" '%s' ""; done
    load_file "$UNPACK/setup.conf" B
    [ "$B_ROLE" = "leader" ] || { BUNDLE_ERR="The bundle $1 was not made by a leader."; return 1; }
    map_ips B 0
    BUNDLE="$1"
}

# fetch_join CODE [LEADER_IP]: fetch the leader's bundle over HTTPS with a join code and open it.
# Returns 1 with the problem in BUNDLE_ERR. The code works only once, so check mode does not use it.
JOINED=0
fetch_join() {
    local file
    if [ "$CHECK" -eq 1 ]; then
        BUNDLE_ERR="Check mode does not use the join code (it works only once). Run setup without --check."
        return 1
    fi
    file="$(mktemp)"
    if ! BUNDLE_ERR="$("$PY" -m furnace.join fetch --code "$1" --out "$file" ${2:+--leader "$2"} 2>&1)"; then
        rm -f "$file"
        return 1
    fi
    if ! open_bundle "$file"; then
        rm -f "$file"
        return 1
    fi
    JOINED=1
}

# Take the shared settings from the leader. STRICT=1: a different value in the
# settings file is an error. The follower's IP is the leader's PEER_IP.
apply_bundle() {
    local strict="$1" k b c
    for k in $SHARED_KEYS; do
        b="B_$k"; c="CFG_$k"
        [ -n "${!b}" ] || continue   # a setting the leader's (older) Furnace does not have yet
        if [ "$strict" -eq 1 ] && [ -n "${!c}" ] && [ "${!c}" != "${!b}" ]; then
            die "$k is '${!c}' in your settings file, but '${!b}' on the leader. Both servers need the same value. Remove it from the file or use the leader's value."
        fi
        printf -v "$c" '%s' "${!b}"
    done
    if [ -n "$CFG_THIS_IP" ] && [ "$CFG_THIS_IP" != "$B_PEER_IP" ]; then
        die "This server is $CFG_THIS_IP, but the leader expects the follower at $B_PEER_IP (PEER_IP on the leader)."
    fi
    if [ -n "$CFG_PEER_IP" ] && [ "$CFG_PEER_IP" != "$B_THIS_IP" ]; then
        die "PEER_IP is $CFG_PEER_IP, but the leader is $B_THIS_IP."
    fi
    CFG_THIS_IP="$B_PEER_IP"
    CFG_PEER_IP="$B_THIS_IP"
    : "${CFG_SSH_ALLOW_FROM:=$B_SSH_ALLOW_FROM}"
    # The leader may run another Linux with another firewall tool: use the one on this server.
    if [ -z "$CFG_HOST_FIREWALL" ]; then
        if [ "$B_HOST_FIREWALL" = "none" ]; then CFG_HOST_FIREWALL=none; else CFG_HOST_FIREWALL="$HOST_FW_TOOL"; fi
    fi
    if [ -z "$CFG_INTERFACE" ]; then
        CFG_INTERFACE="$(card_of_ip "$CFG_THIS_IP")"
        [ -n "$CFG_INTERFACE" ] || die "The leader expects this server at $CFG_THIS_IP, but no network card here has that IP.
  Check PEER_IP on the leader (sudo cat $SETUP_CONF there), or set the IP on this server. See: ip -br addr"
    fi
}

# --------------------------------------------------------------------------- questions

# ask KEY QUESTION DEFAULT: ask until the answer passes check_KEY.
ask() {
    local key="$1" question="$2" default="$3" answer msg
    while true; do
        if [ -n "$default" ]; then
            read -r -p "  $question [$default]: " answer < /dev/tty || die "No answer (the input was closed)."
        else
            read -r -p "  $question: " answer < /dev/tty || die "No answer (the input was closed)."
        fi
        answer="$(trim "$answer")"
        answer="${answer:-$default}"
        if [ -z "$answer" ]; then
            echo "    Please type an answer."
            continue
        fi
        if msg="$("check_$key" "$answer")"; then
            printf -v "CFG_$key" '%s' "$answer"
            return 0
        fi
        printf '    %s\n' "$msg"
    done
}

# ask_yes_no QUESTION DEFAULT(yes|no): returns 0 for yes.
ask_yes_no() {
    local answer
    while true; do
        read -r -p "  $1 [$2]: " answer < /dev/tty || die "No answer (the input was closed)."
        answer="$(trim "$answer")"
        answer="${answer:-$2}"
        case "${answer,,}" in
            y|yes) return 0 ;;
            n|no) return 1 ;;
            *) echo "    Please answer yes or no." ;;
        esac
    done
}

note() { printf '\n  %s\n' "$*"; }

ask_follower_from_bundle() {
    local place msg code
    if ! { place="$(find_bundle)" && open_bundle "$place"; }; then
        [ -n "$BUNDLE_ERR" ] && printf '    %s\n' "$BUNDLE_ERR"
        note "The follower needs the leader's certificate and secrets. Easiest: run this on the leader:"
        info "    sudo furnace join-code"
        info "  and type the code here. Or press Enter to give the file $BUNDLE_NAME instead."
        while true; do
            read -r -p "  Join code from the leader (Enter = use the file): " code < /dev/tty || die "No answer (the input was closed)."
            code="$(trim "$code")"
            [ -z "$code" ] && break
            if fetch_join "$code"; then break; fi
            printf '    %s\n' "$BUNDLE_ERR"
        done
        if [ "$JOINED" -ne 1 ]; then
            note "On the leader the file is /root/$BUNDLE_NAME. Copy it to this server first."
            while true; do
                read -r -p "  Where is $BUNDLE_NAME? (full path): " place < /dev/tty || die "No answer (the input was closed)."
                place="$(trim "$place")"
                if [ ! -f "$place" ]; then echo "    There is no file $place."; continue; fi
                if open_bundle "$place"; then break; fi
                printf '    %s\n' "$BUNDLE_ERR"
            done
        fi
    fi
    if [ "$JOINED" -eq 1 ]; then
        note "Got the leader's certificate and secrets with the join code."
    else
        note "Found the leader's bundle: $BUNDLE"
    fi
    info "  The leader is $B_THIS_IP. This server must be $B_PEER_IP. The other settings come from the leader."
    apply_bundle 0
    note "Network card for $CFG_THIS_IP and the shared IP ($CFG_SHARED_IP): its name, not its IP."
    ask INTERFACE "Network card name" "$CFG_INTERFACE"
    msg="$(check_THIS_IP "$CFG_THIS_IP")" || die "$msg"
    ask_host_firewall follower
}

ask_all() {
    local card ip
    note "Leader = the first server. Follower = the second (standby) server. Set up the leader first."
    ask ROLE "Is this server the leader or the follower?" "${CFG_ROLE:-leader}"
    if [ "$CFG_ROLE" = "follower" ] && ! have_secrets; then
        ask_follower_from_bundle
        return
    fi

    card="${CFG_INTERFACE:-$(default_card)}"
    note "The network card that carries this server's IP and the shared IP: its name, not its IP. See: ip -br addr"
    ask INTERFACE "Network card name" "$card"
    ip="${CFG_THIS_IP:-$(primary_ip "$CFG_INTERFACE")}"
    ask THIS_IP "This server's IP (the leader)" "$ip"
    ask PEER_IP "The follower server's IP" "$CFG_PEER_IP"
    note "The shared IP (VIP) is a free third address. Everything that uses Furnace connects to it."
    ask SHARED_IP "Shared IP" "$CFG_SHARED_IP"
    ask SHARED_IP_PREFIX "Network prefix of the shared IP (24 = 255.255.255.0)" \
        "${CFG_SHARED_IP_PREFIX:-$(prefix_of "$CFG_INTERFACE" "$CFG_THIS_IP")}"
    note "Who may connect. 'any' = every address: the API still needs a token and the lists need a login."
    info "  Or limit it to IPs and ranges, separated by commas."
    ask API_ALLOW_FROM "Who may call the API" "${CFG_API_ALLOW_FROM:-any}"
    ask LISTS_ALLOW_FROM "Who may download the lists" "${CFG_LISTS_ALLOW_FROM:-any}"
    note "The firewall's EDL limit. Adds beyond it are refused. 0 = no limit. Used for every list (IPs, domains, URLs)."
    ask MAX_IPS "Most entries per list" "${CFG_MAX_IPS:-50000}"
    CFG_MAX_DOMAINS="$CFG_MAX_IPS"
    ask ARCHIVE_AFTER_DAYS "Days an IP stays blocked (permanent IPs never expire)" "${CFG_ARCHIVE_AFTER_DAYS:-90}"
    note "VRRP router ID: the same on both servers, unique in this network. Keep 51 if you are not sure."
    ask VRRP_ROUTER_ID "VRRP router ID (1-255)" "${CFG_VRRP_ROUTER_ID:-51}"
    note "Who may log in with SSH (people, for example admins): 'any', or IPs separated by commas."
    ask SSH_ALLOW_FROM "SSH allowed from" "${CFG_SSH_ALLOW_FROM:-any}"
    note "The leader copies the data to the follower over HTTPS. SSH can be a backup for when HTTPS does not work."
    info "  Most servers do not need it. It adds a user (furnace-sync) and SSH between the two servers."
    if ask_yes_no "Use SSH as a backup for the copy to the other server?" "${CFG_REPLICATION_SSH_FALLBACK:-$SSH_FALLBACK_DEFAULT}"; then
        CFG_REPLICATION_SSH_FALLBACK=yes
    else
        CFG_REPLICATION_SSH_FALLBACK=no
    fi
    ask_host_firewall leader
}

# The host firewall question. leader: always asked. follower: the leader's answer counts (no = none);
# only turning on a firewall that is off is asked. Two active firewalls: stop, or go on without one.
ask_host_firewall() {
    local fw_default=yes
    if [ "$HOST_FW_TOOL" = "none" ]; then
        note "No host firewall tool (ufw or firewalld) is installed. Furnace leaves the host firewall alone."
        CFG_HOST_FIREWALL=none
        return
    fi
    if [ "$1" = "follower" ] && [ "$CFG_HOST_FIREWALL" = "none" ]; then
        return   # the leader was set up without the host firewall
    fi
    if [ "$FW_ACTIVE" = "both" ]; then
        note "$(two_firewalls_text)"
        if ask_yes_no "Go on without setting up the host firewall? (You open the ports yourself.)" no; then
            CFG_HOST_FIREWALL=none
            return
        fi
        printf '\nStopped. Nothing was changed. Turn one host firewall off, then run: sudo furnace setup\n'
        exit 1
    fi
    [ "${CFG_HOST_FIREWALL:-$HOST_FW_TOOL}" = "none" ] && fw_default=no
    if [ "$FW_ACTIVE" = "$HOST_FW_TOOL" ]; then
        [ "$1" = "follower" ] && { CFG_HOST_FIREWALL="$HOST_FW_TOOL"; return; }
        note "Furnace opens only what it needs: the web port (API, lists, the other server), SSH, and VRRP from the other server."
        if ask_yes_no "Let Furnace set up this server's own firewall ($HOST_FW_TOOL)?" "$fw_default"; then
            CFG_HOST_FIREWALL="$HOST_FW_TOOL"
        else
            CFG_HOST_FIREWALL=none
        fi
        return
    fi
    note "The host firewall $HOST_FW_TOOL is installed but off. Furnace allows SSH before it turns it on."
    if ask_yes_no "Turn on and set up this server's own firewall ($HOST_FW_TOOL)?" "$fw_default"; then
        CFG_HOST_FIREWALL="$HOST_FW_TOOL"
    else
        CFG_HOST_FIREWALL=none
    fi
}

# The permission grid, the same as in "furnace token add". A number switches a box.
# Write ticks read; switching read off also switches write off. Sets CFG_TOKEN_ALLOW.
ask_token_grid() {
    local names=(blocklist:read blocklist:write whitelist:read whitelist:write) on=(1 1 1 1) answer n i
    mark() { if [ "$1" = 1 ]; then printf 'x'; else printf ' '; fi; }
    while true; do
        printf '\n  What may this token do? Type a box number to switch it (or several: 2 4).\n\n'
        printf '                   Read       Write\n'
        printf '    Blocklist     1 [%s]    2 [%s]\n' "$(mark "${on[0]}")" "$(mark "${on[1]}")"
        printf '    Whitelist     3 [%s]    4 [%s]\n' "$(mark "${on[2]}")" "$(mark "${on[3]}")"
        read -r -p $'\n  Box number, or Enter when done: ' answer < /dev/tty || die "No answer (the input was closed)."
        answer="$(trim "${answer//,/ }")"
        if [ -z "$answer" ]; then
            CFG_TOKEN_ALLOW=""
            for i in 0 1 2 3; do
                if [ "${on[i]}" = 1 ]; then CFG_TOKEN_ALLOW="${CFG_TOKEN_ALLOW:+$CFG_TOKEN_ALLOW,}${names[i]}"; fi
            done
            [ -n "$CFG_TOKEN_ALLOW" ] && return 0
            echo "    Tick at least one box."
            continue
        fi
        if [[ ! "$answer" =~ ^[1-4]([[:space:]]+[1-4])*$ ]]; then
            echo "    Type 1, 2, 3 or 4, or press Enter when done."
            continue
        fi
        for n in $answer; do
            i=$((n - 1))
            if [ "${on[i]}" = 1 ]; then
                on[i]=0
                if [ $((i % 2)) -eq 0 ]; then on[i + 1]=0; fi   # no read, no write
            else
                on[i]=1
                if [ $((i % 2)) -eq 1 ]; then on[i - 1]=1; fi   # write ticks read
            fi
        done
    done
}

ask_first_token() {
    note "The first API token, for the system that calls the API (for example a SOAR or a script)."
    info "  More tokens later: sudo furnace token add NAME"
    ask TOKEN_NAME "Token name" "${CFG_TOKEN_NAME:-api}"
    ask_token_grid
    note "Each token has its own expiry. Furnace warns 14 days before. 0 = never."
    ask TOKEN_EXPIRES_DAYS "Token expires after how many days" "$CFG_TOKEN_EXPIRES_DAYS"
}

show_settings() {
    printf '\n  Settings:\n'
    printf '    %-22s %s\n' "Role" "$CFG_ROLE" "Network card" "$CFG_INTERFACE" "This server" "$CFG_THIS_IP" \
        "Other server" "$CFG_PEER_IP" "Shared IP" "$CFG_SHARED_IP/$CFG_SHARED_IP_PREFIX" "API from" "$CFG_API_ALLOW_FROM" \
        "Lists from" "$CFG_LISTS_ALLOW_FROM" "Most entries per list" "$CFG_MAX_IPS" \
        "IPs expire after" "$CFG_ARCHIVE_AFTER_DAYS days" "VRRP router ID" "$CFG_VRRP_ROUTER_ID" \
        "SSH allowed from" "$CFG_SSH_ALLOW_FROM" "SSH backup copy" "$CFG_REPLICATION_SSH_FALLBACK" \
        "Host firewall" "$CFG_HOST_FIREWALL"
    if needs_first_token; then
        local expiry="never"
        [ "$CFG_TOKEN_EXPIRES_DAYS" = 0 ] || expiry="after $CFG_TOKEN_EXPIRES_DAYS days"
        printf '    %-22s %s\n' "First API token" "$CFG_TOKEN_NAME (${CFG_TOKEN_ALLOW//,/, }; expires $expiry)"
    fi
}

write_setup_conf() {
    local k var
    (
        umask 077
        {
            echo "# Furnace settings, saved by 'furnace setup'. Secret (keepalived password)."
            echo "# Change them with: sudo furnace setup"
            echo "# LEADER_IP and FOLLOWER_IP are the same on both servers; ROLE says which one this is."
            for k in $KEYS; do
                var="CFG_$k"
                case "$k" in
                    THIS_IP|PEER_IP|USE_HTTPS) continue ;;
                    LEADER_IP) if [ "$CFG_ROLE" = leader ]; then var=CFG_THIS_IP; else var=CFG_PEER_IP; fi ;;
                    FOLLOWER_IP) if [ "$CFG_ROLE" = leader ]; then var=CFG_PEER_IP; else var=CFG_THIS_IP; fi ;;
                esac
                printf '%s=%s\n' "$k" "${!var}"
            done
        } > "$SETUP_CONF.tmp"
    )
    mv -f "$SETUP_CONF.tmp" "$SETUP_CONF"
}

# --------------------------------------------------------------------------- collect the settings

# The SSH backup copy is off unless asked for. A server set up before 0.8 has no replication secret yet: the
# other server may still run that old version, which can only copy over SSH. Then SSH stays on for now.
SSH_FALLBACK_DEFAULT=no
OLD_SSH_ONLY=0
if [ -f "$SETUP_CONF" ] && [ ! -f "$ETC/secrets/replica-secret" ]; then
    SSH_FALLBACK_DEFAULT=yes
    OLD_SSH_ONLY=1
fi

case "$MODE" in
    upgrade)
        [ -f "$SETUP_CONF" ] || die "Furnace is not set up yet. Run: sudo furnace setup"
        load_file "$SETUP_CONF" CFG
        map_ips CFG 1
        OLD_IP_NAMES=0
        ;;
    config)
        load_file "$CONFIG" CFG
        map_ips CFG 1
        [ -n "$CFG_HOST_FIREWALL" ] || FW_DEFAULTED=1
        msg="$(check_ROLE "$CFG_ROLE")" || die "$msg"
        if [ "$CFG_ROLE" = "follower" ] && ! have_secrets; then
            if [ -n "$CFG_JOIN_CODE" ]; then
                fetch_join "$CFG_JOIN_CODE" "$CFG_PEER_IP" || die "The join code did not work: $BUNDLE_ERR"
            else
                place="$(find_bundle)" || die "The follower needs the leader's certificate and secrets. Either:
  - On the leader: sudo furnace join-code. Add the code to $CONFIG as JOIN_CODE=... and run setup again.
  - Or copy /root/$BUNDLE_NAME from the leader to this server (for example to /root), then run setup
    again, or give the path: sudo furnace setup --config $CONFIG --bundle FILE"
                open_bundle "$place" || die "$BUNDLE_ERR"
            fi
            apply_bundle 1
        fi
        ;;
    ask)
        { : < /dev/tty; } 2>/dev/null || die "setup asks questions, but there is no terminal. Use: sudo furnace setup --config FILE"
        printf '\nFurnace setup. Press Enter to take the answer in [brackets].\n'
        if [ -f "$SETUP_CONF" ]; then
            load_file "$SETUP_CONF" CFG
            map_ips CFG 1
            OLD_IP_NAMES=0
            printf 'Furnace is already set up here. The brackets show the current answers.\n'
        fi
        ask_all
        if needs_first_token; then ask_first_token; fi
        ;;
esac
fill_defaults
validate_all
if [ "$CFG_USE_HTTPS" = "no" ]; then
    # Plain HTTP (a test-lab setting) was removed in 0.10.0.
    [ "$MODE" = "upgrade" ] || die "USE_HTTPS=no (plain HTTP) was removed in 0.10.0: Furnace always uses HTTPS. Remove the line."
    TODO+=("Plain HTTP (USE_HTTPS=no) was removed in 0.10.0. This server now uses HTTPS on port 443: change the list and API URLs to https:// and trust $ETC/tls/furnace.crt.")
fi
CFG_USE_HTTPS=yes
if [ "$OLD_SSH_ONLY" = 1 ] && [ "$CFG_REPLICATION_SSH_FALLBACK" = "yes" ]; then
    TODO+=("The SSH backup copy is on, because this server came from Furnace 0.7 or older (the other server may still need it). When both servers run this version, turn it off: sudo furnace setup on both, answer no to the SSH backup question.")
fi

if [ "$MODE" = "ask" ]; then
    show_settings
    printf '\n'
    ask_yes_no "Set up Furnace with these settings?" yes || { echo "Stopped. Nothing was changed."; exit 1; }
fi

if [ "$CFG_ROLE" = "leader" ]; then PRIORITY=150; else PRIORITY=100; fi
WEB_PORT=443
SCHEME=https
SSH_PORT="$( (sshd -T 2>/dev/null || true) | awk '$1=="port"{print $2; exit}')"
SSH_PORT="${SSH_PORT:-22}"
decide_host_firewall
if [ "$OLD_IP_NAMES" = 1 ]; then TODO+=("Settings file: THIS_IP and PEER_IP are the old names. Better: LEADER_IP and FOLLOWER_IP, the same file on both servers (see config.env.example)."); fi

# --------------------------------------------------------------------------- check mode

exists() { [ -e "$1" ] && echo "exists, kept" || echo "will be created"; }

if [ "$CHECK" -eq 1 ]; then
    say "Check mode: this is what setup would do. Nothing is changed."
    [ -f "$SETUP_CONF" ] && info "Furnace is already set up. This run would UPDATE it." \
                         || info "Furnace is not set up yet. This run would SET IT UP."
    info "Settings: role $CFG_ROLE, this server $CFG_THIS_IP on $CFG_INTERFACE, other server $CFG_PEER_IP,"
    info "  shared IP $CFG_SHARED_IP/$CFG_SHARED_IP_PREFIX, API from $CFG_API_ALLOW_FROM, lists from $CFG_LISTS_ALLOW_FROM"
    if [ "$CFG_REPLICATION_SSH_FALLBACK" = "yes" ]; then
        info "Users: furnace (no login), furnace-sync (the SSH backup copy only, forced command)"
    else
        info "Users: furnace (no login). SSH backup copy: off (no furnace-sync user)."
    fi
    info "Database $DATA/furnace.db: $(exists "$DATA/furnace.db")"
    info "API tokens and list login: in the database. Made on the leader if there are none, then shown once."
    info "Certificate $ETC/tls/furnace.crt: $(exists "$ETC/tls/furnace.crt")"
    info "Replication key $ETC/ssh/replica_key: $(exists "$ETC/ssh/replica_key")"
    if [ "$CFG_ROLE" = "follower" ] && ! have_secrets; then
        info "Follower: secrets come from the leader bundle $BUNDLE"
    fi
    info "System: ${PRETTY_NAME:-unknown} ($FAMILY family)"
    info "Files written: $SETUP_CONF, $ETC/furnace.env, $ETC/install.env, $NGINX_CONF,"
    info "  /etc/keepalived/keepalived.conf (priority $PRIORITY)"
    info "Host firewall active now: $FW_ACTIVE."
    if [ "$FW_APPLY" != "none" ]; then
        [ "$FW_TURN_ON" -eq 1 ] && info "Host firewall $FW_APPLY is off now: setup turns it on."
        info "Host firewall ($FW_APPLY): allow SSH port $SSH_PORT from $CFG_SSH_ALLOW_FROM$(ssh_peer_text " and $CFG_PEER_IP"),"
        info "  port $WEB_PORT from $(web_sources_text),"
        info "  VRRP (protocol 112) from $CFG_PEER_IP. Deny everything else coming in."
    else
        info "Host firewall: not touched. $FW_NONE_NOTE"
    fi
    selinux_on && info "SELinux is on: setup labels the Furnace folders and scripts and lets nginx reach the API."
    info "Services: furnace-api, nginx, keepalived, timers furnace-replicate, furnace-backup,"
    info "  furnace-cleanup (started by keepalived on the leader only)"
    info "Self-test: add 192.0.2.123, check it in the served EDL, remove it (leader only)"
    exit 0
fi

# --------------------------------------------------------------------------- setup steps

touch "$LOG" && chmod 0600 "$LOG"
exec > >(tee -a "$LOG") 2>&1
LOGGING=1
printf '\n----- Furnace setup started %s (%s) -----\n' "$(date -u +%FT%TZ)" "$MODE"

# The SSH backup copy needs the SSH server. Ubuntu 24.04 starts it through ssh.socket, the others through
# ssh.service (Debian family) or sshd.service (Red Hat family).
if [ "$CFG_REPLICATION_SSH_FALLBACK" = "yes" ] && ! systemctl is-active --quiet "$SSH_UNIT.service" \
        && ! systemctl is-active --quiet "$SSH_UNIT.socket"; then
    if systemctl cat "$SSH_UNIT.socket" >/dev/null 2>&1 && [ "${ID}:${VERSION_ID}" = "ubuntu:24.04" ]; then
        systemctl enable --quiet --now "$SSH_UNIT.socket"
    else
        systemctl enable --quiet --now "$SSH_UNIT.service"
    fi
    ok "SSH server started (needed for the SSH backup copy)"
fi

say "Creating users and folders"
if ! id furnace >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin furnace
fi
if [ "$CFG_REPLICATION_SSH_FALLBACK" = "yes" ]; then
    if ! id furnace-sync >/dev/null 2>&1; then
        useradd --system --user-group --home-dir "$SYNC_HOME" --create-home --shell /bin/sh furnace-sync
    fi
    usermod -p '*' furnace-sync   # no password; only the replication key can log in
fi
install -d -m 0750 -o root -g furnace "$ETC"
install -d -m 0750 -o root -g furnace "$ETC/tls"     # the furnace user reads the certificate (not the key)
install -d -m 0750 -o root -g furnace "$ETC/secrets" "$ETC/ssh"
install -d -m 0750 -o furnace -g furnace "$DATA" "$DATA/archive" "$DATA/backups" "$DATA/reports" "$DATA/replica"
install -d -m 0755 -o furnace -g furnace "$EDL"
install -d -m 2750 -o furnace -g "$NGINX_GROUP" "$AUTH"   # the list users file: nginx reads it
if [ "$CFG_REPLICATION_SSH_FALLBACK" = "yes" ]; then
    install -d -m 2750 -o furnace-sync -g furnace "$INCOMING"
    install -d -m 0700 -o furnace-sync -g furnace-sync "$SYNC_HOME" "$SYNC_HOME/.ssh"
fi
install -d -m 0755 "$STATE_DIR"
[ -f "$STATE_DIR/role" ] || echo follower > "$STATE_DIR/role"
if [ "$CFG_REPLICATION_SSH_FALLBACK" = "yes" ]; then
    ok "Users furnace and furnace-sync (SSH backup copy), and folders are ready"
else
    ok "User furnace and folders are ready (SSH backup copy off)"
fi

write_setup_conf
ok "Settings saved in $SETUP_CONF"

as_furnace() {
    runuser -u furnace -- env FURNACE_CONFIG="$ETC/furnace.env" "$PY" -m furnace "$@"
}
VERSION="$("$PY" -c 'import furnace; print(furnace.__version__)')"

# --------------------------------------------------------------------------- secrets

cert_names_ok() {
    local san
    san="$(openssl x509 -in "$1" -noout -ext subjectAltName 2>/dev/null || true)"
    for ip in "$CFG_SHARED_IP" "$CFG_THIS_IP" "$CFG_PEER_IP"; do
        grep -Eq "IP Address:${ip//./\\.}(,|\$)" <<< "$san" || return 1
    done
}

# make_cert: the self-signed certificate. All extensions come from a settings file of our own, not
# from -addext: OpenSSL 1.1.1 (RHEL 8) adds -addext to the extensions of the system openssl.cnf, so
# "Basic Constraints" was in the certificate twice, and such a certificate is not valid.
make_cert() {
    local key="$ETC/tls/furnace.key" crt="$ETC/tls/furnace.crt" cnf
    cnf="$(mktemp /tmp/furnace-cert.XXXXXX)"
    cat > "$cnf" <<EOF
[req]
prompt = no
distinguished_name = dn
x509_extensions = furnace

[dn]
CN = Furnace ${CFG_SHARED_IP}
O = Furnace

[furnace]
subjectAltName = IP:${CFG_SHARED_IP},IP:${CFG_THIS_IP},IP:${CFG_PEER_IP},IP:127.0.0.1
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,digitalSignature,keyEncipherment,keyCertSign
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
EOF
    (umask 077; openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes -config "$cnf" \
        -keyout "$key" -out "$crt" 2>/dev/null) || { rm -f "$cnf"; die "openssl could not make the certificate."; }
    rm -f "$cnf"
    chmod 0644 "$crt"
}

# cert_repeated FILE: true when an extension is in the certificate more than once (made by setup
# before 0.8 on RHEL 8 / OpenSSL 1.1.1). Such a certificate breaks the standard.
cert_repeated() {
    openssl x509 -in "$1" -noout -text 2>/dev/null | grep -oE '^ +X509v3 [A-Za-z ]+:' | sort | uniq -d | grep -q .
}

fix_secret_modes() {
    chown furnace:furnace "$ETC/ssh/replica_key" && chmod 0400 "$ETC/ssh/replica_key"
    chmod 0644 "$ETC/ssh/replica_key.pub"
    chmod 0600 "$ETC/tls/furnace.key"
    chmod 0644 "$ETC/tls/furnace.crt"
}

say "Preparing secrets (certificate, replication key)"
if [ "$CFG_ROLE" = "leader" ]; then
    if [ ! -f "$ETC/tls/furnace.crt" ] || [ ! -f "$ETC/tls/furnace.key" ]; then
        make_cert
        ok "Self-signed certificate created (10 years) for ${CFG_SHARED_IP}, ${CFG_THIS_IP}, ${CFG_PEER_IP}"
    else
        ok "Certificate kept (not changed)"
    fi
    if [ ! -f "$ETC/ssh/replica_key" ]; then
        ssh-keygen -q -t ed25519 -N "" -C "furnace-replica" -f "$ETC/ssh/replica_key"
        ok "Replication SSH key created"
    else
        ok "Replication SSH key kept (not changed)"
    fi
    fix_secret_modes
    BUNDLE="/root/$BUNDLE_NAME"
    (umask 077; tar -czf "$BUNDLE" -C "$ETC" tls/furnace.crt tls/furnace.key ssh/replica_key \
        ssh/replica_key.pub setup.conf)
    chmod 0600 "$BUNDLE"
    ok "Follower bundle written to $BUNDLE (secret: copy it to the follower, then delete the copy)"
else
    if have_secrets; then
        ok "Certificate and replication key kept (not changed)"
    else
        cert_names_ok "$UNPACK/tls/furnace.crt" \
            || die "The certificate in the bundle does not name ${CFG_SHARED_IP}, ${CFG_THIS_IP} and ${CFG_PEER_IP}."
        cp "$UNPACK/tls/furnace.crt" "$UNPACK/tls/furnace.key" "$ETC/tls/"
        cp "$UNPACK/ssh/replica_key" "$UNPACK/ssh/replica_key.pub" "$ETC/ssh/"
        fix_secret_modes
        rm -f "$BUNDLE"
        if [ "$JOINED" -eq 1 ]; then
            ok "Certificate and replication key fetched from the leader with the join code (the code is used up)"
        else
            ok "Certificate and replication key copied from the leader bundle (bundle file deleted)"
        fi
    fi
fi
if ! cert_names_ok "$ETC/tls/furnace.crt"; then
    warn "The certificate does not name all of ${CFG_SHARED_IP}, ${CFG_THIS_IP}, ${CFG_PEER_IP}."
    warn "If the IPs changed: delete $ETC/tls on the leader, run setup there, then on the follower."
    TODO+=("Fix the certificate: it does not name all server IPs.")
elif cert_repeated "$ETC/tls/furnace.crt"; then
    warn "The certificate has an extension twice (made by an older setup on RHEL 8). Some programs reject it."
    warn "Make a new one: delete $ETC/tls on the leader, run setup there, then on the follower. Then load the new furnace.crt on the firewall."
    TODO+=("Make a new certificate: the current one has an extension twice.")
fi
# Replication over HTTPS: both servers have the same TLS key, so both derive the same secret.
# Nothing new to copy, also not when an older install is upgraded. A new key gives a new secret.
NEW_SECRET="$(mktemp)"
"$PY" -c 'import sys; from furnace.replica import derive_secret; print(derive_secret(sys.argv[1]))' \
    "$ETC/tls/furnace.key" > "$NEW_SECRET"
install -m 0640 -o root -g furnace "$NEW_SECRET" "$ETC/secrets/replica-secret"
rm -f "$NEW_SECRET"
ok "Replication secret ready (from the shared certificate key)"

# --------------------------------------------------------------------------- config files

say "Writing configuration files"
export R_ROLE="$CFG_ROLE" R_THIS_IP="$CFG_THIS_IP" R_PEER_IP="$CFG_PEER_IP" R_SHARED_IP="$CFG_SHARED_IP"
export R_SHARED_IP_PREFIX="$CFG_SHARED_IP_PREFIX" R_INTERFACE="$CFG_INTERFACE" R_PRIORITY="$PRIORITY"
export R_VRRP_ROUTER_ID="$CFG_VRRP_ROUTER_ID" R_ARCHIVE_AFTER_DAYS="$CFG_ARCHIVE_AFTER_DAYS"
export R_MAX_IPS="$CFG_MAX_IPS" R_MAX_DOMAINS="$CFG_MAX_DOMAINS"

allow_lines() {
    local item out=""
    [ "$1" = "any" ] && { printf '        allow all;'; return; }
    for item in $(split_list "$1"); do out+="        allow ${item};"$'\n'; done
    printf '%s' "${out%$'\n'}"
}
R_LISTS_ALLOW="$(allow_lines "$CFG_LISTS_ALLOW_FROM")"
R_API_ALLOW="$(allow_lines "$CFG_API_ALLOW_FROM")"
R_LISTEN="    listen 443 ssl;"
R_TLS="    ssl_certificate $ETC/tls/furnace.crt;
    ssl_certificate_key $ETC/tls/furnace.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:furnace_tls:5m;"
R_REPLICA="    # Replication: only the other server may send a snapshot (with the replication secret).
    location = /replica/snapshot {
        allow ${CFG_PEER_IP};
        deny all;
        client_max_body_size 2g;
        proxy_request_buffering off;
        proxy_pass http://127.0.0.1:8081/replica/snapshot;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_read_timeout 120s;
    }
    # Join code: only the follower may fetch the (encrypted) secrets, once (sudo furnace join-code).
    location = /join {
        allow ${CFG_PEER_IP};
        deny all;
        client_max_body_size 4k;
        proxy_pass http://127.0.0.1:8081/join;
        proxy_set_header X-Forwarded-For \$remote_addr;
    }
"
export R_LISTS_ALLOW R_API_ALLOW R_LISTEN R_TLS R_REPLICA

render() { "$PY" "$LIB/render.py" "$1" "$2"; }

# Remember whether the settings change: only then does the API need a full restart.
ENV_CHANGED=1
NEW_ENV="$(mktemp)"
render "$LIB/templates/furnace.env.tmpl" "$NEW_ENV"
if cmp -s "$NEW_ENV" "$ETC/furnace.env"; then ENV_CHANGED=0; fi
install -m 0640 -o root -g furnace "$NEW_ENV" "$ETC/furnace.env"
rm -f "$NEW_ENV"

cat > "$ETC/install.env" <<EOF
# Non-secret settings, used by the Furnace helper scripts. Written by 'furnace setup'.
ROLE=$CFG_ROLE
THIS_IP=$CFG_THIS_IP
PEER_IP=$CFG_PEER_IP
SHARED_IP=$CFG_SHARED_IP
INTERFACE=$CFG_INTERFACE
REPLICATION_SSH_FALLBACK=$CFG_REPLICATION_SSH_FALLBACK
EOF
chmod 0644 "$ETC/install.env"
ok "Wrote $ETC/furnace.env and $ETC/install.env"

# The admin's own names that may never be whitelisted. Made once, empty; setup never changes it afterwards.
if [ ! -e "$ETC/whitelist-refuse.txt" ]; then
    cat > "$ETC/whitelist-refuse.txt" <<'EOF'
# Names that may never go on the domain whitelist, one per line. Furnace also refuses every shared platform
# and public name of the Public Suffix List (github.io, blogspot.com, co.uk, ...), which comes with Furnace.
# A name here also refuses every name above it: drive.google.com here refuses google.com too, because a
# whitelisted domain protects all its subdomains. Keep this file the same on both servers.
# Examples (remove the # to use them):
# dropbox.com
# drive.google.com
EOF
    chmod 0644 "$ETC/whitelist-refuse.txt"
    ok "Wrote $ETC/whitelist-refuse.txt (your own names that may never be whitelisted; empty)"
fi

if [ "$CFG_REPLICATION_SSH_FALLBACK" = "yes" ]; then
    PUBKEY="$(cat "$ETC/ssh/replica_key.pub")"
    printf 'restrict,command="%s/bin/furnace-receive-replica" %s\n' "$LIB" "$PUBKEY" > "$SYNC_HOME/.ssh/authorized_keys"
    chown furnace-sync:furnace-sync "$SYNC_HOME/.ssh/authorized_keys"
    chmod 0600 "$SYNC_HOME/.ssh/authorized_keys"
    ok "SSH backup copy prepared (furnace-sync may only receive snapshots)"
elif id furnace-sync >/dev/null 2>&1 || [ -d "$SYNC_HOME" ] || [ -d "$INCOMING" ]; then
    # Off (the default since 0.10.0): remove the SSH login of an older version. The key file stays: it
    # opens nothing without this login, and it lets the backup be turned on again without a new join code.
    systemctl disable --quiet --now furnace-apply-replica.path 2>/dev/null || true
    userdel furnace-sync >/dev/null 2>&1 || true
    rm -rf "$SYNC_HOME" "$INCOMING"
    ok "SSH backup copy is off: removed the user furnace-sync and its login (copies go over HTTPS)"
fi

render "$LIB/templates/nginx-furnace.conf.tmpl" "$NGINX_CONF"
if [ "$FAMILY" = "debian" ]; then
    ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/furnace
    if [ -L /etc/nginx/sites-enabled/default ]; then
        rm -f /etc/nginx/sites-enabled/default
        info "Disabled the nginx default site"
    fi
fi
nginx -t -q || die "The nginx configuration test failed. See the messages above."
ok "nginx configured (lists with a login, from $CFG_LISTS_ALLOW_FROM; API with a token, from $CFG_API_ALLOW_FROM; rate limits on)"

if [ -f /etc/keepalived/keepalived.conf ] && ! grep -q "^# Furnace" /etc/keepalived/keepalived.conf; then
    cp /etc/keepalived/keepalived.conf "/etc/keepalived/keepalived.conf.before-furnace.$(date +%Y%m%d%H%M%S)"
    warn "An existing keepalived.conf was saved with the suffix .before-furnace.*"
fi
# Restart keepalived only when its configuration really changes. A restart releases
# the shared IP, and with nopreempt the other server would keep it.
KEEPALIVED_CHANGED=1
NEW_KA="$(mktemp)"
(umask 077; R_KEEPALIVED_PASSWORD="$CFG_KEEPALIVED_PASSWORD" \
    render "$LIB/templates/keepalived.conf.tmpl" "$NEW_KA")
if cmp -s "$NEW_KA" /etc/keepalived/keepalived.conf; then
    KEEPALIVED_CHANGED=0
    rm -f "$NEW_KA"
else
    install -m 0600 -o root -g root "$NEW_KA" /etc/keepalived/keepalived.conf
    rm -f "$NEW_KA"
fi
ok "keepalived configured (unicast VRRP to $CFG_PEER_IP, priority $PRIORITY, nopreempt, health check)"

# If keepalived crashes, it cannot remove the shared IP and the role file still says
# "leader": both servers would own the IP and take changes (split-brain). systemd starts
# it again; at start keepalived drops the old shared IP and joins as the standby.
# The daemon-reload below picks this up; it does not restart a running keepalived.
KA_DROPIN=/etc/systemd/system/keepalived.service.d/furnace.conf
install -d -m 0755 "$(dirname "$KA_DROPIN")"
cat > "$KA_DROPIN.new" <<'EOF'
# Furnace - written by "furnace setup". Restart keepalived when it crashes, so a dead
# keepalived never leaves the shared IP on this server.
[Service]
Restart=on-failure
RestartSec=2
EOF
chmod 0644 "$KA_DROPIN.new"
mv -f "$KA_DROPIN.new" "$KA_DROPIN"
ok "keepalived restarts by itself after a crash ($KA_DROPIN)"

# The package brings the systemd units. If the API unit changed (for example in an
# upgrade), a reload is not enough: the API needs a restart.
API_UNIT_SUM="$(sha256sum "$(systemctl show -p FragmentPath --value furnace-api.service)" | cut -d' ' -f1)"   # not -P: RHEL 8 has systemd 239
[ "$API_UNIT_SUM" = "$(cat "$ETC/.api-unit.sha256" 2>/dev/null || true)" ] || ENV_CHANGED=1
systemctl daemon-reload
ok "systemd units ready"

# --------------------------------------------------------------------------- host firewall

if [ "$FW_APPLY" = "ufw" ]; then
    say "Configuring the host firewall (ufw)"
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    if [ "$CFG_SSH_ALLOW_FROM" = "any" ]; then
        ufw allow "$SSH_PORT/tcp" comment 'furnace ssh' >/dev/null
    else
        for src in $(split_list "$CFG_SSH_ALLOW_FROM"); do
            ufw allow from "$src" to any port "$SSH_PORT" proto tcp comment 'furnace ssh' >/dev/null
        done
    fi
    if [ "$CFG_REPLICATION_SSH_FALLBACK" = "yes" ]; then
        ufw allow from "$CFG_PEER_IP" to any port "$SSH_PORT" proto tcp comment 'furnace replication' >/dev/null
    else   # the SSH backup copy is off: the other server's SSH rule (from an older version) goes
        while num="$( (ufw status numbered 2>/dev/null || true) | grep -E '# furnace replication' | head -n1 \
                      | sed -E 's/^\[ *([0-9]+)\].*/\1/' || true)" && [ -n "$num" ]; do
            ufw --force delete "$num" >/dev/null
        done
    fi
    # Old web rules go first, so a changed answer (for example a new API caller IP) leaves nothing behind.
    while num="$( (ufw status numbered 2>/dev/null || true) | grep -E '# furnace web' | head -n1 \
                  | sed -E 's/^\[ *([0-9]+)\].*/\1/' || true)" && [ -n "$num" ]; do
        ufw --force delete "$num" >/dev/null
    done
    if [ "$CFG_LISTS_ALLOW_FROM" = "any" ] || [ "$CFG_API_ALLOW_FROM" = "any" ]; then
        ufw allow "$WEB_PORT/tcp" comment 'furnace web' >/dev/null
    else
        for src in $(split_list "$CFG_LISTS_ALLOW_FROM") $(split_list "$CFG_API_ALLOW_FROM") "$CFG_PEER_IP"; do
            ufw allow from "$src" to any port "$WEB_PORT" proto tcp comment 'furnace web' >/dev/null
        done
    fi
    if ! ufw allow from "$CFG_PEER_IP" to any proto vrrp comment 'furnace vrrp' >/dev/null 2>&1; then
        # Older ufw without the vrrp keyword: add a raw rule for protocol 112.
        if ! grep -q "furnace-vrrp" /etc/ufw/before.rules; then
            sed -i "/^COMMIT/i -A ufw-before-input -p 112 -s ${CFG_PEER_IP} -j ACCEPT -m comment --comment furnace-vrrp" \
                /etc/ufw/before.rules
        fi
    fi
    ufw --force enable >/dev/null
    [ "$FW_TURN_ON" -eq 1 ] && ok "ufw was off: turned it on, after the SSH rules were in"
    ok "Host firewall on: SSH ($SSH_PORT), port $WEB_PORT from $(web_sources_text), VRRP from $CFG_PEER_IP"
elif [ "$FW_APPLY" = "firewalld" ]; then
    say "Configuring the host firewall (firewalld)"
    systemctl enable --quiet --now firewalld
    ZONE="$(firewall-cmd --get-default-zone)"
    fwc() { firewall-cmd --permanent --zone="$ZONE" "$@" >/dev/null; }
    # Remove the rules of the last run first, so a changed answer leaves nothing behind.
    if [ -f "$FW_RULES" ]; then
        while IFS= read -r rule; do
            case "$rule" in
                rich:*) fwc --remove-rich-rule="${rule#rich:}" 2>/dev/null || true ;;
                removed-service:*) fwc --add-service="${rule#removed-service:}" ;;
            esac
        done < "$FW_RULES"
    fi
    family_of() { case "$1" in *:*) echo ipv6 ;; *) echo ipv4 ;; esac; }
    RULES=()
    allow_port() {  # allow_port PORT SOURCE|any
        if [ "$2" = "any" ]; then
            RULES+=("rich:rule port port=\"$1\" protocol=\"tcp\" accept")
        else
            RULES+=("rich:rule family=\"$(family_of "$2")\" source address=\"$2\" port port=\"$1\" protocol=\"tcp\" accept")
        fi
    }
    if [ "$CFG_SSH_ALLOW_FROM" = "any" ]; then
        allow_port "$SSH_PORT" any
    else
        for src in $(split_list "$CFG_SSH_ALLOW_FROM"); do allow_port "$SSH_PORT" "$src"; done
    fi
    if [ "$CFG_REPLICATION_SSH_FALLBACK" = "yes" ]; then allow_port "$SSH_PORT" "$CFG_PEER_IP"; fi
    if [ "$CFG_LISTS_ALLOW_FROM" = "any" ] || [ "$CFG_API_ALLOW_FROM" = "any" ]; then
        allow_port "$WEB_PORT" any
    else
        for src in $(split_list "$CFG_LISTS_ALLOW_FROM") $(split_list "$CFG_API_ALLOW_FROM") "$CFG_PEER_IP"; do
            allow_port "$WEB_PORT" "$src"
        done
    fi
    RULES+=("rich:rule family=\"ipv4\" source address=\"$CFG_PEER_IP\" protocol value=\"vrrp\" accept")
    for rule in "${RULES[@]}"; do fwc --add-rich-rule="${rule#rich:}"; done
    # SSH limited to some addresses: the zone's open "ssh" service must go, like ufw's default deny.
    if [ "$CFG_SSH_ALLOW_FROM" != "any" ] && firewall-cmd --permanent --zone="$ZONE" --query-service=ssh >/dev/null 2>&1; then
        fwc --remove-service=ssh
        RULES+=("removed-service:ssh")
    fi
    printf '%s\n' "${RULES[@]}" > "$FW_RULES"
    chmod 0600 "$FW_RULES"
    firewall-cmd --reload >/dev/null
    [ "$FW_TURN_ON" -eq 1 ] && ok "firewalld was off: turned it on (SSH stays open)"
    ok "Host firewall (firewalld, zone $ZONE): SSH ($SSH_PORT), port $WEB_PORT from $(web_sources_text), VRRP from $CFG_PEER_IP"
else
    TODO+=("$FW_NONE_NOTE")
fi

# --------------------------------------------------------------------------- SELinux

# Red Hat family systems run SELinux. Without these labels nginx cannot read the lists or reach the API,
# keepalived cannot run the health check, and sshd cannot read the replication key.
if selinux_on; then
    say "Configuring SELinux"
    command -v semanage >/dev/null 2>&1 \
        || die "SELinux is on, but the semanage command is missing. Install the package policycoreutils-python-utils."
    label() {  # label TYPE PATH_REGEX: add the rule, or change it if it is there already
        semanage fcontext -a -t "$1" "$2" 2>/dev/null || semanage fcontext -m -t "$1" "$2"
    }
    label httpd_sys_content_t "$EDL(/.*)?"
    label httpd_sys_content_t "$AUTH(/.*)?"
    label ssh_home_t "$SYNC_HOME/\.ssh(/.*)?"   # the SSH backup copy (when it is on)
    # The scripts are programs: as bin_t, systemd runs them in the normal service domain
    # (unconfined_service_t). As lib_t they would stay in init_t, which may not run ssh.
    label bin_t "$LIB/bin(/.*)?"
    label keepalived_unconfined_script_exec_t "$LIB/bin/furnace-(health-check|notify)"
    restorecon -RF "$EDL" "$AUTH" "$LIB/bin"
    if [ -d "$SYNC_HOME/.ssh" ]; then restorecon -RF "$SYNC_HOME/.ssh"; fi
    if [ "$(getsebool httpd_can_network_connect 2>/dev/null | awk '{print $3}')" != "on" ]; then
        setsebool -P httpd_can_network_connect on   # nginx -> the API on 127.0.0.1:8081
    fi
    ok "SELinux labels set (lists, logins, replication key, Furnace scripts); nginx may reach the API"
fi

# --------------------------------------------------------------------------- start

say "Starting services"
as_furnace rebuild >/dev/null
ok "Database ready and EDL files written (existing data kept)"
systemctl enable --quiet furnace-api.service
if systemctl is-active --quiet furnace-api.service && [ "$ENV_CHANGED" -eq 0 ]; then
    # Upgrade without downtime: new workers load the new code, the main process keeps serving.
    systemctl reload furnace-api.service
    info "furnace-api reloaded with the new program (no downtime)."
else
    systemctl restart furnace-api.service
fi
echo "$API_UNIT_SUM" > "$ETC/.api-unit.sha256"
systemctl enable --quiet nginx
if systemctl is-active --quiet nginx; then systemctl reload nginx; else systemctl start nginx; fi
systemctl enable --quiet --now furnace-replicate.timer furnace-backup.timer
if [ "$CFG_REPLICATION_SSH_FALLBACK" = "yes" ]; then
    systemctl enable --quiet --now furnace-apply-replica.path   # loads the snapshots that came over SSH
fi
systemctl enable --quiet keepalived
if [ "$KEEPALIVED_CHANGED" -eq 1 ] || ! systemctl is-active --quiet keepalived; then
    systemctl restart keepalived
    info "keepalived (re)started. If this server held the shared IP, the other server may hold it now."
else
    info "keepalived configuration unchanged. Not restarted, so the shared IP does not move."
fi
ok "Services started: furnace-api, nginx, keepalived, replication, backup"

# --------------------------------------------------------------------------- self-test

CURL=(curl -sS --max-time 10)
CURL+=(--cacert "$ETC/tls/furnace.crt")
BASE="${SCHEME}://127.0.0.1"
TEST_IP=192.0.2.123

pass() { ok "Self-test: $*"; }
fail() { printf '    [FAIL] Self-test: %s\n' "$*"; FAILED=1; TODO+=("Self-test failed: $*"); }

# The root-owned flag allows the test range and holds a token for the self-test, for 10 minutes at most.
# No API restart is needed (a restart could move the shared IP). It is removed at the end.
start_selftest() {
    local token
    token="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')"
    (umask 027; printf '%s\n' "$token" > "$STATE_DIR/selftest")
    chown root:furnace "$STATE_DIR/selftest"
    HDR="$(mktemp)"
    chmod 0600 "$HDR"
    printf 'Authorization: Bearer %s\n' "$token" > "$HDR"
}

current_role() { cat "$STATE_DIR/role" 2>/dev/null || echo unknown; }

# On the server that owns the shared IP: make the first API token and the EDL login if there are
# none, and keep the tokens of version 0.2 working. New secrets are shown once at the end, never logged.
init_secrets() {
    local legacy=() out kind name a b
    [ -f "$ETC/tokens.env" ] && legacy=(--legacy-tokens "$ETC/tokens.env")
    if ! out="$(as_furnace init-secrets "${legacy[@]}" --token-name "$CFG_TOKEN_NAME" \
                --allow "$CFG_TOKEN_ALLOW" --expires-days "$CFG_TOKEN_EXPIRES_DAYS")"; then
        fail "could not make the API token and the list login"
        return
    fi
    while IFS=$'\t' read -r kind name a b; do
        case "$kind" in
            migrated) ok "The API token of version 0.2 keeps working, now named '$name'" ;;
            token) NEW_SECRETS+=("API token '$name'. $a Token:" "    $b") ;;
            edl) NEW_SECRETS+=("List login (for the device that downloads the lists): user '$name', password:" "    $a")
                 EDL_TEST_LOGIN="$name:$a" ;;
        esac
    done <<< "$out"
    ok "API token and list login ready (see: sudo furnace token list, sudo furnace edl-user list)"
}
EDL_TEST_LOGIN=""

served_blocklist_has() {
    local body
    body="$("${CURL[@]}" "$BASE/edl/blocklist.txt" 2>/dev/null || true)"
    grep -qx "$1" <<< "$body"
}

peer_is_leader() {
    local answer
    answer="$("${CURL[@]}" "${SCHEME}://${CFG_PEER_IP}/health" 2>/dev/null || true)"
    grep -q '"role": *"leader"' <<< "$answer"
}

wait_health() {
    for _ in $(seq 1 30); do
        "${CURL[@]}" -o /dev/null -f "$BASE/health" 2>/dev/null && return 0
        sleep 1
    done
    return 1
}

code_of() { "${CURL[@]}" -o /dev/null -w '%{http_code}' "$@" 2>/dev/null || true; }

say "Self-test"
start_selftest
if wait_health; then pass "/health answers"; else fail "/health does not answer"; fi
for file in blocklist.txt domains.txt urls.txt; do
    [ "$(code_of "$BASE/edl/$file")" = "200" ] && pass "nginx serves /edl/$file" || fail "nginx does not serve /edl/$file"
done
for path in /edl/archive.csv /archive/archive.csv /furnace.db /edl/../furnace/furnace.db; do
    c="$(code_of --path-as-is "$BASE$path")"
    [ "$c" = "404" ] || [ "$c" = "403" ] || [ "$c" = "400" ] && pass "$path is not served ($c)" || fail "$path answered $c"
done
[ "$(code_of -X POST "$BASE/api/v1/ips")" = "401" ] && pass "the API refuses calls without a token" \
    || fail "the API did not refuse a call without a token"
# From any other address the lists need a login (401), or the address is not allowed at all (403).
c="$(code_of "${SCHEME}://${CFG_THIS_IP}/edl/blocklist.txt")"
[ "$c" = "401" ] || [ "$c" = "403" ] && pass "the lists need a login from other addresses ($c)" \
    || fail "the lists answered $c without a login (expected 401 or 403)"
c="$(code_of -u "lists:wrong-password" "${SCHEME}://${CFG_THIS_IP}/edl/blocklist.txt")"
[ "$c" = "401" ] || [ "$c" = "403" ] && pass "a wrong list password is refused ($c)" \
    || fail "a wrong list password answered $c (expected 401 or 403)"

if [ "$CFG_ROLE" = "leader" ]; then
    for _ in $(seq 1 30); do
        [ "$(cat "$STATE_DIR/role" 2>/dev/null)" = "leader" ] && break
        sleep 1
    done
    if [ "$(cat "$STATE_DIR/role" 2>/dev/null)" != "leader" ] && peer_is_leader; then
        warn "The other server ($CFG_PEER_IP) owns the shared IP now (nopreempt). That is normal after a failover."
        warn "The add/remove self-test runs only on the server that owns the shared IP. Skipped."
    elif [ "$(cat "$STATE_DIR/role" 2>/dev/null)" != "leader" ]; then
        fail "keepalived did not make this server the leader within 30 seconds"
    else
        pass "keepalived made this server the leader"
        init_secrets
        c="$(code_of -X POST -H @"$HDR" -H 'Content-Type: application/json' \
             -d "{\"ip\":\"$TEST_IP\",\"reason\":\"setup self-test\",\"added_by\":\"furnace setup\"}" "$BASE/api/v1/ips")"
        [ "$c" = "201" ] || [ "$c" = "200" ] && pass "added $TEST_IP through the API ($c)" || fail "could not add $TEST_IP (HTTP $c)"
        served_blocklist_has "$TEST_IP" && pass "$TEST_IP is in the blocklist served by nginx" \
            || fail "$TEST_IP is not in the served blocklist"
        c="$(code_of -X DELETE -H @"$HDR" "$BASE/api/v1/ips/$TEST_IP?removed_by=furnace%20setup")"
        [ "$c" = "200" ] && pass "removed $TEST_IP through the API" || fail "could not remove $TEST_IP (HTTP $c)"
        served_blocklist_has "$TEST_IP" && fail "$TEST_IP is still in the served blocklist" \
            || pass "$TEST_IP is gone from the served blocklist"
        if [ -n "$EDL_TEST_LOGIN" ] && [ "$CFG_LISTS_ALLOW_FROM" = "any" ]; then
            [ "$(code_of -u "$EDL_TEST_LOGIN" "${SCHEME}://${CFG_THIS_IP}/edl/blocklist.txt")" = "200" ] \
                && pass "the new list login can download the list" || fail "the new list login cannot download the list"
        fi
    fi
else
    # Check the role first: a follower that owns the shared IP accepts changes, and the test
    # IP must never land on the real list.
    if [ "$(current_role)" = "leader" ]; then
        warn "This follower owns the shared IP right now (the leader is down or not set up yet)."
        warn "The 'follower refuses changes' test is skipped, so no test IP is added to the list."
        init_secrets
    else
        c="$(code_of -X POST -H @"$HDR" -H 'Content-Type: application/json' -d "{\"ip\":\"$TEST_IP\"}" "$BASE/api/v1/ips")"
        if [ "$c" = "503" ]; then
            pass "the follower refuses changes while it is not the leader (503)"
        else
            fail "the follower answered $c to a change (expected 503)"
            # It was accepted after all (the role changed a moment ago): take the test IP out again.
            [ "$c" = "201" ] || [ "$c" = "200" ] && code_of -X DELETE -H @"$HDR" \
                "$BASE/api/v1/ips/$TEST_IP?removed_by=furnace%20setup" >/dev/null || true
        fi
    fi
fi
rm -f "$HDR" "$STATE_DIR/selftest"; HDR=""

# Version 0.2 kept the API tokens in a file. They are in the database now (also on this server
# once the leader's copy arrived), so the file can go.
if [ -f "$ETC/tokens.env" ] && [ "$(token_count)" -gt 0 ]; then
    rm -f "$ETC/tokens.env"
    ok "Removed $ETC/tokens.env (version 0.2). The API tokens are in the database now."
fi

# --------------------------------------------------------------------------- summary

say "Summary"
for item in "${DONE[@]}"; do printf '  - %s\n' "$item"; done

if [ "${#NEW_SECRETS[@]}" -gt 0 ]; then
    if [ "$MODE" = "ask" ] && { : > /dev/tty; } 2>/dev/null; then   # a person is at the terminal
        {
            printf '\n  ============ NEW SECRETS (shown only once, not in the log) ============\n'
            printf '  Store them now. Furnace keeps only a hash and cannot show them again.\n'
            printf '  Lost one? Make a new one: sudo furnace token rotate NAME / edl-user rotate NAME\n\n'
            printf '  %s\n' "${NEW_SECRETS[@]}"
            printf '  =======================================================================\n\n'
        } > /dev/tty
    else
        (umask 077; {
            echo "# New Furnace secrets. Store them, then delete this file: sudo rm $SECRETS_FILE"
            printf '%s\n' "${NEW_SECRETS[@]}"
        } > "$SECRETS_FILE")
        TODO+=("New secrets (API tokens or list login) are in $SECRETS_FILE. Store them, then delete the file.")
    fi
    case "${NEW_SECRETS[*]}" in
        *"List login"*) TODO+=("The lists now need a login. Give the user and password to the device that downloads them (for example in its EDL or threat feed settings).") ;;
    esac
fi

if [ "$MODE" != "upgrade" ]; then
    if [ "$CFG_ROLE" = "leader" ]; then
        if [ "$FAMILY" = "debian" ]; then
            PKG_HINT="the .deb file there, then run: sudo apt install ./furnace_${VERSION}_all.deb"
        else
            PKG_HINT="the .rpm file there, then run: sudo dnf install ./furnace-${VERSION}-1.noarch.rpm"
        fi
        TODO+=("Follower ($CFG_PEER_IP): copy /root/$BUNDLE_NAME and $PKG_HINT && sudo furnace setup")
    fi
    TODO+=("Allow on the network: list readers and API callers -> $CFG_SHARED_IP:$WEB_PORT, HTTPS and VRRP between $CFG_THIS_IP and $CFG_PEER_IP$(ssh_peer_text ", and SSH (the backup copy is on)").")
    TODO+=("Device that downloads the lists (for example a firewall): trust $ETC/tls/furnace.crt and add the two lists with the list login. See the README.")
    TODO+=("System that calls the API (for example a SOAR): store the API token and trust the certificate. See the API guide in the README.")
    TODO+=("After both servers are set up: run sudo furnace failover-test on the leader.")
fi

if [ "${#TODO[@]}" -gt 0 ]; then
    printf '\n  Still to do by hand:\n'
    for item in "${TODO[@]}"; do printf '  - %s\n' "$item"; done
fi
printf '\n  Log file: %s (no secrets in it)\n' "$LOG"

if [ "$FAILED" -ne 0 ]; then
    printf '\nSetup finished, but the self-test FAILED. Check the items above.\n' >&2
    exit 1
fi
printf '\nFurnace %s is set up and the self-test passed. Check it any time with: sudo furnace status\n' "$VERSION"

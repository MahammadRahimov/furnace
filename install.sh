#!/usr/bin/env bash
# Furnace installer for Ubuntu 22.04 and 24.04.
#
#   sudo ./install.sh            install, or upgrade when Furnace is already installed
#   sudo ./install.sh --check    show what would be done, change nothing
#   sudo ./install.sh --config FILE   use another settings file (default: ./config.env)
#
# Safe to run again: a second run upgrades the program. It never wipes the
# database and never changes the API tokens or the certificate.
set -Eeuo pipefail
umask 022

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$HERE/config.env"
CHECK=0
LOG=/var/log/furnace-install.log
BUNDLE_NAME=furnace-peer-bundle.tar.gz

ETC=/etc/furnace
OPT=/opt/furnace
DATA=/var/lib/furnace
EDL=/var/lib/furnace-edl
INCOMING=/var/lib/furnace-incoming
SYNC_HOME=/var/lib/furnace-sync
STATE_DIR=/run/furnace-state

DONE=()
TODO=()
FAILED=0
NEW_TOKENS=0

# --------------------------------------------------------------------------- output

say()  { printf '\n==> %s  [%s]\n' "$*" "$(date +%T)"; }
ok()   { printf '    [ok] %s\n' "$*"; DONE+=("$*"); }
info() { printf '    %s\n' "$*"; }
warn() { printf '    [warning] %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
on_error() {
    printf '\nERROR: the install stopped at line %s. Nothing after this point was done.\n' "$1" >&2
    [ "$CHECK" -eq 0 ] && printf 'See %s for details.\n' "$LOG" >&2
}
trap 'on_error $LINENO' ERR

usage() {
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK=1 ;;
        --config) shift; [ $# -gt 0 ] || usage 2; CONFIG="$1" ;;
        -h|--help) usage 0 ;;
        *) echo "Unknown option: $1" >&2; usage 2 ;;
    esac
    shift
done

# --------------------------------------------------------------------------- basic checks

[ "$(id -u)" -eq 0 ] || die "Run this script as root: sudo ./install.sh"

[ -r /etc/os-release ] || die "Cannot read /etc/os-release. Only Ubuntu 22.04 and 24.04 are supported."
# shellcheck source=/dev/null
. /etc/os-release
if [ "${ID:-}" != "ubuntu" ] || { [ "${VERSION_ID:-}" != "22.04" ] && [ "${VERSION_ID:-}" != "24.04" ]; }; then
    die "This is ${PRETTY_NAME:-an unknown system}. Only Ubuntu 22.04 and 24.04 are supported."
fi

if [ "$CHECK" -eq 0 ]; then
    touch "$LOG" && chmod 0600 "$LOG"
    exec > >(tee -a "$LOG") 2>&1
    printf '\n----- Furnace install started %s -----\n' "$(date -u +%FT%TZ)"
fi

# --------------------------------------------------------------------------- settings

KEYS="ROLE THIS_IP PEER_IP SHARED_IP SHARED_IP_PREFIX INTERFACE XSOAR_IP FIREWALL_IP ARCHIVE_AFTER_DAYS
KEEPALIVED_PASSWORD VRRP_ROUTER_ID USE_HTTPS MAX_IPS MAX_DOMAINS SSH_ALLOW_FROM HOST_FIREWALL"

load_config() {
    [ -f "$CONFIG" ] || die "The settings file $CONFIG does not exist. Copy config.env.example to config.env and fill it in."
    local line key value
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        line="${line#"${line%%[![:space:]]*}"}"
        [ -z "$line" ] && continue
        [ "${line:0:1}" = "#" ] && continue
        [[ "$line" == *=* ]] || die "Bad line in $CONFIG: '$line'. Use KEY=VALUE."
        key="${line%%=*}"
        value="${line#*=}"
        value="${value%"${value##*[![:space:]]}"}"
        if [[ "$value" =~ ^\"(.*)\"$ ]] || [[ "$value" =~ ^\'(.*)\'$ ]]; then
            value="${BASH_REMATCH[1]}"
        fi
        case " $(echo $KEYS) " in
            *" $key "*) printf -v "CFG_$key" '%s' "$value" ;;
            *) die "Unknown setting '$key' in $CONFIG." ;;
        esac
    done < "$CONFIG"
    : "${CFG_SHARED_IP_PREFIX:=24}" "${CFG_VRRP_ROUTER_ID:=51}" "${CFG_USE_HTTPS:=yes}"
    : "${CFG_MAX_IPS:=50000}" "${CFG_MAX_DOMAINS:=50000}" "${CFG_SSH_ALLOW_FROM:=any}"
    : "${CFG_HOST_FIREWALL:=ufw}" "${CFG_ARCHIVE_AFTER_DAYS:=30}"
    local k
    for k in $KEYS; do
        local var="CFG_$k"
        [ -n "${!var:-}" ] || die "The setting $k is empty in $CONFIG."
    done
}

is_ipv4() {
    python3 - "$1" <<'PY' 2>/dev/null
import ipaddress, sys
ip = ipaddress.ip_address(sys.argv[1])
sys.exit(0 if ip.version == 4 and str(ip) == sys.argv[1] else 1)
PY
}

is_ip_or_cidr() {
    python3 - "$1" <<'PY' 2>/dev/null
import ipaddress, sys
ipaddress.ip_network(sys.argv[1], strict=True)
PY
}

is_int_between() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge "$2" ] && [ "$1" -le "$3" ]; }

split_list() { echo "$1" | tr ',' ' '; }

validate_config() {
    [[ "$CFG_ROLE" =~ ^(leader|follower)$ ]] || die "ROLE must be 'leader' or 'follower', not '$CFG_ROLE'."
    local name
    for name in THIS_IP PEER_IP SHARED_IP; do
        local var="CFG_$name"
        is_ipv4 "${!var}" || die "$name '${!var}' is not a valid IPv4 address."
    done
    [ "$CFG_THIS_IP" != "$CFG_PEER_IP" ] || die "THIS_IP and PEER_IP must be different."
    [ "$CFG_SHARED_IP" != "$CFG_THIS_IP" ] && [ "$CFG_SHARED_IP" != "$CFG_PEER_IP" ] \
        || die "SHARED_IP must be a third address, not THIS_IP or PEER_IP."
    is_int_between "$CFG_SHARED_IP_PREFIX" 8 32 || die "SHARED_IP_PREFIX must be a number from 8 to 32."
    [[ "$CFG_INTERFACE" =~ ^[A-Za-z0-9_.:-]{1,15}$ ]] || die "INTERFACE '$CFG_INTERFACE' is not a valid network card name."
    ip link show "$CFG_INTERFACE" >/dev/null 2>&1 \
        || die "The network card '$CFG_INTERFACE' does not exist on this server. See: ip -br addr"
    ip -o -4 addr show dev "$CFG_INTERFACE" | grep -qw "inet ${CFG_THIS_IP}/[0-9]*" \
        || die "THIS_IP ${CFG_THIS_IP} is not set on the network card ${CFG_INTERFACE}. See: ip -br addr"
    local item
    for item in $(split_list "$CFG_XSOAR_IP"); do
        is_ip_or_cidr "$item" || die "XSOAR_IP contains '$item'. That is not a valid IP address."
    done
    for item in $(split_list "$CFG_FIREWALL_IP"); do
        is_ip_or_cidr "$item" || die "FIREWALL_IP contains '$item'. That is not a valid IP address."
    done
    is_int_between "$CFG_ARCHIVE_AFTER_DAYS" 1 3650 || die "ARCHIVE_AFTER_DAYS must be a number from 1 to 3650."
    [[ "$CFG_KEEPALIVED_PASSWORD" =~ ^[A-Za-z0-9._@%+=:!-]{6,8}$ ]] \
        || die "KEEPALIVED_PASSWORD must be 6 to 8 characters: letters, numbers or . _ @ % + = : ! -"
    [ "$CFG_KEEPALIVED_PASSWORD" != "Chg3Me!x" ] || die "KEEPALIVED_PASSWORD is still the example value. Choose your own."
    is_int_between "$CFG_VRRP_ROUTER_ID" 1 255 || die "VRRP_ROUTER_ID must be a number from 1 to 255."
    [[ "$CFG_USE_HTTPS" =~ ^(yes|no)$ ]] || die "USE_HTTPS must be 'yes' or 'no'."
    is_int_between "$CFG_MAX_IPS" 0 10000000 || die "MAX_IPS must be a number (0 means no limit)."
    is_int_between "$CFG_MAX_DOMAINS" 0 10000000 || die "MAX_DOMAINS must be a number (0 means no limit)."
    if [ "$CFG_SSH_ALLOW_FROM" != "any" ]; then
        for item in $(split_list "$CFG_SSH_ALLOW_FROM"); do
            is_ip_or_cidr "$item" || die "SSH_ALLOW_FROM contains '$item'. Use 'any' or IP addresses."
        done
    fi
    [[ "$CFG_HOST_FIREWALL" =~ ^(ufw|none)$ ]] || die "HOST_FIREWALL must be 'ufw' or 'none'."
}

say "Reading settings from $CONFIG"
load_config
validate_config
ok "Settings are valid (role: $CFG_ROLE, this server: $CFG_THIS_IP, shared IP: $CFG_SHARED_IP)"
[ "$CFG_USE_HTTPS" = "yes" ] || warn "USE_HTTPS=no. Plain HTTP is only for a test lab. Do not use it in production."

if [ "$CFG_ROLE" = "leader" ]; then PRIORITY=150; else PRIORITY=100; fi
if [ "$CFG_USE_HTTPS" = "yes" ]; then WEB_PORT=443; SCHEME=https; else WEB_PORT=80; SCHEME=http; fi
SSH_PORT="$( (sshd -T 2>/dev/null || true) | awk '$1=="port"{print $2; exit}')"
SSH_PORT="${SSH_PORT:-22}"
INSTALLED=0
[ -f "$OPT/venv/bin/furnace-admin" ] && INSTALLED=1

# --------------------------------------------------------------------------- check mode

exists() { [ -e "$1" ] && echo "exists, kept" || echo "will be created"; }

if [ "$CHECK" -eq 1 ]; then
    say "Check mode: this is what the install would do. Nothing is changed."
    [ "$INSTALLED" -eq 1 ] && info "Furnace is already installed. This run would UPGRADE it." \
                           || info "Furnace is not installed yet. This run would INSTALL it."
    info "Packages: python3-venv nginx keepalived openssl curl ufw openssh-server sqlite3"
    info "Users: furnace (no login), furnace-sync (replication only, forced command)"
    info "Program: $OPT/venv (pip install of this folder)"
    info "Database $DATA/furnace.db: $(exists "$DATA/furnace.db")"
    info "API tokens $ETC/tokens.env: $(exists "$ETC/tokens.env")"
    if [ "$CFG_USE_HTTPS" = "yes" ]; then
        info "Certificate $ETC/tls/furnace.crt: $(exists "$ETC/tls/furnace.crt")"
    fi
    info "Replication key $ETC/ssh/replica_key: $(exists "$ETC/ssh/replica_key")"
    if [ "$CFG_ROLE" = "follower" ] && [ ! -f "$ETC/tokens.env" ]; then
        info "Follower: needs $BUNDLE_NAME from the leader (in $HERE or the home folder of \$SUDO_USER)."
    fi
    info "Files written: $ETC/furnace.env, $ETC/install.env, /etc/nginx/sites-available/furnace,"
    info "  /etc/keepalived/keepalived.conf (priority $PRIORITY), systemd units in /etc/systemd/system"
    if [ "$CFG_HOST_FIREWALL" = "ufw" ]; then
        info "Host firewall (ufw): allow SSH port $SSH_PORT from $CFG_SSH_ALLOW_FROM and $CFG_PEER_IP,"
        info "  port $WEB_PORT from firewall ($CFG_FIREWALL_IP), XSOAR ($CFG_XSOAR_IP) and $CFG_PEER_IP,"
        info "  VRRP (protocol 112) from $CFG_PEER_IP. Deny everything else coming in."
    else
        info "Host firewall: not touched (HOST_FIREWALL=none)."
    fi
    info "Services: furnace-api, nginx, keepalived, timers furnace-replicate, furnace-backup,"
    info "  furnace-cleanup (started by keepalived on the leader only)"
    info "Self-test: add 192.0.2.123, check it in the served EDL, remove it (leader only)"
    exit 0
fi

# --------------------------------------------------------------------------- install steps

say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv nginx keepalived openssl curl ufw openssh-server \
    openssh-client sqlite3 gzip >/dev/null
ok "Packages installed"

# Replication needs the SSH server. Ubuntu 24.04 starts it through ssh.socket, 22.04 through ssh.service.
if ! systemctl is-active --quiet ssh.service && ! systemctl is-active --quiet ssh.socket; then
    if systemctl cat ssh.socket >/dev/null 2>&1 && [ "${VERSION_ID}" = "24.04" ]; then
        systemctl enable --quiet --now ssh.socket
    else
        systemctl enable --quiet --now ssh.service
    fi
    ok "SSH server started (needed for replication)"
fi

say "Creating users and folders"
if ! id furnace >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin furnace
fi
if ! id furnace-sync >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$SYNC_HOME" --create-home --shell /bin/sh furnace-sync
fi
usermod -p '*' furnace-sync   # no password; only the replication key can log in
install -d -m 0750 -o root -g furnace "$ETC"
install -d -m 0700 -o root -g root "$ETC/tls"
install -d -m 0750 -o root -g furnace "$ETC/ssh"
install -d -m 0750 -o furnace -g furnace "$DATA" "$DATA/archive" "$DATA/backups" "$DATA/reports" "$DATA/replica"
install -d -m 0755 -o furnace -g furnace "$EDL"
install -d -m 2750 -o furnace-sync -g furnace "$INCOMING"
install -d -m 0700 -o furnace-sync -g furnace-sync "$SYNC_HOME" "$SYNC_HOME/.ssh"
install -d -m 0755 "$OPT" "$OPT/bin"
install -d -m 0755 "$STATE_DIR"
[ -f "$STATE_DIR/role" ] || echo follower > "$STATE_DIR/role"
ok "Users furnace and furnace-sync, and folders are ready"

say "Installing the Furnace program"
[ -x "$OPT/venv/bin/python" ] || python3 -m venv "$OPT/venv"
PIP=("$OPT/venv/bin/pip" install --quiet --no-cache-dir --disable-pip-version-check)
if [ -d "$HERE/wheels" ]; then
    PIP+=(--no-index --find-links "$HERE/wheels")
    info "Using the offline packages in $HERE/wheels"
fi
"${PIP[@]}" -r "$HERE/requirements.txt" \
    || die "Could not install Flask and gunicorn. The server needs access to PyPI (or a proxy), or put wheel files in $HERE/wheels."
BUILD="$(mktemp -d)"
cp -r "$HERE/furnace" "$HERE/pyproject.toml" "$HERE/README.md" "$BUILD/"
"${PIP[@]}" --force-reinstall --no-deps "$BUILD"
rm -rf "$BUILD"
VERSION="$("$OPT/venv/bin/python" -c 'import furnace; print(furnace.__version__)')"
ok "Furnace $VERSION installed in $OPT/venv"

# --------------------------------------------------------------------------- secrets

cert_names_ok() {
    local san
    san="$(openssl x509 -in "$1" -noout -ext subjectAltName 2>/dev/null || true)"
    for ip in "$CFG_SHARED_IP" "$CFG_THIS_IP" "$CFG_PEER_IP"; do
        grep -Eq "IP Address:${ip//./\\.}(,|\$)" <<< "$san" || return 1
    done
}

make_tokens() {
    local block whitelist
    block="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    whitelist="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    (umask 077; printf 'FURNACE_BLOCK_TOKEN=%s\nFURNACE_WHITELIST_TOKEN=%s\n' "$block" "$whitelist" > "$ETC/tokens.env")
}

make_cert() {
    local key="$ETC/tls/furnace.key" crt="$ETC/tls/furnace.crt"
    (umask 077; openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes \
        -keyout "$key" -out "$crt" -subj "/CN=Furnace ${CFG_SHARED_IP}/O=Furnace" \
        -addext "subjectAltName=IP:${CFG_SHARED_IP},IP:${CFG_THIS_IP},IP:${CFG_PEER_IP},IP:127.0.0.1" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign" \
        -addext "extendedKeyUsage=serverAuth" 2>/dev/null)
    chmod 0644 "$crt"
}

fix_secret_modes() {
    chown furnace:furnace "$ETC/tokens.env" && chmod 0400 "$ETC/tokens.env"
    chown furnace:furnace "$ETC/ssh/replica_key" && chmod 0400 "$ETC/ssh/replica_key"
    chmod 0644 "$ETC/ssh/replica_key.pub"
    chmod 0600 "$ETC/tls/furnace.key"
    chmod 0644 "$ETC/tls/furnace.crt"
}

find_bundle() {
    local place
    for place in "$HERE/$BUNDLE_NAME" "${SUDO_USER:+$(getent passwd "$SUDO_USER" | cut -d: -f6)/$BUNDLE_NAME}"; do
        [ -n "$place" ] && [ -f "$place" ] && { echo "$place"; return 0; }
    done
    return 1
}

say "Preparing secrets (API tokens, certificate, replication key)"
if [ "$CFG_ROLE" = "leader" ]; then
    if [ ! -f "$ETC/tokens.env" ]; then
        make_tokens
        NEW_TOKENS=1
        ok "New API tokens created in $ETC/tokens.env"
    else
        ok "API tokens kept (not changed)"
    fi
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
    (umask 077; tar -czf "$BUNDLE" -C "$ETC" tokens.env tls/furnace.crt tls/furnace.key ssh/replica_key ssh/replica_key.pub)
    chmod 0600 "$BUNDLE"
    ok "Follower bundle written to $BUNDLE (secret: copy it to the follower, then delete the copy)"
else
    if [ -f "$ETC/tokens.env" ] && [ -f "$ETC/tls/furnace.crt" ] && [ -f "$ETC/ssh/replica_key" ]; then
        ok "Tokens, certificate and replication key kept (not changed)"
    else
        BUNDLE="$(find_bundle)" || die "The follower needs $BUNDLE_NAME from the leader.
  1. Install the leader first. It writes /root/$BUNDLE_NAME.
  2. Copy that file into $HERE on this server, for example:
     on the leader:  sudo scp /root/$BUNDLE_NAME <you>@${CFG_THIS_IP}:~/
  3. Run sudo ./install.sh again. It looks in $HERE and in your home folder."
        UNPACK="$(mktemp -d)"
        (umask 077; tar -xzf "$BUNDLE" -C "$UNPACK")
        for f in tokens.env tls/furnace.crt tls/furnace.key ssh/replica_key ssh/replica_key.pub; do
            [ -f "$UNPACK/$f" ] || { rm -rf "$UNPACK"; die "The bundle $BUNDLE is incomplete ($f is missing)."; }
        done
        cert_names_ok "$UNPACK/tls/furnace.crt" || { rm -rf "$UNPACK"; die "The certificate in the bundle does not name ${CFG_SHARED_IP}, ${CFG_THIS_IP} and ${CFG_PEER_IP}. Check the IPs in both config.env files."; }
        cp "$UNPACK/tokens.env" "$ETC/tokens.env"
        cp "$UNPACK/tls/furnace.crt" "$UNPACK/tls/furnace.key" "$ETC/tls/"
        cp "$UNPACK/ssh/replica_key" "$UNPACK/ssh/replica_key.pub" "$ETC/ssh/"
        rm -rf "$UNPACK"
        fix_secret_modes
        rm -f "$BUNDLE"
        ok "Tokens, certificate and replication key copied from the leader bundle (bundle file deleted)"
    fi
fi
if [ "$CFG_USE_HTTPS" = "yes" ] && ! cert_names_ok "$ETC/tls/furnace.crt"; then
    warn "The certificate does not name all of ${CFG_SHARED_IP}, ${CFG_THIS_IP}, ${CFG_PEER_IP}."
    warn "If the IPs changed: delete $ETC/tls on the leader, run the install there, then on the follower."
    TODO+=("Fix the certificate: it does not name all server IPs.")
fi

# --------------------------------------------------------------------------- config files

say "Writing configuration files"
export R_ROLE="$CFG_ROLE" R_THIS_IP="$CFG_THIS_IP" R_PEER_IP="$CFG_PEER_IP" R_SHARED_IP="$CFG_SHARED_IP"
export R_SHARED_IP_PREFIX="$CFG_SHARED_IP_PREFIX" R_INTERFACE="$CFG_INTERFACE" R_PRIORITY="$PRIORITY"
export R_VRRP_ROUTER_ID="$CFG_VRRP_ROUTER_ID" R_ARCHIVE_AFTER_DAYS="$CFG_ARCHIVE_AFTER_DAYS"
export R_MAX_IPS="$CFG_MAX_IPS" R_MAX_DOMAINS="$CFG_MAX_DOMAINS"

allow_lines() {
    local item out=""
    for item in $(split_list "$1"); do out+="        allow ${item};"$'\n'; done
    printf '%s' "${out%$'\n'}"
}
R_FIREWALL_ALLOW="$(allow_lines "$CFG_FIREWALL_IP")"
R_XSOAR_ALLOW="$(allow_lines "$CFG_XSOAR_IP")"
if [ "$CFG_USE_HTTPS" = "yes" ]; then
    R_LISTEN="    listen 443 ssl;"
    R_TLS="    ssl_certificate $ETC/tls/furnace.crt;
    ssl_certificate_key $ETC/tls/furnace.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:furnace_tls:5m;"
else
    R_LISTEN="    listen 80;"
    R_TLS="    # Plain HTTP (USE_HTTPS=no). Test lab only."
fi
export R_FIREWALL_ALLOW R_XSOAR_ALLOW R_LISTEN R_TLS

render() { python3 "$HERE/deploy/render.py" "$1" "$2"; }

# Remember whether the settings change: only then does the API need a full restart.
ENV_CHANGED=1
NEW_ENV="$(mktemp)"
render "$HERE/deploy/templates/furnace.env.tmpl" "$NEW_ENV"
if cmp -s "$NEW_ENV" "$ETC/furnace.env"; then ENV_CHANGED=0; fi
install -m 0640 -o root -g furnace "$NEW_ENV" "$ETC/furnace.env"
rm -f "$NEW_ENV"

cat > "$ETC/install.env" <<EOF
# Non-secret install settings, used by the Furnace helper scripts. Written by install.sh.
ROLE=$CFG_ROLE
THIS_IP=$CFG_THIS_IP
PEER_IP=$CFG_PEER_IP
SHARED_IP=$CFG_SHARED_IP
INTERFACE=$CFG_INTERFACE
USE_HTTPS=$CFG_USE_HTTPS
EOF
chmod 0644 "$ETC/install.env"
ok "Wrote $ETC/furnace.env and $ETC/install.env"

for script in furnace-notify furnace-health-check furnace-replicate furnace-receive-replica furnace-failover-test; do
    install -m 0755 -o root -g root "$HERE/deploy/scripts/$script" "$OPT/bin/$script"
done
install -m 0755 -o root -g root "$HERE/deploy/scripts/furnace-admin-wrapper" /usr/local/bin/furnace-admin
install -m 0755 -o root -g root "$HERE/uninstall.sh" "$OPT/uninstall.sh"
ok "Helper scripts installed in $OPT/bin, command furnace-admin in /usr/local/bin"

PUBKEY="$(cat "$ETC/ssh/replica_key.pub")"
printf 'restrict,command="%s/bin/furnace-receive-replica" %s\n' "$OPT" "$PUBKEY" > "$SYNC_HOME/.ssh/authorized_keys"
chown furnace-sync:furnace-sync "$SYNC_HOME/.ssh/authorized_keys"
chmod 0600 "$SYNC_HOME/.ssh/authorized_keys"
ok "Replication login prepared (furnace-sync may only receive snapshots)"

render "$HERE/deploy/templates/nginx-furnace.conf.tmpl" /etc/nginx/sites-available/furnace
ln -sf /etc/nginx/sites-available/furnace /etc/nginx/sites-enabled/furnace
if [ -L /etc/nginx/sites-enabled/default ]; then
    rm -f /etc/nginx/sites-enabled/default
    info "Disabled the nginx default site"
fi
nginx -t -q || die "The nginx configuration test failed. See the messages above."
ok "nginx configured (EDL only to $CFG_FIREWALL_IP, API only to $CFG_XSOAR_IP, rate limits on)"

if [ -f /etc/keepalived/keepalived.conf ] && ! grep -q "^# Furnace" /etc/keepalived/keepalived.conf; then
    cp /etc/keepalived/keepalived.conf "/etc/keepalived/keepalived.conf.before-furnace.$(date +%Y%m%d%H%M%S)"
    warn "An existing keepalived.conf was saved with the suffix .before-furnace.*"
fi
# Restart keepalived only when its configuration really changes. A restart releases
# the shared IP, and with nopreempt the other server would keep it.
KEEPALIVED_CHANGED=1
NEW_KA="$(mktemp)"
(umask 077; R_KEEPALIVED_PASSWORD="$CFG_KEEPALIVED_PASSWORD" \
    render "$HERE/deploy/templates/keepalived.conf.tmpl" "$NEW_KA")
if cmp -s "$NEW_KA" /etc/keepalived/keepalived.conf; then
    KEEPALIVED_CHANGED=0
    rm -f "$NEW_KA"
else
    install -m 0600 -o root -g root "$NEW_KA" /etc/keepalived/keepalived.conf
    rm -f "$NEW_KA"
fi
ok "keepalived configured (unicast VRRP to $CFG_PEER_IP, priority $PRIORITY, nopreempt, health check)"

cmp -s "$HERE/deploy/systemd/furnace-api.service" /etc/systemd/system/furnace-api.service || ENV_CHANGED=1
for unit in "$HERE"/deploy/systemd/*; do
    install -m 0644 "$unit" "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload
ok "systemd units installed"

# --------------------------------------------------------------------------- host firewall

if [ "$CFG_HOST_FIREWALL" = "ufw" ]; then
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
    ufw allow from "$CFG_PEER_IP" to any port "$SSH_PORT" proto tcp comment 'furnace replication' >/dev/null
    for src in $(split_list "$CFG_FIREWALL_IP") $(split_list "$CFG_XSOAR_IP") "$CFG_PEER_IP"; do
        ufw allow from "$src" to any port "$WEB_PORT" proto tcp comment 'furnace web' >/dev/null
    done
    if ! ufw allow from "$CFG_PEER_IP" to any proto vrrp comment 'furnace vrrp' >/dev/null 2>&1; then
        # Older ufw without the vrrp keyword: add a raw rule for protocol 112.
        if ! grep -q "furnace-vrrp" /etc/ufw/before.rules; then
            sed -i "/^COMMIT/i -A ufw-before-input -p 112 -s ${CFG_PEER_IP} -j ACCEPT -m comment --comment furnace-vrrp" \
                /etc/ufw/before.rules
        fi
    fi
    ufw --force enable >/dev/null
    ok "Host firewall on: SSH ($SSH_PORT), port $WEB_PORT from firewall/XSOAR/peer, VRRP from $CFG_PEER_IP"
else
    TODO+=("Host firewall was not configured (HOST_FIREWALL=none). Open port $WEB_PORT, SSH and VRRP (protocol 112) yourself.")
fi

# --------------------------------------------------------------------------- start

say "Starting services"
runuser -u furnace -- env FURNACE_CONFIG="$ETC/furnace.env" "$OPT/venv/bin/furnace-admin" rebuild >/dev/null
ok "Database ready and EDL files written (existing data kept)"
systemctl enable --quiet furnace-api.service
if systemctl is-active --quiet furnace-api.service && [ "$ENV_CHANGED" -eq 0 ]; then
    # Upgrade without downtime: new workers load the new code, the main process keeps serving.
    systemctl reload furnace-api.service
    info "furnace-api reloaded with the new program (no downtime)."
else
    systemctl restart furnace-api.service
fi
systemctl enable --quiet nginx
if systemctl is-active --quiet nginx; then systemctl reload nginx; else systemctl start nginx; fi
systemctl enable --quiet --now furnace-replicate.timer furnace-backup.timer furnace-apply-replica.path
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
[ "$CFG_USE_HTTPS" = "yes" ] && CURL+=(--cacert "$ETC/tls/furnace.crt")
BASE="${SCHEME}://127.0.0.1"
TEST_IP=192.0.2.123
HDR=""

pass() { ok "Self-test: $*"; }
fail() { printf '    [FAIL] Self-test: %s\n' "$*"; FAILED=1; TODO+=("Self-test failed: $*"); }

# Allow the test range for a few minutes without restarting the API (a restart could move the shared IP).
set_test_ranges() {
    if [ "$1" = "true" ]; then touch "$STATE_DIR/selftest"; else rm -f "$STATE_DIR/selftest"; fi
}

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

cleanup_selftest() {
    [ -n "$HDR" ] && rm -f "$HDR"
    rm -f "$STATE_DIR/selftest"
}

code_of() { "${CURL[@]}" -o /dev/null -w '%{http_code}' "$@" 2>/dev/null || true; }

say "Self-test"
trap cleanup_selftest EXIT
if wait_health; then pass "/health answers"; else fail "/health does not answer"; fi
for file in blocklist.txt domains.txt; do
    [ "$(code_of "$BASE/edl/$file")" = "200" ] && pass "nginx serves /edl/$file" || fail "nginx does not serve /edl/$file"
done
for path in /edl/archive.csv /archive/archive.csv /furnace.db /edl/../furnace/furnace.db; do
    c="$(code_of --path-as-is "$BASE$path")"
    [ "$c" = "404" ] || [ "$c" = "403" ] || [ "$c" = "400" ] && pass "$path is not served ($c)" || fail "$path answered $c"
done
[ "$(code_of -X POST "$BASE/api/v1/ips")" = "401" ] && pass "the API refuses calls without a token" \
    || fail "the API did not refuse a call without a token"

HDR="$(mktemp)"
chmod 0600 "$HDR"
printf 'Authorization: Bearer %s\n' "$(grep '^FURNACE_BLOCK_TOKEN=' "$ETC/tokens.env" | cut -d= -f2-)" > "$HDR"

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
        set_test_ranges true
        c="$(code_of -X POST -H @"$HDR" -H 'Content-Type: application/json' \
             -d "{\"ip\":\"$TEST_IP\",\"reason\":\"install self-test\",\"added_by\":\"install.sh\"}" "$BASE/api/v1/ips")"
        [ "$c" = "201" ] || [ "$c" = "200" ] && pass "added $TEST_IP through the API ($c)" || fail "could not add $TEST_IP (HTTP $c)"
        served_blocklist_has "$TEST_IP" && pass "$TEST_IP is in the blocklist served by nginx" \
            || fail "$TEST_IP is not in the served blocklist"
        c="$(code_of -X DELETE -H @"$HDR" "$BASE/api/v1/ips/$TEST_IP?removed_by=install.sh")"
        [ "$c" = "200" ] && pass "removed $TEST_IP through the API" || fail "could not remove $TEST_IP (HTTP $c)"
        served_blocklist_has "$TEST_IP" && fail "$TEST_IP is still in the served blocklist" \
            || pass "$TEST_IP is gone from the served blocklist"
        set_test_ranges false
    fi
else
    c="$(code_of -X POST -H @"$HDR" -H 'Content-Type: application/json' -d "{\"ip\":\"$TEST_IP\"}" "$BASE/api/v1/ips")"
    if [ "$(cat "$STATE_DIR/role" 2>/dev/null)" = "leader" ]; then
        warn "This follower owns the shared IP right now (the leader is down or not installed yet)."
    elif [ "$c" = "503" ]; then
        pass "the follower refuses changes while it is not the leader (503)"
    else
        fail "the follower answered $c to a change (expected 503)"
    fi
fi
rm -f "$HDR"; HDR=""
trap - EXIT

# --------------------------------------------------------------------------- summary

say "Summary"
for item in "${DONE[@]}"; do printf '  - %s\n' "$item"; done

if [ "$NEW_TOKENS" -eq 1 ]; then
    if { : > /dev/tty; } 2>/dev/null; then   # a real terminal is attached
        {
            printf '\n  ================== API TOKENS (shown only once) ==================\n'
            printf '  Store them in the XSOAR credentials store. They are NOT in the log.\n'
            sed 's/^/  /' "$ETC/tokens.env"
            printf '  ===================================================================\n\n'
        } > /dev/tty
    else
        printf '\n  New API tokens were created. Read them once with: sudo cat %s\n' "$ETC/tokens.env"
    fi
fi

if [ "$CFG_ROLE" = "leader" ]; then
    TODO+=("Copy /root/$BUNDLE_NAME to the follower ($CFG_PEER_IP) and run the install there. Then delete the copy.")
fi
TODO+=("Allow on the network: firewall -> $CFG_SHARED_IP:$WEB_PORT, XSOAR -> $CFG_SHARED_IP:$WEB_PORT, VRRP and SSH between $CFG_THIS_IP and $CFG_PEER_IP.")
TODO+=("Firewall: import $ETC/tls/furnace.crt as a trusted CA, create a certificate profile, and create two EDL objects (IP List and Domain List). See the README.")
TODO+=("XSOAR: store the tokens and the certificate, then build the HTTP calls. See the API guide in the README.")
TODO+=("After both servers are installed: run sudo $OPT/bin/furnace-failover-test on the leader.")

printf '\n  Still to do by hand:\n'
for item in "${TODO[@]}"; do printf '  - %s\n' "$item"; done
printf '\n  Log file: %s (no secrets in it)\n' "$LOG"

if [ "$FAILED" -ne 0 ]; then
    printf '\nThe install finished, but the self-test FAILED. Check the items above.\n' >&2
    exit 1
fi
printf '\nFurnace %s is installed and the self-test passed.\n' "$VERSION"

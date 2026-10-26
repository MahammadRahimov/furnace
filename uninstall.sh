#!/usr/bin/env bash
# Furnace uninstaller.
#
#   sudo ./uninstall.sh           remove the program and services, KEEP the data
#   sudo ./uninstall.sh --purge   also delete the database, backups, tokens, certificate and users
#   add --yes to skip the question
set -Eeuo pipefail

PURGE=0
YES=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        --yes) YES=1 ;;
        -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo ./uninstall.sh" >&2; exit 1; }

if [ "$YES" -eq 0 ]; then
    if [ "$PURGE" -eq 1 ]; then
        echo "This removes Furnace AND DELETES all data: database, archive, backups, tokens and certificate."
    else
        echo "This removes the Furnace program and services. The data in /var/lib/furnace and /etc/furnace is kept."
    fi
    read -r -p "Type 'yes' to continue: " answer
    [ "$answer" = "yes" ] || { echo "Stopped. Nothing was changed."; exit 1; }
fi

echo "==> Stopping services"
for unit in furnace-api.service furnace-cleanup.timer furnace-cleanup.service furnace-backup.timer \
            furnace-backup.service furnace-replicate.timer furnace-replicate.service \
            furnace-apply-replica.path furnace-apply-replica.service; do
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$unit"
done
systemctl daemon-reload

if [ -f /etc/keepalived/keepalived.conf ] && grep -q "^# Furnace" /etc/keepalived/keepalived.conf; then
    systemctl disable --now keepalived >/dev/null 2>&1 || true
    rm -f /etc/keepalived/keepalived.conf
    echo "    Removed the Furnace keepalived configuration and stopped keepalived (the shared IP is released)."
fi

echo "==> Removing the nginx site"
rm -f /etc/nginx/sites-enabled/furnace /etc/nginx/sites-available/furnace
if nginx -t -q 2>/dev/null; then systemctl reload nginx 2>/dev/null || true; fi

echo "==> Removing host firewall rules added by Furnace"
if command -v ufw >/dev/null 2>&1; then
    while true; do
        num="$( (ufw status numbered 2>/dev/null || true) | grep -E '# furnace' | head -n1 \
                | sed -E 's/^\[ *([0-9]+)\].*/\1/' || true)"
        [ -n "$num" ] || break
        ufw --force delete "$num" >/dev/null
    done
    sed -i '/furnace-vrrp/d' /etc/ufw/before.rules 2>/dev/null || true
    echo "    ufw stays enabled. Check the remaining rules with: sudo ufw status"
fi

echo "==> Removing the program"
rm -rf /opt/furnace /usr/local/bin/furnace-admin /run/furnace-state

if [ "$PURGE" -eq 1 ]; then
    echo "==> Deleting data, secrets and users"
    rm -rf /var/lib/furnace /var/lib/furnace-edl /var/lib/furnace-incoming /var/lib/furnace-sync /etc/furnace
    rm -f /root/furnace-peer-bundle.tar.gz
    userdel furnace-sync >/dev/null 2>&1 || true
    userdel furnace >/dev/null 2>&1 || true
    echo "Furnace is removed. All data was deleted."
else
    echo "Furnace is removed. Data kept in /var/lib/furnace, /var/lib/furnace-edl and /etc/furnace."
    echo "Run ./install.sh again to reinstall with the same data and tokens."
fi

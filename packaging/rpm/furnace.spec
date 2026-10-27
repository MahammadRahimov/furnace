# Furnace RPM for the Red Hat family (RHEL, Rocky, AlmaLinux, Oracle Linux, CentOS Stream 8/9/10),
# Fedora and Amazon Linux 2023. Build it with packaging/build-rpm.sh, not by hand.
#
# build-rpm.sh passes: furnace_version, furnace_stage (the files, already in place).

# Furnace brings its own Python libraries in /usr/lib/furnace/vendor. Leave them as they are:
# no bytecode compiling, no shebang changes, no automatic dependencies from their files.
%global __os_install_post %{nil}
%global __brp_mangle_shebangs %{nil}
%global _binary_payload w9.xzdio
%global use_source_date_epoch_as_buildtime 1
%global clamp_mtime_to_source_date_epoch 1

Name:           furnace
Version:        %{furnace_version}
Release:        1
Summary:        Blocklist server that publishes IP and domain lists for firewalls
License:        Proprietary
URL:            https://github.com/MahammadRahimov/furnace
BuildArch:      noarch
AutoReqProv:    no

# Python 3.9 or newer: the system python3 (RHEL 9/10, Fedora, Amazon Linux), or a newer
# python3.X package where python3 is older (RHEL 8: python3.6).
Requires:       (python3 >= 3.9 or python3.12 or python3.11 or python39)
Requires:       nginx
Requires:       keepalived
Requires:       openssl
Requires:       /usr/bin/curl
Requires:       openssh-server
Requires:       openssh-clients
Requires:       sqlite
Requires:       iproute
Requires:       gzip
Requires:       tar
Requires:       util-linux
Requires:       shadow-utils
Requires:       systemd
Requires:       policycoreutils-python-utils
Recommends:     firewalld

%description
Furnace keeps a blocklist of IPs and domains. A SOAR, a script or a UI adds
and removes entries through an HTTPS API with a token. A firewall downloads
the lists as plain text files (Palo Alto: External Dynamic Lists, EDLs) with
a login. Two servers share one IP address with keepalived, and the leader
copies its database to the follower every minute.

Flask and gunicorn are bundled inside the package, so it works the same on
every supported system.

After the install, run: sudo furnace setup

%install
cp -a %{furnace_stage}/. %{buildroot}/

%files
%dir /usr/lib/furnace
/usr/lib/furnace/furnace
/usr/lib/furnace/vendor
/usr/lib/furnace/templates
/usr/lib/furnace/bin
/usr/lib/furnace/setup.sh
/usr/lib/furnace/render.py
/usr/bin/furnace
/usr/lib/systemd/system/furnace-*
/usr/lib/tmpfiles.d/furnace.conf
%doc /usr/share/doc/furnace

%post
systemd-tmpfiles --create furnace.conf >/dev/null 2>&1 || :
# No compiled .pyc files: Furnace runs with PYTHONDONTWRITEBYTECODE, and files the package
# does not own would stay behind after "dnf remove".
systemctl daemon-reload >/dev/null 2>&1 || :
if [ -f /etc/furnace/setup.conf ]; then
    # Already set up: apply the new version with the saved answers. A problem here must not
    # break dnf, so it is reported and the package stays installed.
    echo "Furnace: updating this server with the saved settings (furnace setup --upgrade)..."
    if ! /usr/lib/furnace/setup.sh --upgrade; then
        echo "" >&2
        echo "WARNING: Furnace was upgraded, but 'furnace setup --upgrade' reported a problem." >&2
        echo "         See /var/log/furnace-setup.log, fix it, then run: sudo furnace setup --upgrade" >&2
    fi
else
    cat <<'EOF'

  Furnace is installed. Now set it up (it asks a few questions):

      sudo furnace setup

EOF
fi

%preun
# $1 = 0: the package is removed (not upgraded). Undo the system changes; keep the data.
if [ "$1" -eq 0 ]; then
    /usr/lib/furnace/bin/furnace-uninstall || :
fi
# Only rm here: dnf may already have removed findutils or python in the same transaction.
for d in /usr/lib/furnace/furnace/__pycache__ /usr/lib/furnace/vendor/*/__pycache__ \
         /usr/lib/furnace/vendor/*/*/__pycache__ /usr/lib/furnace/vendor/*/*/*/__pycache__; do
    rm -rf "$d"
done

%postun
if [ "$1" -eq 0 ]; then
    systemctl daemon-reload >/dev/null 2>&1 || :
    rm -rf /run/furnace-state
    echo "Furnace is removed. The data is kept in /var/lib/furnace* and /etc/furnace."
    echo "Install the package again to use it. To delete all data, see the README (Upgrade and uninstall)."
fi

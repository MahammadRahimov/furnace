"""Furnace in Docker: settings from the environment, secrets, the follower bundle and the config files."""

import shutil
import tarfile
from pathlib import Path

import pytest

from furnace import container
from furnace.container import SettingsError, load_settings

ROOT = Path(__file__).resolve().parents[1]
CARDS = {"lo": [("127.0.0.1", 8)], "eth0": [("192.0.2.11", 24)], "eth1": [("203.0.113.5", 24)]}
LEADER = {"FURNACE_ROLE": "leader", "FURNACE_THIS_IP": "192.0.2.11", "FURNACE_PEER_IP": "192.0.2.12",
          "FURNACE_SHARED_IP": "192.0.2.10"}
FOLLOWER = {**LEADER, "FURNACE_ROLE": "follower", "FURNACE_THIS_IP": "192.0.2.12", "FURNACE_PEER_IP": "192.0.2.11"}
FOLLOWER_CARDS = {"eth0": [("192.0.2.12", 24)]}
needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


def problems(env, cards=CARDS) -> str:
    with pytest.raises(SettingsError) as info:
        load_settings(env, cards)
    return str(info.value)


def test_good_settings_and_defaults():
    s = load_settings(LEADER, CARDS)
    assert (s.role, s.interface, s.priority) == ("leader", "eth0", 150)
    assert (s.shared_ip_prefix, s.vrrp_router_id, s.api_allow_from, s.lists_allow_from) == (24, 51, "any", "any")
    f = load_settings(FOLLOWER, FOLLOWER_CARDS)
    assert f.priority == 100


def test_every_problem_is_reported_at_once():
    text = problems({"FURNACE_THIS_IP": "192.0.2.300", "FURNACE_SHARED_IP_PREFIX": "40",
                     "FURNACE_API_ALLOW_FROM": "192.0.2.0/33", "FURNACE_VRRP_ROUTER_ID": "x"})
    for part in ("FURNACE_ROLE is not set", "FURNACE_THIS_IP '192.0.2.300'", "FURNACE_PEER_IP is not set",
                 "FURNACE_SHARED_IP is not set", "FURNACE_SHARED_IP_PREFIX must be a number from 8 to 32",
                 "FURNACE_API_ALLOW_FROM contains '192.0.2.0/33'", "FURNACE_VRRP_ROUTER_ID"):
        assert part in text


def test_addresses_must_differ():
    assert "PEER_IP must be the other host" in problems({**LEADER, "FURNACE_PEER_IP": "192.0.2.11"})
    assert "SHARED_IP must be a third address" in problems({**LEADER, "FURNACE_SHARED_IP": "192.0.2.12"})
    assert "'boss'" in problems({**LEADER, "FURNACE_ROLE": "boss"})


def test_the_container_needs_the_host_network():
    bridge = {"lo": [("127.0.0.1", 8)], "eth0": [("172.17.0.2", 16)]}
    assert "network_mode: host" in problems(LEADER, bridge)
    assert "does not exist here" in problems({**LEADER, "FURNACE_INTERFACE": "ens192"})
    assert "not set on the network card eth1" in problems({**LEADER, "FURNACE_INTERFACE": "eth1"})
    assert "not a network card name" in problems({**LEADER, "FURNACE_INTERFACE": "192.0.2.11"})


def test_allow_lists():
    s = load_settings({**LEADER, "FURNACE_API_ALLOW_FROM": " 198.51.100.7, 203.0.113.0/24 "}, CARDS)
    assert s.api_allow_from == "198.51.100.7,203.0.113.0/24"
    assert container.allow_lines(s.api_allow_from) == "        allow 198.51.100.7;\n        allow 203.0.113.0/24;"
    assert container.allow_lines("any") == "        allow all;"
    assert "needs 'any' or at least one" in problems({**LEADER, "FURNACE_LISTS_ALLOW_FROM": ","})


@needs_openssl
def test_leader_makes_secrets_and_the_follower_takes_them(tmp_path):
    leader_etc, follower_etc = tmp_path / "leader", tmp_path / "follower"
    notes = container.prepare_secrets(load_settings(LEADER, CARDS), leader_etc, group=None)
    assert any("certificate made" in n for n in notes)
    for name in container.BUNDLE_FILES:
        assert (leader_etc / name).is_file()
    assert len((leader_etc / "secrets/keepalived-password").read_text().strip()) == 8
    assert (leader_etc / "tls/furnace.key").stat().st_mode & 0o777 == 0o600
    assert (leader_etc / "secrets/replica-secret").stat().st_mode & 0o777 == 0o640
    assert container.cert_names(leader_etc / "tls/furnace.crt") >= {"192.0.2.10", "192.0.2.11", "192.0.2.12"}

    # Starting again keeps everything.
    before = {n: (leader_etc / n).read_bytes() for n in container.BUNDLE_FILES}
    container.prepare_secrets(load_settings(LEADER, CARDS), leader_etc, group=None)
    assert before == {n: (leader_etc / n).read_bytes() for n in container.BUNDLE_FILES}

    follower = load_settings(FOLLOWER, FOLLOWER_CARDS)
    with pytest.raises(SettingsError, match="copy furnace-peer-bundle.tar.gz"):
        container.prepare_secrets(follower, follower_etc, group=None)
    follower_etc.mkdir()
    shutil.copy(leader_etc / container.BUNDLE_NAME, follower_etc)
    notes = container.prepare_secrets(follower, follower_etc, group=None)
    assert any("bundle" in n for n in notes)
    assert not (follower_etc / container.BUNDLE_NAME).exists()  # deleted after reading
    assert before == {n: (follower_etc / n).read_bytes() for n in container.BUNDLE_FILES}
    container.prepare_secrets(follower, follower_etc, group=None)  # a restart needs no bundle


@needs_openssl
def test_the_certificate_must_name_the_ips(tmp_path):
    container.prepare_secrets(load_settings(LEADER, CARDS), tmp_path, group=None)
    moved = {**LEADER, "FURNACE_SHARED_IP": "192.0.2.20"}
    with pytest.raises(SettingsError, match="does not name 192.0.2.20"):
        container.prepare_secrets(load_settings(moved, CARDS), tmp_path, group=None)


def test_a_bad_bundle_is_refused(tmp_path):
    bundle = tmp_path / container.BUNDLE_NAME
    bundle.write_text("not a tar file")
    with pytest.raises(SettingsError, match="cannot be read"):
        container.open_bundle(bundle, tmp_path / "etc")
    (tmp_path / "x").write_text("x")
    with tarfile.open(bundle, "w:gz") as tar:
        tar.add(tmp_path / "x", arcname="tls/furnace.crt")
        tar.add(tmp_path / "x", arcname="../../evil")
    with pytest.raises(SettingsError, match="has no tls/furnace.key"):
        container.open_bundle(bundle, tmp_path / "etc")
    assert not (tmp_path.parent / "evil").exists()


def test_the_templates_get_every_value(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets/keepalived-password").write_text("Ab3dEf7h\n")
    values = container.template_values(load_settings({**LEADER, "FURNACE_LISTS_ALLOW_FROM": "198.51.100.1"}, CARDS),
                                       tmp_path)
    nginx = container.render(ROOT / "deploy/templates/nginx-furnace.conf.tmpl", values)
    assert "location = /replica/snapshot" in nginx and "allow 192.0.2.12;" in nginx
    assert "allow 198.51.100.1;" in nginx and "listen 443 ssl;" in nginx and "{{" not in nginx
    keepalived = container.render(ROOT / "deploy/templates/keepalived.conf.tmpl", values)
    assert "priority 150" in keepalived and "auth_pass Ab3dEf7h" in keepalived
    assert "192.0.2.10/24 dev eth0" in keepalived and "{{" not in keepalived
    with pytest.raises(SettingsError, match="no value for"):
        container.render(ROOT / "deploy/templates/keepalived.conf.tmpl", {})


def test_daily_jobs_run_once_per_day_and_after_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(container, "DATA", tmp_path)
    sup = container.Supervisor.__new__(container.Supervisor)
    assert sup.due_daily("backup", 0, 0) is True
    assert sup.due_daily("backup", 0, 0) is False  # already today
    assert sup.due_daily("cleanup", 23, 59) in (False, True)  # only after 23:59


def test_replication_every_20_seconds_like_the_servers():
    timer = Path(__file__).resolve().parents[1] / "deploy/systemd/furnace-replicate.timer"
    assert container.REPLICATE_EVERY == 20
    assert "OnUnitActiveSec=20s" in timer.read_text(encoding="utf-8")


def test_the_refuse_file_is_made_once_and_never_changed(tmp_path):
    note = container.write_refuse_file(tmp_path)
    path = tmp_path / "whitelist-refuse.txt"
    assert "whitelist-refuse.txt" in note and path.read_text(encoding="utf-8") == container.REFUSE_TEXT
    path.write_text("dropbox.com\n", encoding="utf-8")
    assert container.write_refuse_file(tmp_path) == ""
    assert path.read_text(encoding="utf-8") == "dropbox.com\n"


def test_the_refuse_text_is_the_same_as_setup():
    setup = (Path(__file__).resolve().parents[1] / "deploy/setup.sh").read_text(encoding="utf-8")
    assert container.REFUSE_TEXT in setup


def test_the_image_has_the_public_suffix_list():
    dockerfile = (Path(__file__).resolve().parents[1] / "docker/Dockerfile").read_text(encoding="utf-8")
    assert "furnace/public_suffix_list.dat" in dockerfile


def test_replication_log_only_changes_and_a_reminder_while_down():
    s = container.ContainerSettings(role="leader", this_ip="192.0.2.11", peer_ip="192.0.2.12", shared_ip="192.0.2.10")
    sup = container.Supervisor(s)
    note = sup.note_replication
    assert note(True, "sent (1 IPs)", 0) == "replication: sent (1 IPs)"
    assert note(True, "sent (2 IPs)", 20) == ""                       # every 20 s: nothing new
    down = "cannot reach the other server at 192.0.2.12: No route to host"
    assert note(False, down, 40) == f"replication FAILED: {down}"
    lines = [note(False, down, 40 + 20 * i) for i in range(1, 29)]     # the other host stays down 9+ minutes
    assert lines == [""] * 28
    reminder = note(False, down, 40 + container.REPLICA_REMIND)
    assert reminder.startswith("replication still FAILED (30 tries in a row)") and down in reminder
    assert note(False, "the other server refused the secret", 660).startswith("replication still FAILED")  # a new reason
    assert note(True, "sent (3 IPs)", 680) == "replication works again after 31 failed tries: sent (3 IPs)"
    assert note(True, "sent (3 IPs)", 700) == ""
    assert sup.replica_state == "ok sent (3 IPs)"


# --------------------------------------------------------------------------- the IP names of 0.10.0

BOTH = {"FURNACE_LEADER_IP": "192.0.2.11", "FURNACE_FOLLOWER_IP": "192.0.2.12", "FURNACE_SHARED_IP": "192.0.2.10"}


def test_the_same_settings_on_both_hosts_find_the_role():
    s = load_settings(BOTH, CARDS)
    assert (s.role, s.this_ip, s.peer_ip, s.interface, s.priority) == ("leader", "192.0.2.11", "192.0.2.12", "eth0", 150)
    assert s.notes == []
    f = load_settings(BOTH, FOLLOWER_CARDS)
    assert (f.role, f.this_ip, f.peer_ip, f.priority) == ("follower", "192.0.2.12", "192.0.2.11", 100)


def test_new_names_problems():
    assert "has neither 192.0.2.11 (FURNACE_LEADER_IP) nor 192.0.2.12" in problems(BOTH, {"eth0": [("198.51.100.5", 24)]})
    both_here = {"eth0": [("192.0.2.11", 24), ("192.0.2.12", 24)]}
    assert "Add FURNACE_ROLE=leader or FURNACE_ROLE=follower" in problems(BOTH, both_here)
    assert load_settings({**BOTH, "FURNACE_ROLE": "follower"}, both_here).this_ip == "192.0.2.12"
    assert "FURNACE_ROLE is follower, but this host has the leader's IP (192.0.2.11)" in problems(
        {**BOTH, "FURNACE_ROLE": "follower"})
    assert "must be the two different hosts" in problems({**BOTH, "FURNACE_FOLLOWER_IP": "192.0.2.11"})
    assert "SHARED_IP must be a third address" in problems({**BOTH, "FURNACE_SHARED_IP": "192.0.2.12"})
    assert "FURNACE_FOLLOWER_IP '192.0.2.300'" in problems({**BOTH, "FURNACE_FOLLOWER_IP": "192.0.2.300"})
    text = problems({})
    for part in ("FURNACE_LEADER_IP is not set", "FURNACE_FOLLOWER_IP is not set", "FURNACE_SHARED_IP is not set"):
        assert part in text
    assert "not both" in problems({**BOTH, "FURNACE_THIS_IP": "192.0.2.11"})
    assert "FURNACE_LEADER_IP 192.0.2.11 is not set on the network card eth1" in problems(
        {**BOTH, "FURNACE_ROLE": "leader", "FURNACE_INTERFACE": "eth1"})


def test_the_old_names_still_work_with_a_note():
    s = load_settings(LEADER, CARDS)
    assert (s.role, s.this_ip, s.peer_ip) == ("leader", "192.0.2.11", "192.0.2.12")
    assert any("old names" in n for n in s.notes)


# --------------------------------------------------------------------------- the join code (Docker)


def test_a_bad_join_code_is_reported():
    assert "FURNACE_JOIN_CODE: This is not a Furnace join code" in problems({**BOTH, "FURNACE_JOIN_CODE": "hello"},
                                                                            FOLLOWER_CARDS)
    from furnace.join import format_code
    other = format_code(b"\x01" * 8, b"\x02" * 8, "192.0.2.99")
    assert "was made by 192.0.2.99, but the leader is 192.0.2.11" in problems(
        {**BOTH, "FURNACE_JOIN_CODE": other}, FOLLOWER_CARDS)


@needs_openssl
def test_the_follower_fetches_the_secrets_with_a_join_code(tmp_path, monkeypatch):
    from furnace import join
    leader_etc, follower_etc = tmp_path / "leader", tmp_path / "follower"
    container.prepare_secrets(load_settings(BOTH, CARDS), leader_etc, group=None)
    bundle = (leader_etc / container.BUNDLE_NAME).read_bytes()
    code = join.make(bundle, join.cert_der(leader_etc / "tls/furnace.crt"), "192.0.2.11", tmp_path / "join")
    asked = []

    def fake_fetch(given, leader_ip):  # the network part is tested in test_join.py
        asked.append((given, leader_ip))
        return bundle
    monkeypatch.setattr(join, "fetch", fake_fetch)
    s = load_settings({**BOTH, "FURNACE_JOIN_CODE": code}, FOLLOWER_CARDS)
    notes = container.prepare_secrets(s, follower_etc, group=None)
    assert asked == [(code, "192.0.2.11")]
    assert any("fetched from the leader 192.0.2.11 with the join code" in n for n in notes)
    for name in container.BUNDLE_FILES:
        assert (follower_etc / name).read_bytes() == (leader_etc / name).read_bytes()
    assert not (follower_etc / container.BUNDLE_NAME).exists()

    def failing(given, leader_ip):
        raise join.JoinError("The leader said: The join code is wrong.")
    monkeypatch.setattr(join, "fetch", failing)
    with pytest.raises(SettingsError, match="The join code did not work: The leader said"):
        container.prepare_secrets(s, tmp_path / "other", group=None)


def test_a_follower_without_anything_says_both_ways():
    with pytest.raises(SettingsError) as info:
        container.prepare_secrets(load_settings(BOTH, FOLLOWER_CARDS), Path("/nonexistent-furnace-test"), group=None)
    assert "furnace join-code" in str(info.value) and "furnace-peer-bundle.tar.gz" in str(info.value)

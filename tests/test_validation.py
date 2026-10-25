import pytest

from furnace.validation import (Rejected, normalize_domain, validate_ip,
                                validate_whitelist_cidr, clean_text)


def code_of(func, *args, **kwargs):
    with pytest.raises(Rejected) as info:
        func(*args, **kwargs)
    return info.value.code


@pytest.mark.parametrize("value, code", [
    ("", "missing_value"),
    (None, "missing_value"),
    (12345, "missing_value"),
    ("not-an-ip", "invalid_ip"),
    ("hello", "invalid_ip"),
    ("192.0.2.0/24", "range_not_allowed"),
    ("192.0.2.1-192.0.2.9", "range_not_allowed"),
    ("256.1.1.1", "invalid_ip"),
    ("010.1.1.1", "invalid_ip"),
    ("10.1.2.3", "private_ip"),
    ("172.16.0.1", "private_ip"),
    ("192.168.1.1", "private_ip"),
    ("127.0.0.1", "loopback_ip"),
    ("::1", "loopback_ip"),
    ("169.254.1.1", "link_local_ip"),
    ("fe80::1", "link_local_ip"),
    ("100.64.0.1", "shared_address_space"),
    ("100.127.255.254", "shared_address_space"),
    ("224.0.0.1", "multicast_ip"),
    ("ff02::1", "multicast_ip"),
    ("240.0.0.1", "reserved_ip"),
    ("0.0.0.0", "unspecified_ip"),
    ("::", "unspecified_ip"),
    ("fc00::1", "private_ip"),
    ("2001:db8::1", "private_ip"),
    ("::ffff:198.51.100.1", "ipv4_mapped_not_allowed"),
])
def test_bad_ips_are_refused(value, code):
    assert code_of(validate_ip, value, allow_test_ranges=True) == code


def test_test_ranges_need_the_setting():
    for ip in ("192.0.2.10", "198.51.100.20", "203.0.113.30"):
        assert code_of(validate_ip, ip) == "documentation_range"
        assert str(validate_ip(ip, allow_test_ranges=True)) == ip


def test_ip_is_canonical():
    assert str(validate_ip("  203.0.113.5 ", allow_test_ranges=True)) == "203.0.113.5"


@pytest.mark.parametrize("value, expected", [
    ("Example.COM", "example.com"),
    ("sub.example.com.", "sub.example.com"),
    (" bad-site.example.org ", "bad-site.example.org"),
    ("xn--bcher-kva.example", "xn--bcher-kva.example"),
    ("bücher.example", "xn--bcher-kva.example"),
])
def test_good_domains(value, expected):
    assert normalize_domain(value) == expected


@pytest.mark.parametrize("value, code", [
    ("", "missing_value"),
    ("*.example.com", "wildcard_not_allowed"),
    ("http://example.com", "url_not_allowed"),
    ("example.com/path", "url_not_allowed"),
    ("user@example.com", "url_not_allowed"),
    ("192.0.2.1", "ip_not_allowed"),
    ("2001:db8::1", "ip_not_allowed"),
    ("localhost", "invalid_domain"),
    ("-bad.example.com", "invalid_domain"),
    ("bad-.example.com", "invalid_domain"),
    ("exa mple.com", "invalid_domain"),
    ("example.com:443", "invalid_domain"),
    ("example..com", "invalid_domain"),
    ("example.123", "invalid_domain"),
    ("a" * 64 + ".example.com", "invalid_domain"),
    ("under_score.example.com", "invalid_domain"),
])
def test_bad_domains(value, code):
    assert code_of(normalize_domain, value) == code


def test_whitelist_cidr():
    assert str(validate_whitelist_cidr("198.51.100.0/24")) == "198.51.100.0/24"
    assert str(validate_whitelist_cidr("198.51.100.7")) == "198.51.100.7/32"
    assert str(validate_whitelist_cidr("198.51.100.7/24")) == "198.51.100.0/24"
    assert str(validate_whitelist_cidr("10.0.0.0/8")) == "10.0.0.0/8"
    assert code_of(validate_whitelist_cidr, "10.0.0.0/7") == "range_too_wide"
    assert code_of(validate_whitelist_cidr, "2001:db8::/15") == "range_too_wide"
    assert str(validate_whitelist_cidr("2001:db8::/32")) == "2001:db8::/32"
    assert str(validate_whitelist_cidr("2001:db8::/16")) == "2001::/16"
    assert code_of(validate_whitelist_cidr, "nonsense") == "invalid_cidr"


def test_clean_text():
    assert clean_text(None, "reason", 10) == ""
    assert clean_text("  two   words ", "reason", 20) == "two words"
    assert clean_text(4521, "incident_id", 20) == "4521"
    assert code_of(clean_text, "x" * 11, "reason", 10) == "field_too_long"
    assert code_of(clean_text, ["list"], "reason", 10) == "field_too_long"

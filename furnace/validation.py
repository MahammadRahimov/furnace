"""Checks for IPs, domains and whitelist ranges.

Every check returns a clean value or raises Rejected with a short
reason code. The reason codes are part of the API.
"""

from __future__ import annotations

import ipaddress
import re

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

TEST_RANGES = tuple(ipaddress.ip_network(n) for n in
                    ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
SHARED_RANGE = ipaddress.ip_network("100.64.0.0/10")
MIN_WHITELIST_PREFIX = {4: 8, 6: 16}

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

REASON_TEXT = {
    "missing_value": "No IP or domain was given.",
    "invalid_ip": "This is not a valid IP address.",
    "range_not_allowed": "Ranges are not allowed. Send one IP address.",
    "private_ip": "This is a private IP address. Only public IPs can be blocked.",
    "loopback_ip": "This is a loopback IP address. It cannot be blocked.",
    "link_local_ip": "This is a link-local IP address. It cannot be blocked.",
    "shared_address_space": "This IP is in the shared range 100.64.0.0/10. It cannot be blocked.",
    "multicast_ip": "This is a multicast IP address. It cannot be blocked.",
    "reserved_ip": "This is a reserved IP address. It cannot be blocked.",
    "unspecified_ip": "This is the empty address. It cannot be blocked.",
    "documentation_range": "This IP is in a documentation range. It is only allowed in test mode.",
    "ipv4_mapped_not_allowed": "Send the plain IPv4 address, not the IPv6-mapped form.",
    "not_public": "This IP is not a public address. It cannot be blocked.",
    "whitelisted": "This entry is on the whitelist. It cannot be blocked.",
    "limit_reached": "The list is full. The firewall cannot take more entries.",
    "invalid_domain": "This is not a valid domain name.",
    "wildcard_not_allowed": "Wildcards are not allowed. Send one exact domain.",
    "url_not_allowed": "This looks like a URL. Send only the domain name.",
    "ip_not_allowed": "This is an IP address. Use the IP endpoint instead.",
    "invalid_cidr": "This is not a valid IP address or range.",
    "range_too_wide": "This range is too wide. The limit is /8 for IPv4 and /16 for IPv6.",
    "field_too_long": "A field is too long.",
    "not_leader": "This server is the standby. Send changes to the shared IP.",
}


class Rejected(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.message = detail or REASON_TEXT.get(code, code)


def is_test_ip(ip: IPAddress) -> bool:
    return ip.version == 4 and any(ip in net for net in TEST_RANGES)


def _looks_like_dash_range(text: str) -> bool:
    parts = text.split("-")
    if len(parts) != 2:
        return False
    try:
        ipaddress.ip_address(parts[0].strip())
        ipaddress.ip_address(parts[1].strip())
        return True
    except ValueError:
        return False


def validate_ip(value: object, allow_test_ranges: bool = False) -> IPAddress:
    """Accept one public IP address. Return it in canonical form."""
    if not isinstance(value, str) or not value.strip():
        raise Rejected("missing_value")
    text = value.strip()
    if "/" in text or _looks_like_dash_range(text):
        raise Rejected("range_not_allowed")
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        raise Rejected("invalid_ip") from None

    if ip.version == 6 and ip.ipv4_mapped is not None:
        raise Rejected("ipv4_mapped_not_allowed")
    if is_test_ip(ip):
        if allow_test_ranges:
            return ip
        raise Rejected("documentation_range")
    if ip.is_unspecified:
        raise Rejected("unspecified_ip")
    if ip.is_loopback:
        raise Rejected("loopback_ip")
    if ip.is_link_local:
        raise Rejected("link_local_ip")
    if ip.is_multicast:
        raise Rejected("multicast_ip")
    if ip.version == 4 and ip in SHARED_RANGE:
        raise Rejected("shared_address_space")
    if ip.is_reserved:
        raise Rejected("reserved_ip")
    if ip.is_private:
        raise Rejected("private_ip")
    if not ip.is_global:
        raise Rejected("not_public")
    return ip


def normalize_domain(value: object) -> str:
    """Accept one exact domain name. Return it lowercased (punycode for non-ASCII)."""
    if not isinstance(value, str) or not value.strip():
        raise Rejected("missing_value")
    text = value.strip().lower()
    if "://" in text or "/" in text or "?" in text or "#" in text or "@" in text:
        raise Rejected("url_not_allowed")
    if "*" in text:
        raise Rejected("wildcard_not_allowed")
    if text.endswith("."):
        text = text[:-1]
    try:
        ipaddress.ip_address(text.strip("[]"))
        raise Rejected("ip_not_allowed")
    except ValueError:
        pass
    if not text.isascii():
        try:
            text = text.encode("idna").decode("ascii")
        except UnicodeError:
            raise Rejected("invalid_domain") from None
    if len(text) > 253:
        raise Rejected("invalid_domain")
    labels = text.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise Rejected("invalid_domain")
    if labels[-1].isdigit():
        raise Rejected("invalid_domain")
    return text


def validate_whitelist_cidr(value: object) -> IPNetwork:
    """Accept an IP or a CIDR range for the whitelist. Host bits are cleared."""
    if not isinstance(value, str) or not value.strip():
        raise Rejected("missing_value")
    text = value.strip()
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        raise Rejected("invalid_cidr") from None
    if net.prefixlen < MIN_WHITELIST_PREFIX[net.version]:
        raise Rejected("range_too_wide")
    return net


def clean_text(value: object, field: str, limit: int) -> str:
    """Optional free-text field (reason, added_by, incident_id)."""
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise Rejected("field_too_long", f"The field {field} must be text.")
    text = " ".join(value.split())
    if len(text) > limit:
        raise Rejected("field_too_long", f"The field {field} is longer than {limit} characters.")
    return text


def ip_sort_key(text: str) -> tuple[int, int]:
    ip = ipaddress.ip_address(text)
    return (ip.version, int(ip))


def range_hex(net: IPNetwork) -> tuple[str, str]:
    return to_hex(net.network_address), to_hex(net.broadcast_address)


def to_hex(ip: IPAddress) -> str:
    return format(int(ip), "032x")

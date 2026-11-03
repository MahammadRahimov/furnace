"""Checks for IPs, domains and whitelist ranges.

Every check returns a clean value or raises Rejected with a short
reason code. The reason codes are part of the API.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Union

from furnace import psl

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]  # not "|": Python 3.9 is supported
IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

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
    "invalid_permanent": "The field permanent must be true or false.",
    "not_leader": "This server is the standby. Send changes to the shared IP.",
    "invalid_url": "This is not a valid URL. Send a host name with an optional path, for example bad.example.com/login.",
    "scheme_not_allowed": "Only http:// and https:// addresses can be blocked.",
    "login_not_allowed": "A URL with a user name or password (user@host) cannot be blocked.",
    "url_too_long": "The URL is longer than 255 characters (the firewall's limit).",
    "wildcard_partial": "A wildcard (* or ^) must be a whole part between dots or slashes: *.example.com, "
                        "not *example.com.",
    "wildcard_too_wide": "This wildcard is too wide: the name before the end must be written out, "
                         "for example *.example.com/, not *.com/ or example.*/.",
    "ipv6_url_not_allowed": "URLs with an IPv6 address are not supported. Block the IP in the IP list.",
    "whitelist_not_allowed": "This name may not be whitelisted: it is a shared platform or a public name.",
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


# ---------------------------------------------------------------------- URLs
#
# A URL entry is what the firewall's URL list takes: host[:port] and an optional path, with no
# http:// or https:// (one entry covers both). A wildcard is a whole part (token): * = one or
# more parts, ^ = exactly one part. Parts are separated by . / ? & = ; +
# Stored form: lower case, no scheme, no #fragment, and a host alone ends with /, so
# "bad.example.com" never also matches "bad.example.com.evil.test".

URL_MAX = 255
WILDCARDS = ("*", "^")
_SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*)://")
_TOKEN_SPLIT = re.compile(r"[./?&=;+]")
_PORT = re.compile(r"^[0-9]{1,5}$")
# Second-level names under a country code that are public, like co.uk or com.az. With a wildcard,
# *.co.uk/ would block every company in that country, so one more part must be written out.
_PUBLIC_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "edu", "ac", "gob", "or", "ne", "go", "mil", "nic"}


@dataclass(frozen=True)
class URLEntry:
    url: str    # the stored and published form, for example *.bad.example.com/login
    host: str   # the host part without the port: a name (may have wildcards) or an IPv4 address

    @property
    def is_ip(self) -> bool:
        try:
            ipaddress.ip_address(self.host)
            return True
        except ValueError:
            return False


def _check_tokens(text: str) -> None:
    for token in _TOKEN_SPLIT.split(text):
        if any(w in token for w in WILDCARDS) and token not in WILDCARDS:
            raise Rejected("wildcard_partial")


def normalize_url(value: object, allow_test_ranges: bool = False) -> URLEntry:
    """Accept one URL for the URL list. http:// and https:// are removed (user decision 2026-11-02)."""
    if not isinstance(value, str) or not value.strip():
        raise Rejected("missing_value")
    text = value.strip()
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise Rejected("invalid_url")
    text = text.lower()
    scheme = _SCHEME.match(text)
    if scheme:
        if scheme.group(1) not in ("http", "https"):
            raise Rejected("scheme_not_allowed")
        text = text[scheme.end():]
    text = text.split("#", 1)[0]
    cut = min((i for i in (text.find("/"), text.find("?")) if i >= 0), default=len(text))
    authority, rest = text[:cut], text[cut:]
    if rest.startswith("?"):
        rest = "/" + rest
    if "@" in authority:
        raise Rejected("login_not_allowed")
    if authority.startswith("["):
        raise Rejected("ipv6_url_not_allowed")
    host, _, port = authority.partition(":")
    if port and (not _PORT.match(port) or not 0 < int(port) < 65536):
        raise Rejected("invalid_url")
    if host.endswith("."):
        host = host[:-1]
    if not host:
        raise Rejected("invalid_url")
    if ":" in host:
        raise Rejected("ipv6_url_not_allowed")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        host = str(validate_ip(host, allow_test_ranges))
    else:
        host = _url_host(host)
    if not rest.isascii():
        raise Rejected("invalid_url", "The path has letters that are not ASCII. Send it percent-encoded (%C3%A7).")
    _check_tokens(rest)
    url = host + (f":{int(port)}" if port else "") + (rest or "/")
    if len(url) > URL_MAX:
        raise Rejected("url_too_long")
    return URLEntry(url, host)


def _url_host(host: str) -> str:
    labels = host.split(".")
    out = []
    for label in labels:
        if label in WILDCARDS:
            out.append(label)
            continue
        if any(w in label for w in WILDCARDS):
            raise Rejected("wildcard_partial")
        if not label.isascii():
            try:
                label = label.encode("idna").decode("ascii")
            except UnicodeError:
                raise Rejected("invalid_url") from None
        if not _LABEL.match(label):
            raise Rejected("invalid_url")
        out.append(label)
    if len(out) < 2 or out[-1] in WILDCARDS or out[-1].isdigit():
        raise Rejected("wildcard_too_wide" if out[-1] in WILDCARDS else "invalid_url")
    if any(label in WILDCARDS for label in out):
        written = 3 if out[-2] in _PUBLIC_SECOND_LEVEL and len(out[-1]) == 2 else 2
        if len(out) <= written or any(label in WILDCARDS for label in out[-written:]):
            raise Rejected("wildcard_too_wide")
        _check_shared_platform(out)
    return ".".join(out)


def _check_shared_platform(labels: list) -> None:
    """A wildcard may not cover a whole shared platform or public name (user decision 2026-11-03).

    The written-out name after the last wildcard is checked against the Public Suffix List, the same
    list the whitelist uses: *.github.io/ would block every GitHub Pages site, *.amazonaws.com/ covers
    s3.amazonaws.com and the other Amazon platforms. One name below them is fine: *.bad.github.io/.
    """
    last = max(i for i, label in enumerate(labels) if label in WILDCARDS)
    tail = ".".join(labels[last + 1:])
    rules = psl.load()
    if psl.public_suffix(tail, rules):
        raise Rejected("wildcard_too_wide",
                       f"This wildcard is too wide: {tail} is a shared platform or a public name, so it would "
                       f"block every site under it. Write out one site, for example *.bad.{tail}/.")
    under = rules.parents.get(tail)
    if under:
        raise Rejected("wildcard_too_wide",
                       f"This wildcard is too wide: {tail} contains shared platforms (sites of many owners), "
                       f"so it would block too much. Write out one site instead.")


def host_touches_domain(host: str, domain: str) -> bool:
    """True when a URL host (with wildcards) can match the domain or any of its subdomains.

    This is the whitelist rule for URLs (user decision 2026-11-02): x.example.com on the whitelist
    protects x.example.com and everything under it, so x.example.com/, www.x.example.com/a and
    *.x.example.com/ are refused, and a wildcard such as *.example.com/ too (it covers x.example.com).
    """
    pattern, target = host.split("."), domain.split(".")

    def match(i: int, j: int) -> bool:  # pattern[:i] against target[:j], from the right
        if j == 0:
            return True        # the domain is covered; the rest of the pattern is the subdomain part
        if i == 0:
            return False       # the pattern is a parent of the domain: it does not touch it
        part = pattern[i - 1]
        if part == "*":
            return True        # * takes the rest of the domain and more
        if part == "^" or part == target[j - 1]:
            return match(i - 1, j - 1)
        return False

    return match(len(pattern), len(target))


def domain_key(name: str) -> str:
    """The last two parts of a name: a whitelist domain can touch a URL host only if they share them."""
    return ".".join(name.split(".")[-2:])


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
    """Optional free-text field (reason, added_by, removed_by)."""
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

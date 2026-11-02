"""Which domains may not go on the whitelist (user decision 2026-11-02).

The whitelist is for one company's or one person's sites, not for platforms where anyone can make a site.
Furnace uses the Public Suffix List (publicsuffix.org, the list browsers use; a copy comes with Furnace in
public_suffix_list.dat, MPL 2.0) and an optional local file of the admin's own names.

A whitelist entry is refused when
  - it is a public suffix itself: a public name (com, co.uk) or a shared platform (github.io, blogspot.com),
  - or it would cover one: a shared platform lies under it (amazonaws.com covers s3.amazonaws.com),
  - or it is in the local refuse file, or would cover a name in it (google.com covers drive.google.com).
One name below a public suffix is fine: company.github.io, example.co.uk.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PSL_FILE = Path(__file__).with_name("public_suffix_list.dat")


@dataclass(frozen=True)
class Rules:
    exact: frozenset          # "co.uk", "github.io"
    wildcard: frozenset       # "*.ck" stored as "ck"
    exception: frozenset      # "!www.ck" stored as "www.ck"
    private: frozenset        # the rules of the PRIVATE section (shared platforms), in the same forms
    parents: dict             # every name that has a rule under it -> one such rule (for the message)


@lru_cache(maxsize=4)
def load(path: Path = PSL_FILE) -> Rules:
    exact, wildcard, exception, private = set(), set(), set(), set()
    parents: dict[str, str] = {}
    section_private = False
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if "===BEGIN PRIVATE DOMAINS===" in line:
                section_private = True
            if not line or line.startswith("//"):
                continue
            rule = line.split()[0].lower()
            try:
                rule = ".".join(label if label in ("*",) or label.startswith("!") or label.isascii()
                                else label.encode("idna").decode("ascii") for label in rule.split("."))
            except UnicodeError:
                continue
            if rule.startswith("!"):
                exception.add(rule[1:])
                continue
            if rule.startswith("*."):
                rule = rule[2:]
                wildcard.add(rule)
            else:
                exact.add(rule)
            if section_private:
                private.add(rule)
            labels = rule.split(".")
            for i in range(1, len(labels) - 1):  # parents with at least two labels
                parents.setdefault(".".join(labels[i:]), rule)
    return Rules(frozenset(exact), frozenset(wildcard), frozenset(exception), frozenset(private), parents)


def public_suffix(domain: str, rules: Rules | None = None) -> str | None:
    """The public suffix the domain is, or None when it is a normal name (one or more labels below one)."""
    rules = rules or load()
    if domain in rules.exception:
        return None
    if domain in rules.exact:
        return domain
    labels = domain.split(".")
    if len(labels) > 1 and ".".join(labels[1:]) in rules.wildcard:
        return "*." + ".".join(labels[1:])
    return None


def read_local(path: Path | None) -> set[str]:
    """The admin's own names that may never be whitelisted: one per line, # comments. A missing file is empty."""
    if path is None:
        return set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    names = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().lower().rstrip(".")
        if line:
            names.add(line)
    return names


def refusal(domain: str, local_file: Path | None = None, rules: Rules | None = None) -> str | None:
    """Why this domain may not be whitelisted, or None when it may."""
    rules = rules or load()
    suffix = public_suffix(domain, rules)
    if suffix:
        if suffix.lstrip("*.") in rules.private or domain in rules.private:
            return (f"{domain} is a shared platform: anyone can make a site under it. "
                    f"Whitelist the exact site instead, for example company.{domain}.")
        return (f"{domain} is a public name (like com or co.uk): it is not one company's site. "
                f"Whitelist the exact domain instead, for example company.{domain}.")
    under = rules.parents.get(domain)
    if under:
        return (f"{domain} covers the shared platform {under}: anyone can make a site under it. "
                f"Whitelist the exact site instead.")
    local = read_local(local_file)
    if domain in local:
        return f"{domain} is on the local list of names that may not be whitelisted ({local_file})."
    covered = sorted(n for n in local if n.endswith("." + domain))
    if covered:
        return (f"{domain} covers {covered[0]}, which is on the local list of names that may not be "
                f"whitelisted ({local_file}). Whitelist the exact site instead.")
    return None

"""Profile URL normalisation. The public identifier is the cache key, so every
accepted spelling of a URL has to collapse to exactly one id."""

import re
from urllib.parse import unquote, urlparse

# /in/<id> optionally followed by locale prefixes, detail sub-pages, etc.
_PROFILE_RE = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?in/([^/?#]+)", re.I)
_VALID_ID = re.compile(r"^[\w\-%.À-￿]{2,150}$", re.U)

_REJECT_PATHS = (
    "company",
    "school",
    "posts",
    "feed",
    "jobs",
    "groups",
    "showcase",
    "pub/dir",
    "learning",
    "events",
    "newsletters",
)


class InvalidProfileURL(ValueError):
    pass


def public_id_from_url(value: str) -> str:
    """linkedin.com/in/foo-bar/ -> 'foo-bar'. Raises InvalidProfileURL otherwise."""
    if not value or not value.strip():
        raise InvalidProfileURL("empty url")
    raw = value.strip()

    # a bare vanity slug is a convenience, not a URL
    if "/" not in raw and "." not in raw and " " not in raw:
        candidate = raw
    else:
        if not re.match(r"^[a-z]+://", raw, re.I):
            raw = "https://" + raw
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if not host.endswith("linkedin.com"):
            raise InvalidProfileURL(f"not a linkedin.com url: {host or value!r}")

        path = parsed.path or "/"
        for bad in _REJECT_PATHS:
            if re.match(rf"^/(?:[a-z]{{2}}(?:-[a-z]{{2}})?/)?{re.escape(bad)}(/|$)", path, re.I):
                raise InvalidProfileURL(f"not a personal profile url: /{bad}")

        m = _PROFILE_RE.match(path)
        if not m:
            raise InvalidProfileURL("url is not a /in/<profile> link")
        candidate = m.group(1)

    candidate = unquote(candidate).strip()
    if not _VALID_ID.match(candidate):
        raise InvalidProfileURL(f"implausible profile id: {candidate!r}")
    return candidate


def canonical_url(public_id: str) -> str:
    return f"https://www.linkedin.com/in/{public_id}"

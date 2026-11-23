"""Principal identities: the people and workloads behind usage logs, IAM allow policies and IAM deny policies.

A person is either
- a Google Account (Cloud Identity or Google Workspace user), logged as ``user:alice@example.com`` or the bare
  e-mail, written ``user:EMAIL`` in allow policies and ``principal://goog/subject/EMAIL`` in deny policies; or
- a single identity in a workforce identity pool (an external identity provider through Workforce Identity
  Federation), written
  ``principal://iam.googleapis.com/locations/global/workforcePools/POOL_ID/subject/SUBJECT`` in usage logs, allow
  policies and deny policies alike (IAM "Principal identifiers").

Service accounts and every other principal type (groups, domains, principal sets) are not people: usage by them can
be attributed to an organization unit, but they never get personal caps.

Keys used across the control plane (case-insensitive, so the BigQuery path, which lower-cases labels.user_id, the
Cloud Logging path and the license API agree):
- Google Account: the lower-cased e-mail (unchanged from earlier versions, so stored rules keep matching).
- Workforce identity: the principal URI lower-cased. Principal.display keeps the subject as the identity provider
  sent it; allow_member and deny_principal build the documented URI form from the value they are given.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import EMAIL_RE

GOOGLE_USER = "google_user"
WORKFORCE = "workforce"
SERVICE_ACCOUNT = "service_account"
OTHER = "other"

WORKFORCE_PREFIX = "principal://iam.googleapis.com/locations/global/workforcepools/"
WORKFORCE_URI_PREFIX = "principal://iam.googleapis.com/locations/global/workforcePools/"
# Deny policies name a Google Account as principal://goog/subject/EMAIL.
GOOGLE_SUBJECT_PREFIX = "principal://goog/subject/"
# Pool ids: letters, digits and hyphens. Subjects: 1 to 512 printable characters without whitespace.
WORKFORCE_RE = re.compile(
    r"^principal://iam\.googleapis\.com/locations/global/workforcePools/([A-Za-z0-9][A-Za-z0-9\-]{1,62})"
    r"/subject/([^\s\x00-\x1f\x7f]{1,512})$", re.IGNORECASE)
# Workforce principals inside free text (masking); stops at whitespace and quotes.
WORKFORCE_IN_TEXT_RE = re.compile(
    r"(principal://iam\.googleapis\.com/locations/global/workforcePools/[A-Za-z0-9\-]{2,63}/subject/)"
    r"([^\s\"'<>]{1,512})", re.IGNORECASE)
_USER_PREFIX_RE = re.compile(r"^user:", re.IGNORECASE)
_SA_PREFIX_RE = re.compile(r"^serviceaccount:", re.IGNORECASE)


@dataclass(frozen=True)
class Principal:
    kind: str
    key: str          # control plane key (see module docstring); for OTHER the trimmed input
    display: str      # e-mail, workforce subject (original case), service account e-mail or the input

    @property
    def is_person(self):
        return self.kind in (GOOGLE_USER, WORKFORCE)


def _workforce(value):
    m = WORKFORCE_RE.match(value)
    if not m:
        return None
    pool, subject = m.group(1).lower(), m.group(2)
    return Principal(WORKFORCE, "%s%s/subject/%s" % (WORKFORCE_PREFIX, pool, subject.lower()), subject)


def parse(value):
    """Principal for a usage label, IAM member or license principal; None for an empty or non-string value."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.lower().startswith("principal://"):
        if raw.lower().startswith(GOOGLE_SUBJECT_PREFIX):
            email = raw[len(GOOGLE_SUBJECT_PREFIX):].strip().lower()
            if EMAIL_RE.match(email) and not email.endswith(".gserviceaccount.com"):
                return Principal(GOOGLE_USER, email, email)
            return Principal(OTHER, raw, raw)
        found = _workforce(raw)
        return found if found is not None else Principal(OTHER, raw, raw)
    if _SA_PREFIX_RE.match(raw):
        email = raw.split(":", 1)[1].strip().lower()
        return Principal(SERVICE_ACCOUNT, email, email)
    candidate = _USER_PREFIX_RE.sub("", raw).strip()
    if ":" in candidate.partition("@")[0]:
        return Principal(OTHER, raw, raw)  # group:, domain:, deleted:, principalSet:, ...
    email = candidate.lower()
    if email.endswith(".gserviceaccount.com") and "@" in email:
        return Principal(SERVICE_ACCOUNT, email, email)
    if EMAIL_RE.match(email):
        return Principal(GOOGLE_USER, email, email)
    return Principal(OTHER, raw, raw)


def person_key(value):
    """The person behind a principal (see module docstring), or None for workloads and other principal types."""
    found = parse(value)
    return found.key if (found is not None and found.is_person) else None


def is_person_key(key):
    found = parse(key)
    return bool(found is not None and found.is_person and found.key == key)


def is_workforce_key(key):
    return isinstance(key, str) and key.lower().startswith(WORKFORCE_PREFIX) and WORKFORCE_RE.match(key) is not None


def _workforce_uri(found):
    pool = found.key[len(WORKFORCE_PREFIX):].split("/subject/", 1)[0]
    return "%s%s/subject/%s" % (WORKFORCE_URI_PREFIX, pool, found.display)


def allow_member(value):
    """IAM allow-policy member for a person (key or principal), or None. Pass the principal as logged when the
    identity provider treats subjects as case-sensitive: keys are lower-cased."""
    found = parse(value)
    if found is None or not found.is_person:
        return None
    return "user:%s" % found.key if found.kind == GOOGLE_USER else _workforce_uri(found)


def deny_principal(value):
    """IAM deny-policy principal identifier for a person (key or principal), or None (see allow_member)."""
    found = parse(value)
    if found is None or not found.is_person:
        return None
    return GOOGLE_SUBJECT_PREFIX + found.key if found.kind == GOOGLE_USER else _workforce_uri(found)


def display(key):
    found = parse(key)
    return found.display if found is not None else ""


def _mask_part(text):
    return "%s***" % (text[:2] if len(text) > 2 else text[:1])


def mask(key):
    """Masked display for viewers: ad***@example.com for e-mails, ab*** for workforce subjects."""
    found = parse(key)
    if found is None:
        return key
    if found.kind == WORKFORCE:
        subject = found.display
        if "@" in subject:
            local, _, domain = subject.rpartition("@")
            return "%s@%s" % (_mask_part(local), domain)
        return _mask_part(subject)
    if found.kind == GOOGLE_USER:
        local, _, domain = found.key.rpartition("@")
        return "%s@%s" % (_mask_part(local), domain)
    return found.display


def mask_workforce_in_text(text):
    """Masks the subject of every workforce principal URI in `text` (viewer and guest responses)."""
    if not isinstance(text, str) or "principal://" not in text.lower():
        return text
    return WORKFORCE_IN_TEXT_RE.sub(lambda m: m.group(1) + _mask_part(m.group(2)), text)


def sql_person_key(user_expr):
    """BigQuery expression: the person key behind a labels.user_id expression, lower-cased with a leading "user:"
    removed (person_key does the same in Python; service accounts and other principals keep their shape and are
    filtered with person_key)."""
    return "LOWER(REGEXP_REPLACE(TRIM(%s), r'(?i)^user:', ''))" % user_expr


def logging_literal(value):
    """A Cloud Logging filter string literal (double quotes, backslash escapes)."""
    return '"%s"' % str(value).replace("\\", "\\\\").replace('"', '\\"')

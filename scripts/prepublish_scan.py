#!/usr/bin/env python3
"""Prepublish scan: the release gate that keeps secrets and deployment identifiers out of the repository.

Run it before every push (CI runs it too):

    python3 scripts/prepublish_scan.py                 # files Git would publish from this checkout
    python3 scripts/prepublish_scan.py --root DIR      # every file under DIR (for example a release copy)

It fails (exit code 1) when a scanned file contains:
  - a secret pattern: private keys, service account keys, Google API keys, OAuth client secrets,
    access tokens, GitHub tokens, Slack tokens or webhooks, Google Chat webhook keys;
  - a value from your git-ignored local configuration (deploy.env, config.local.yaml,
    terraform/terraform.tfvars): project IDs, billing account IDs, bucket names, e-mail addresses, URLs;
  - a term listed in the git-ignored file .prepublish-terms (one term per line, # for comments) or in
    the PREPUBLISH_TERMS environment variable (comma-separated), for example personal account names.
    Lines starting with ! are public phrases, such as the repository URL github.com/OWNER/REPO: a listed
    term or local value may appear inside them (PREPUBLISH_ALLOW adds more, comma-separated);
  - an e-mail address outside the reserved example domains and Google service-agent domains;
  - a Cloud Run URL (https://SERVICE-PROJECT_NUMBER.REGION.run.app) or a local home directory path.

Twelve-digit numbers are reported as notes (project numbers look like that). Matched values are never
printed in full. In CI there is no local configuration, so the secret, e-mail, URL and path checks run.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SELF = pathlib.Path(__file__).resolve()
TERMS_FILE = ".prepublish-terms"
LOCAL_CONFIG_FILES = ("deploy.env", "config.local.yaml", "terraform/terraform.tfvars")
MAX_BYTES = 2_000_000

SECRETS = [
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
    (r"\"private_key\"\s*:\s*\"-----", "service account key"),
    (r"AIza[0-9A-Za-z_\-]{35}", "Google API key"),
    (r"GOCSPX-[0-9A-Za-z_\-]{20,}", "OAuth client secret"),
    (r"ya29\.[0-9A-Za-z_\-]{20,}", "Google access token"),
    (r"1//0[0-9A-Za-z_\-]{30,}", "Google refresh token"),
    (r"gh[pousr]_[A-Za-z0-9]{36,}", "GitHub token"),
    (r"github_pat_[A-Za-z0-9_]{40,}", "GitHub token"),
    (r"https://[^/\s:@]+:[^/\s@]{8,}@github\.com", "credential in a Git URL"),
    (r"xox[baprs]-[0-9A-Za-z\-]{10,}", "Slack token"),
    (r"hooks\.slack\.com/services/T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{12,}", "Slack webhook"),
    (r"chat\.googleapis\.com/v1/spaces/[A-Za-z0-9_\-]+/messages\?key=[A-Za-z0-9_\-]{20,}", "Chat webhook key"),
]
EMAIL_RE = re.compile(r"(?<![A-Za-z0-9:/])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
OK_EMAIL_DOMAINS = ("example.com", "example.net", "example.org", "localhost",
                    "iam.gserviceaccount.com", "developer.gserviceaccount.com", "gserviceaccount.com",
                    "users.noreply.github.com")
RUN_APP_RE = re.compile(r"https://[a-z0-9-]+-\d{6,}\.[a-z0-9-]+\.run\.app")
HOME_RE = re.compile(r"/Users/[A-Za-z][\w.-]*/|/home/[a-z][\w.-]*/|C:\\Users\\[A-Za-z]")
TWELVE_DIGITS_RE = re.compile(r"(?<![\w-])\d{12}(?![\w-])")
# Values that are common words or defaults, never treated as identifiers.
GENERIC = {"true", "false", "none", "null", "global", "us", "eu", "en", "ko", "utc", "asia/seoul", "us-central1",
           "ge_analytics", "google", "google_oauth", "iap", "local", "auto", "allauthenticatedusers",
           "your organization", "billing_export", "billing_detailed_usage", "1gi", "512mi", "manual"}


def mask(value):
    value = str(value)
    return (value[:2] + "***") if len(value) > 2 else "***"


def read_terms():
    """(terms, allowed). Lines of .prepublish-terms that start with "!" (and PREPUBLISH_ALLOW, comma-separated)
    are public phrases, for example the repository URL github.com/OWNER/REPO: they are removed from the text
    before the term and local value checks, so a listed name may appear inside them and nowhere else."""
    terms, allowed = set(), set()
    path = ROOT / TERMS_FILE
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip().lower()
            if line.startswith("!"):
                if line[1:].strip():
                    allowed.add(line[1:].strip())
            elif line:
                terms.add(line)
    for term in (os.environ.get("PREPUBLISH_TERMS") or "").split(","):
        if term.strip():
            terms.add(term.strip().lower())
    for phrase in (os.environ.get("PREPUBLISH_ALLOW") or "").split(","):
        if phrase.strip():
            allowed.add(phrase.strip().lower())
    return terms, allowed


def _scalar_values(text):
    """KEY=VALUE (shell), key: value (YAML) and key = "value" (tfvars) scalars, lists split on commas."""
    values = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^[\w.\-\"]+\s*(?:=|:)\s*(.+)$", line)
        if not match:
            if line.startswith("- "):
                values.append(line[2:])
            continue
        raw = match.group(1).split(" #", 1)[0].strip()
        raw = raw.strip("[]")
        for part in raw.split(","):
            values.append(part)
    out = []
    for value in values:
        value = value.strip().strip("'\"").strip()
        for prefix in ("serviceAccount:", "user:", "group:", "domain:"):
            if value.startswith(prefix):
                value = value[len(prefix):]
        if value:
            out.append(value)
    return out


def _is_identifier(low):
    """True for values that identify a deployment (project IDs and numbers, billing IDs, buckets, e-mail
    addresses, URLs); false for defaults, model names, schedules, sizes and placeholders."""
    if len(low) < 6 or low in GENERIC:
        return False
    if re.search(r"\s|\*", low) or low.startswith(("gemini-", "your-", "roles/", "http://localhost",
                                                    "ai-control-plane")):  # product defaults
        return False
    if re.search(r"example\.(?:com|net|org)", low):
        return False
    if low.isdigit():
        return len(low) >= 9  # project numbers; shorter numbers are sizes and limits
    if re.fullmatch(r"[a-z]+", low) and len(low) < 10:
        return False  # plain short words (periods, modes, city names)
    return True


def local_values():
    found = set()
    for rel in LOCAL_CONFIG_FILES:
        path = ROOT / rel
        if not path.is_file():
            continue
        for value in _scalar_values(path.read_text(encoding="utf-8", errors="replace")):
            low = value.lower().rstrip("/")
            if _is_identifier(low):
                found.add(low)
    return found


def publish_set(args):
    if args.root:
        base = pathlib.Path(args.root).resolve()
        files = [p for p in base.rglob("*") if p.is_file() and ".git" not in p.relative_to(base).parts]
        return base, files
    if args.files:
        base = ROOT
        names = pathlib.Path(args.files).read_text(encoding="utf-8").splitlines()
        return base, [base / n.strip() for n in names if n.strip()]
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return ROOT, [ROOT / n for n in out.splitlines() if n and (ROOT / n).is_file()]


def check_text(rel, text, terms=(), values=(), allowed=()):
    """(problems, notes) for one file. Allowed public phrases are removed only for the term and local value
    checks; secrets, e-mail addresses, Cloud Run URLs and home paths are checked on the full text."""
    problems, notes = [], []
    low = text.lower()
    named = low
    for phrase in allowed:
        named = named.replace(phrase, " ")
    for pattern, label in SECRETS:
        if re.search(pattern, text):
            problems.append("%s: %s" % (rel, label))
    for term in terms:
        if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", named):
            problems.append("%s: listed term %s" % (rel, mask(term)))
    for value in values:
        if value in named:
            problems.append("%s: value from local configuration %s" % (rel, mask(value)))
    for match in EMAIL_RE.finditer(text):
        email = match.group(0).lower().rstrip(".")
        domain = email.split("@", 1)[1]
        if domain.startswith(("example.com", "example.net", "example.org")):
            continue  # reserved example domains (also when text follows, as in test hash inputs)
        if not any(domain == d or domain.endswith("." + d) for d in OK_EMAIL_DOMAINS):
            problems.append("%s: e-mail address %s" % (rel, mask(email)))
    for match in RUN_APP_RE.finditer(text):
        problems.append("%s: Cloud Run URL with a project number %s" % (rel, mask(match.group(0)[8:])))
    if HOME_RE.search(text):
        problems.append("%s: local home directory path" % rel)
    for match in TWELVE_DIGITS_RE.finditer(text):
        notes.append("%s: 12-digit number %s" % (rel, mask(match.group(0))))
    return problems, notes


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--root", help="scan every file under this directory instead of the Git publish set")
    group.add_argument("--files", help="scan the files listed in this file (paths relative to the repository)")
    args = parser.parse_args()

    base, files = publish_set(args)
    terms, allowed = read_terms()
    values = local_values()
    problems, notes = [], []
    for path in sorted(files):
        if path.resolve() == SELF or "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(base))
        if path.name in (TERMS_FILE,) + tuple(pathlib.Path(f).name for f in LOCAL_CONFIG_FILES):
            problems.append("%s: local configuration file in the publish set" % rel)
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                notes.append("%s: larger than %d bytes, skipped" % (rel, MAX_BYTES))
                continue
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary file (images, fonts)
        found, noted = check_text(rel, text, terms, values, allowed)
        problems += found
        notes += noted

    print("files scanned: %d | listed terms: %d | public phrases: %d | local configuration values: %d"
          % (len(files), len(terms), len(allowed), len(values)))
    for note in sorted(set(notes)):
        print("note: " + note)
    if problems:
        print("PROBLEMS (fix them before publishing):")
        for problem in sorted(set(problems)):
            print("  " + problem)
        return 1
    print("clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())

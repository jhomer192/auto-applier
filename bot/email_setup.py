"""Self-service email setup for applicants.

Lets a scoped applicant (e.g. zvessey) register their own mailbox so the applier
applies *as them* and reads their verification PINs:
  - `profile.yaml`'s `email:` (typed into application forms), and
  - `.env`'s IMAP_USER/IMAP_PASS/IMAP_HOST/IMAP_PORT (read by scripts/check_email.cjs
    at apply time to pull the PIN)
both get pointed at the submitted address. The IMAP login is verified BEFORE
anything is persisted, so bad creds never get stored. The caller is responsible
for deleting the Discord message that carried the password — this module never
logs or echoes it.
"""
from __future__ import annotations

import asyncio
import imaplib
import os
import re

# domain -> (imap_host, port). Covers the common consumer providers plus the
# homerfamily Network Solutions mailbox. Unknown domains require an explicit host.
_IMAP_HOSTS: dict[str, tuple[str, int]] = {
    "gmail.com": ("imap.gmail.com", 993),
    "googlemail.com": ("imap.gmail.com", 993),
    "outlook.com": ("outlook.office365.com", 993),
    "hotmail.com": ("outlook.office365.com", 993),
    "live.com": ("outlook.office365.com", 993),
    "msn.com": ("outlook.office365.com", 993),
    "yahoo.com": ("imap.mail.yahoo.com", 993),
    "aol.com": ("imap.aol.com", 993),
    "icloud.com": ("imap.mail.me.com", 993),
    "me.com": ("imap.mail.me.com", 993),
    "homerfamily.com": ("netsol-imap-oxcs.hostingplatform.com", 993),
}

_ADDR_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailSetupError(ValueError):
    """Raised with a user-safe message (never contains the password)."""


def resolve_imap(address: str, explicit_host: str | None = None) -> tuple[str, int]:
    """Return (imap_host, port) for *address*, or raise EmailSetupError."""
    if not _ADDR_RE.match(address or ""):
        raise EmailSetupError(f"`{address}` doesn't look like an email address.")
    if explicit_host:
        host = explicit_host.strip()
        port = 993
        if ":" in host:
            host, _, p = host.partition(":")
            port = int(p) if p.isdigit() else 993
        return host, port
    domain = address.rsplit("@", 1)[1].lower()
    if domain not in _IMAP_HOSTS:
        raise EmailSetupError(
            f"I don't know the IMAP server for `{domain}`. Re-run with it explicitly:\n"
            f"`/email {address} <app-password> imap.yourprovider.com`"
        )
    return _IMAP_HOSTS[domain]


def _test_login(host: str, port: int, user: str, password: str) -> None:
    """Blocking IMAP login check. Raises EmailSetupError on failure."""
    try:
        with imaplib.IMAP4_SSL(host, port) as conn:
            conn.login(user, password)
            conn.select("INBOX", readonly=True)
    except imaplib.IMAP4.error:
        # Wrong user/password or provider rejected the login (e.g. Gmail needs an
        # App Password, not the account password). Message must not leak the secret.
        raise EmailSetupError(
            "IMAP login was rejected. Double-check the address and use an **app "
            "password** (Gmail/Yahoo/Outlook require one — not your normal password)."
        )
    except OSError as exc:
        raise EmailSetupError(f"Couldn't reach {host}:{port} ({exc.__class__.__name__}).")


def _atomic_write(path: str, text: str, mode: int = 0o600) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def set_env_keys(env_path: str, updates: dict[str, str]) -> None:
    """Set/replace each KEY=value in *env_path*, preserving all other lines. 0600."""
    lines = []
    if os.path.exists(env_path):
        lines = open(env_path, encoding="utf-8").read().splitlines()
    remaining = dict(updates)
    out = []
    for ln in lines:
        key = ln.split("=", 1)[0] if "=" in ln else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(ln)
    for key, val in remaining.items():
        out.append(f"{key}={val}")
    _atomic_write(env_path, "\n".join(out) + "\n")


def set_profile_email(profile_path: str, address: str) -> None:
    """Replace the top-level `email:` line in profile.yaml, preserving the rest."""
    text = open(profile_path, encoding="utf-8").read()
    if re.search(r"(?m)^email:.*$", text):
        text = re.sub(r"(?m)^email:.*$", f"email: {address}", text, count=1)
    else:
        text = f"email: {address}\n" + text
    _atomic_write(profile_path, text, mode=0o644)


async def submit_email(
    address: str,
    password: str,
    *,
    env_path: str,
    profile_path: str,
    explicit_host: str | None = None,
) -> str:
    """Validate creds (live IMAP login), then persist to .env + profile.yaml.

    Returns a user-safe success summary (no password). Raises EmailSetupError with
    a safe message on any validation/storage failure.
    """
    address = (address or "").strip()
    password = (password or "").strip()
    if not address or not password:
        raise EmailSetupError("Usage: `/email <address> <app-password> [imap-host]`")
    host, port = resolve_imap(address, explicit_host)
    await asyncio.to_thread(_test_login, host, port, address, password)
    set_env_keys(env_path, {
        "IMAP_USER": address,
        "IMAP_PASS": password,
        "IMAP_HOST": host,
        "IMAP_PORT": str(port),
    })
    set_profile_email(profile_path, address)
    return (
        f"✅ Verified and saved **{address}** (IMAP `{host}`).\n"
        f"Applications will now go out under this address and I'll read its inbox "
        f"for verification PINs. Your message was deleted so the password isn't left in chat."
    )

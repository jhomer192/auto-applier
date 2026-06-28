"""Self-service email setup for applicants.

Lets a scoped applicant (e.g. zvessey) register their own mailbox so the applier
applies *as them* and reads their verification PINs:
  - `profile.yaml`'s `email:` (typed into application forms), and
  - `.env`'s IMAP_USER/IMAP_PASS/IMAP_HOST/IMAP_PORT (read by scripts/check_email.cjs
    at apply time to pull the PIN)
both get pointed at the submitted address, AND the same four keys are refreshed in
this process's os.environ — otherwise apply subprocesses (which inherit
`dict(os.environ)`) and check_email.cjs (which only fills vars *not already set*)
would keep using the stale startup creds. The IMAP login is verified BEFORE
anything is persisted, so bad creds never get stored. The caller is responsible
for deleting the Discord message that carried the password; this module never
logs or echoes it.
"""
from __future__ import annotations

import asyncio
import imaplib
import os
import re
import tempfile

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
# An explicit IMAP host: a dotted hostname, optional :port. App passwords never
# contain a dot, so this reliably distinguishes a trailing host from password text.
_HOST_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:\d+)?$")


class EmailSetupError(ValueError):
    """Raised with a user-safe message (never contains the password)."""


def parse_email_command(remainder: str) -> tuple[str | None, str | None, str | None]:
    """Parse `<address> <app-password...> [imap-host]` into (address, password, host).

    The app password may arrive in space-separated groups (Gmail shows it as four
    chunks and users paste it verbatim) — middle tokens are joined with no
    separator. An explicit IMAP host, if present, is the trailing token that looks
    like a dotted hostname. Returns (None, None, None) on empty input.
    """
    toks = (remainder or "").split()
    if not toks:
        return None, None, None
    address = toks[0]
    rest = toks[1:]
    host = None
    if rest and _HOST_RE.match(rest[-1]):
        host = rest[-1]
        rest = rest[:-1]
    password = "".join(rest)  # collapse spaced app-password groups
    return address, (password or None), host


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
    """Write *text* to *path* atomically. The temp file is created 0600 with an
    unpredictable name (O_EXCL via mkstemp) so the secret is never world-readable
    nor exposed to a symlink/clobber race, then chmod'd to the final mode."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".env")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_env_keys(env_path: str, updates: dict[str, str]) -> None:
    """Set/replace each KEY=value in *env_path*, preserving all other lines and
    collapsing any pre-existing duplicates so no stale secret survives. 0600."""
    lines = []
    if os.path.exists(env_path):
        lines = open(env_path, encoding="utf-8").read().splitlines()
    written: set[str] = set()
    out: list[str] = []
    for ln in lines:
        if "=" in ln:
            key = ln.split("=", 1)[0].strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if key in updates:
                if key not in written:  # first occurrence -> rewrite; drop the rest
                    out.append(f"{key}={updates[key]}")
                    written.add(key)
                continue
        out.append(ln)
    for key, val in updates.items():
        if key not in written:
            out.append(f"{key}={val}")
            written.add(key)
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
    profile_paths,
    explicit_host: str | None = None,
) -> str:
    """Validate creds (live IMAP login), then persist to .env + every profile.yaml
    in *profile_paths* AND refresh os.environ so the very next apply uses them.

    *profile_paths* must include BOTH the bot's profile.yaml and the apply
    workspace's (/opt/auto-applier/profile.yaml) — the apply subprocess reads its
    own copy, so updating only the bot's would leave the wrong email on forms.

    Returns a user-safe success summary (no password, no deletion claim — the
    caller reports the message-deletion outcome). Raises EmailSetupError with a
    safe message on any validation/storage failure.
    """
    address = (address or "").strip()
    password = (password or "").strip()
    if not address or not password:
        raise EmailSetupError("Usage: `/email <address> <app-password> [imap-host]`")
    host, port = resolve_imap(address, explicit_host)
    await asyncio.to_thread(_test_login, host, port, address, password)
    creds = {"IMAP_USER": address, "IMAP_PASS": password,
             "IMAP_HOST": host, "IMAP_PORT": str(port)}
    set_env_keys(env_path, creds)
    for p in profile_paths:
        if p and os.path.exists(p):
            set_profile_email(p, address)
    # Refresh the live process env: apply subprocesses inherit dict(os.environ),
    # and check_email.cjs only fills vars that aren't ALREADY set — without this,
    # the next apply would read PINs from the stale startup inbox, not this one.
    os.environ.update(creds)
    return (
        f"✅ Verified and saved **{address}** (IMAP `{host}`). Applications will now "
        f"go out under this address and I'll read its inbox for verification PINs."
    )

"""Dereferencing secret pointers.

bounds core never stores a secret — `credential.secret_uri` is an address
into a backend whose job is holding secrets. This module resolves that
address at the moment of use, and nowhere else: the control plane and the
log stay two hops from the key (ref -> secret_uri -> bytes) and never
get closer.

Schemes:

    env:NAME                  environment variable. Dev and smoke only.
    keyring://service/entry   the OS keyring (Windows Credential Manager,
                              macOS Keychain, libsecret). Needs the
                              `keyring` package: pip install bounds[secrets]

Missing is loud. A pointer that doesn't dereference raises — it never
returns None or empty, because a blank secret handed to a connector fails
somewhere less honest than here. Note the failure point: dispatch() resolves
*after* the allow is committed, so a broken pointer lands in the log as
allow + outcome=error. The decision was right; the execution failed; the
log says exactly that.
"""

import os


class SecretResolutionError(Exception):
    """The pointer did not dereference. Configuration, not policy —
    check() has already allowed by the time this can raise."""


def _from_env(rest: str) -> str:
    val = os.environ.get(rest)
    if not val:
        raise SecretResolutionError(f"env var {rest!r} is unset or empty")
    return val


def _from_keyring(rest: str) -> str:
    try:
        import keyring
    except ImportError as e:
        raise SecretResolutionError(
            "keyring:// requires the keyring package — pip install bounds[secrets]"
        ) from e

    service, _, entry = rest.partition("/")
    if not service or not entry:
        raise SecretResolutionError(
            f"malformed keyring uri {rest!r} — want keyring://service/entry"
        )
    val = keyring.get_password(service, entry)
    if not val:
        raise SecretResolutionError(f"no keyring entry for {service}/{entry}")
    return val


_SCHEMES = {
    "env": lambda uri: _from_env(uri.partition(":")[2]),
    "keyring": lambda uri: _from_keyring(uri.partition("://")[2]),
}


def resolve(secret_uri: str) -> str:
    scheme = secret_uri.split(":", 1)[0]
    handler = _SCHEMES.get(scheme)
    if handler is None:
        raise SecretResolutionError(
            f"unknown secret scheme {scheme!r} in {secret_uri!r} "
            f"(known: {sorted(_SCHEMES)})"
        )
    return handler(secret_uri)


def resolver_for(conn):
    """The production `resolve_secret` for dispatch(): ref -> credential row
    -> secret_uri -> backend. The seam dispatch() has had since day one,
    finally wearing something real."""

    def resolve_secret(ref: str) -> str:
        row = conn.execute(
            "SELECT secret_uri FROM credential WHERE ref = %s", (ref,)
        ).fetchone()
        if row is None:
            # check() verified the ref before allowing, so this means the
            # credential vanished between decision and execution. Loud.
            raise SecretResolutionError(f"credential ref {ref!r} does not resolve")
        return resolve(row["secret_uri"])

    return resolve_secret

"""A pointer that doesn't dereference must raise — never return None or
empty. A blank secret handed to a connector fails somewhere less honest
than here.
"""

import sys
from types import SimpleNamespace as NS

import pytest

from bounds.secrets import SecretResolutionError, resolve, resolver_for


# --- env ----------------------------------------------------------------


def test_env_resolves(monkeypatch):
    monkeypatch.setenv("SMTP_PW", "hunter2")
    assert resolve("env:SMTP_PW") == "hunter2"


def test_env_unset_is_loud(monkeypatch):
    monkeypatch.delenv("SMTP_PW", raising=False)
    with pytest.raises(SecretResolutionError, match="SMTP_PW"):
        resolve("env:SMTP_PW")


def test_env_empty_is_as_bad_as_unset(monkeypatch):
    monkeypatch.setenv("SMTP_PW", "")
    with pytest.raises(SecretResolutionError):
        resolve("env:SMTP_PW")


# --- keyring ------------------------------------------------------------


def fake_keyring(store):
    return NS(get_password=lambda service, entry: store.get((service, entry)))


def test_keyring_resolves(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "keyring", fake_keyring({("bounds", "demo"): "s3cret"})
    )
    assert resolve("keyring://bounds/demo") == "s3cret"


def test_keyring_missing_entry_is_loud(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring({}))
    with pytest.raises(SecretResolutionError, match="bounds/demo"):
        resolve("keyring://bounds/demo")


def test_keyring_uri_without_an_entry_is_malformed(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring({}))
    with pytest.raises(SecretResolutionError, match="malformed"):
        resolve("keyring://bounds")


# --- scheme dispatch ----------------------------------------------------


def test_unknown_scheme_names_itself():
    with pytest.raises(SecretResolutionError, match="'vault'"):
        resolve("vault://secret/data/x")


# --- the ref -> uri -> secret chain -------------------------------------


class FakeConn:
    def __init__(self, row):
        self._row = row

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return self._row


def test_resolver_for_walks_ref_to_uri_to_secret(monkeypatch):
    monkeypatch.setenv("DEMO", "the-bytes")
    resolve_secret = resolver_for(FakeConn({"secret_uri": "env:DEMO"}))
    assert resolve_secret("demo-send-only") == "the-bytes"


def test_resolver_for_is_loud_on_a_vanished_ref():
    resolve_secret = resolver_for(FakeConn(None))
    with pytest.raises(SecretResolutionError, match="does not resolve"):
        resolve_secret("gone")

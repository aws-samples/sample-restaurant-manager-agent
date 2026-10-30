# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the VAPID rotation Lambda + shared keygen (spec 028).

boto3 clients are replaced with fakes; no AWS calls. Verifies a fresh keypair is
written to the secret with the subject preserved, the PUBLIC key is mirrored to
SSM, and the marker never contains the private key.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import pathlib
import sys

import pytest

_ROTATOR_DIR = pathlib.Path(__file__).resolve().parent.parent / "lambdas/rotator"


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _ensure_importable() -> None:
    """Put the Lambda dir on `sys.path` **last**, never first.

    The tests need it there so the handler's first-party imports resolve
    (`keygen`, and `from keygen import ...` inside `main.py`). But `scripts/
    bundle.sh` also vendors third-party packages into that same directory for
    **arm64**, so prepending it made the vendored `cryptography` shadow the dev
    venv's x86_64 build, and importing it died on the arm64 `_rust.abi3.so`:

        ImportError: lambdas/rotator/cryptography/hazmat/bindings/_rust.abi3.so:
        cannot open shared object file: No such file or directory

    That made three tests here pass on a clean checkout and fail on any host
    that had prepared a deploy -- i.e. the result depended on local build state
    rather than on the code. Appending keeps first-party resolution (nothing
    else is named `keygen`) while site-packages continues to win for anything
    vendored. `test_vendored_deps_do_not_shadow_the_venv` guards it.
    """
    path = str(_ROTATOR_DIR)
    if path not in sys.path:
        sys.path.append(path)


def _load_keygen():
    _ensure_importable()
    import keygen  # noqa: PLC0415
    return keygen


def _load_rotator():
    _ensure_importable()
    spec = importlib.util.spec_from_file_location("rotator_main", _ROTATOR_DIR / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeSecrets:
    def __init__(self, existing=None):
        self.existing = existing
        self.put = []

    def get_secret_value(self, SecretId):
        if self.existing is None:
            raise RuntimeError("not found")
        return {"SecretString": json.dumps(self.existing)}

    def put_secret_value(self, SecretId, SecretString):
        self.put.append(json.loads(SecretString))


class _FakeSsm:
    def __init__(self):
        self.put = []

    def put_parameter(self, Name, Value, Type, Overwrite):
        self.put.append({"Name": Name, "Value": Value, "Type": Type, "Overwrite": Overwrite})


def test_generate_vapid_shape_and_freshness():
    kg = _load_keygen()
    keys = kg.generate_vapid("mailto:ops@example.com")
    assert keys["subject"] == "mailto:ops@example.com"
    assert len(_b64url_decode(keys["private_key"])) == 32          # P-256 private scalar
    pub = _b64url_decode(keys["public_key"])
    assert len(pub) == 65 and pub[0] == 0x04                        # uncompressed point
    assert kg.generate_vapid("mailto:x")["private_key"] != keys["private_key"]  # fresh each call


def test_handler_rotates_preserves_subject_and_mirrors_public_key(capsys):
    rot = _load_rotator()
    rot._secrets = _FakeSecrets(existing={"private_key": "old", "public_key": "oldpub", "subject": "mailto:keep@x"})
    rot._ssm = _FakeSsm()

    out = rot.handler({}, None)

    assert out["status"] == "rotated"
    assert len(rot._secrets.put) == 1
    written = rot._secrets.put[0]
    assert written["subject"] == "mailto:keep@x"                    # subject preserved
    assert written["private_key"] != "old" and written["public_key"] != "oldpub"  # new keypair
    assert len(rot._ssm.put) == 1
    assert rot._ssm.put[0]["Value"] == written["public_key"]        # public key mirrored to SSM
    assert rot._ssm.put[0]["Overwrite"] is True
    assert out["public_key"] == written["public_key"]
    # Marker logs the PUBLIC key only, never the private key.
    marker = capsys.readouterr().out
    assert written["public_key"] in marker
    assert written["private_key"] not in marker


def test_handler_subject_fallback_when_secret_unreadable():
    rot = _load_rotator()
    rot._secrets = _FakeSecrets(existing=None)   # get_secret_value raises -> default subject
    rot._ssm = _FakeSsm()
    out = rot.handler({}, None)
    assert out["status"] == "rotated"
    assert rot._secrets.put[0]["subject"] == rot.DEFAULT_SUBJECT


def test_vendored_deps_do_not_shadow_the_venv() -> None:
    """A vendored arm64 package must never win over the dev venv's own build.

    This is the regression guard for the shadowing bug described on
    `_ensure_importable`. It is only meaningful once `scripts/bundle.sh` has
    run -- on a clean checkout there is nothing to shadow -- so it skips when
    the vendored tree is absent rather than passing vacuously and implying
    coverage it does not have.
    """
    if not (_ROTATOR_DIR / "cryptography").is_dir():
        pytest.skip("vendored tree absent (bundle.sh not run) - nothing to shadow")

    _load_keygen()  # performs the sys.path manipulation under test
    import cryptography  # noqa: PLC0415

    resolved = pathlib.Path(cryptography.__file__).resolve()
    assert _ROTATOR_DIR.resolve() not in resolved.parents, (
        f"cryptography resolved to the vendored arm64 copy at {resolved}. The Lambda "
        "directory must be APPENDED to sys.path, never prepended, or this suite's "
        "result depends on whether the host has bundled for deploy."
    )

    # And the handler must still be importable -- an append that broke first-party
    # resolution would make the guard above pass for the wrong reason.
    assert callable(_load_rotator().handler)

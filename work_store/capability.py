"""Owner capability issuance/verification for resolve_block (design section 1,
acceptance criterion B).

A capability is an HMAC-signed token bound to (operation, item_id,
expected_revision, decision_ref) plus a random nonce. Verification is
transaction-scoped: the caller must `reserve()` before opening the
transaction, then `finalize()` on commit or `release()` on rollback. A
reserved-but-unfinalized token cannot be reserved again (prevents a
concurrent double-spend of the same capability), and a finalized token can
never be reused (prevents replay after a successful resolution). Knowing the
stored decision_ref text (readable by anyone via snapshot) is not sufficient
to construct a valid signature, since the HMAC secret never leaves the
issuer.
"""
from __future__ import annotations

import hmac
import hashlib
import secrets


class CapabilityError(Exception):
    pass


class OwnerCapabilityIssuer:
    def __init__(self, secret: bytes | None = None) -> None:
        self._secret = secret if secret is not None else secrets.token_bytes(32)
        self._reserved: set[str] = set()
        self._consumed: set[str] = set()

    def issue(self, item_id: str, expected_revision: int, decision_ref: str,
              operation: str = "resolve_block") -> str:
        nonce = secrets.token_hex(16)
        payload = "|".join([operation, item_id, str(expected_revision), decision_ref, nonce])
        sig = hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return payload + "|" + sig

    def _verify_scope(self, token: str, item_id: str, expected_revision: int,
                       decision_ref: str, operation: str) -> None:
        parts = token.split("|")
        if len(parts) != 6:
            raise CapabilityError("capability-malformed")
        op, iid, rev, ref, nonce, sig = parts
        payload = "|".join([op, iid, rev, ref, nonce])
        expected_sig = hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            raise CapabilityError("capability-signature-invalid")
        if op != operation or iid != item_id or rev != str(expected_revision) or ref != decision_ref:
            raise CapabilityError("capability-scope-mismatch")

    def reserve(self, token: str, item_id: str, expected_revision: int,
                decision_ref: str, operation: str = "resolve_block") -> str:
        self._verify_scope(token, item_id, expected_revision, decision_ref, operation)
        if token in self._consumed:
            raise CapabilityError("capability-already-consumed")
        if token in self._reserved:
            raise CapabilityError("capability-already-reserved")
        self._reserved.add(token)
        return token

    def finalize(self, token: str) -> None:
        self._reserved.discard(token)
        self._consumed.add(token)

    def release(self, token: str) -> None:
        self._reserved.discard(token)

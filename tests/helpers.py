import os
import sqlite3
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from work_store import api, db, migrate
from work_store.capability import OwnerCapabilityIssuer
from work_store.db import OperationError
from work_store.util import now_rfc3339

_TMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
os.makedirs(_TMP_DIR, exist_ok=True)


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = os.path.join(_TMP_DIR, f"test-{uuid.uuid4().hex}.db")
        raw = sqlite3.connect(self.db_path)
        self.migration_checksum = migrate.apply_schema(raw, now_rfc3339(), "test-actor")
        raw.close()
        self.conn = db.connect(self.db_path)
        self.issuer = OwnerCapabilityIssuer()

    def tearDown(self) -> None:
        self.conn.close()
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(self.db_path + ext)
            except FileNotFoundError:
                pass

    # -- convenience builders -------------------------------------------------

    def make_item(self, phase="design-accepted", lane="operator", env="local",
                   title="item", item_id=None, actor="tester"):
        item_id = item_id or f"item-{uuid.uuid4().hex[:8]}"
        api.create_item(self.conn, item_id, title, phase, lane, env, actor)
        return item_id

    def claim_item(self, item_id, expected_revision=1, actor="tester", valid_until=None,
                    proof_value=None, observation="live"):
        valid_until = valid_until or "2999-01-01T00:00:00.000000Z"
        proof_value = proof_value or f"pid-{uuid.uuid4().hex[:6]}"
        result = api.claim(
            self.conn, item_id, expected_revision,
            {"proof_kind": "worker-pid", "proof_value": proof_value, "host_id": "host-1",
             "issued_actor": actor, "valid_until": valid_until, "observation": observation},
            actor,
        )
        return result

    def raw_authorized_insert(self, kind, item_id, statements, actor="tester", **ctx_kwargs):
        """Attempt one or more raw INSERTs through the normal authorized
        operation context (service-authorized SQL path), for tests that
        probe the boundary directly rather than through a typed API
        wrapper. `statements` is either a list of (sql, params) tuples, or
        a callable(ctx) -> list of (sql, params) tuples (use this form
        when a statement needs the real ctx.nonce). All statements run
        with the SAME operation context/nonce, mirroring how a typed API
        function would issue multiple inserts atomically. sqlite3 errors
        are translated to OperationError like the real API layer does.
        """
        try:
            with db.operation(self.conn, kind, item_id, actor, **ctx_kwargs) as ctx:
                resolved = statements(ctx) if callable(statements) else statements
                for sql, params in resolved:
                    self.conn.execute(sql, params)
                nonce = ctx.nonce
        except sqlite3.Error as e:
            raise OperationError(str(e)) from e
        return nonce


def corrupted_copy(src_path: str, dst_path: str, disable_triggers: list[str]) -> sqlite3.Connection:
    """Offline copy with specific trigger(s) dropped, for fixtures that must
    demonstrate fail-closed detection (I001-I017) after an admitted write
    that the live service would never allow. Also disables CHECK constraint
    enforcement (PRAGMA ignore_check_constraints), since several invariants
    are additionally (redundantly) guarded by a row-local CHECK -- exercising
    the invariant query itself requires getting the bad row into the table
    at all, which on a live connection the CHECK alone would already refuse.
    Never used against production; this bare connection has no authorizer
    and no op_* functions, which is exactly why the *_contract/*_event
    triggers touching those tables must also be dropped before any direct
    INSERT on them will succeed."""
    import shutil
    shutil.copyfile(src_path, dst_path)
    conn = sqlite3.connect(dst_path)
    conn.execute("PRAGMA ignore_check_constraints=ON")
    for trg in disable_triggers:
        conn.execute(f"DROP TRIGGER {trg}")
    conn.commit()
    return conn

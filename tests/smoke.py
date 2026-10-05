import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from work_store import migrate, db
from work_store.util import now_rfc3339

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smoke.db")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(path + ext)
    except FileNotFoundError:
        pass

raw = sqlite3.connect(path)
checksum = migrate.apply_schema(raw, now_rfc3339(), "build-tool")
print("checksum", checksum)
raw.close()

conn = db.connect(path)
print("attr set ok", conn._op_holder)

with db.operation(conn, "create_item", "item-A", "tester") as ctx:
    conn.execute(
        "INSERT INTO work_items(id,title,phase,lane,env,brief_ref,created_at,created_actor) VALUES (?,?,?,?,?,?,?,?)",
        ("item-A", "Test item", "design-accepted", "operator", "local", None, now_rfc3339(), "tester"),
    )
    conn.execute(
        "INSERT INTO item_revisions(item_id,revision,prior_revision,status,payload,occurred_at,actor,operation_nonce) VALUES (?,1,NULL,'queued','{}',?,?,?)",
        ("item-A", now_rfc3339(), "tester", ctx.nonce),
    )
print("insert ok")
row = conn.execute("SELECT * FROM current_items WHERE id='item-A'").fetchone()
print("current_items row", row)

try:
    conn.execute("UPDATE item_revisions SET status='done' WHERE item_id='item-A'")
    print("FAIL: update succeeded")
except sqlite3.Error as e:
    print("update correctly denied:", e)

try:
    conn.execute(
        "INSERT INTO item_events(item_id,occurred_at,actor,event_type) VALUES (?,?,?,?)",
        ("item-A", now_rfc3339(), "tester", "operator-note"),
    )
    print("FAIL: insert without context succeeded")
except sqlite3.Error as e:
    print("insert without context correctly denied:", e)

"""Serve the control room against the live demo database.

Registers the demo send_email action (importing live_smoke does that),
seeds boundary + credential, then serves on 127.0.0.1:8400.

    python scripts/control_room.py
"""

import psycopg
import uvicorn

# Importing registers the demo action in the catalogue REGISTRY.
from live_smoke import DSN, SEED
from psycopg.rows import dict_row

from bounds.catalogue import sync, verify
from bounds.gui import create_app


def main():
    with psycopg.connect(DSN, autocommit=True, row_factory=dict_row) as conn:
        sync(conn)
        verify(conn)
        conn.execute(SEED)
    print("catalogue synced, boundary seeded — http://127.0.0.1:8400")
    uvicorn.run(create_app(DSN), host="127.0.0.1", port=8400, log_level="warning")


if __name__ == "__main__":
    main()

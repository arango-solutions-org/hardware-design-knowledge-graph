#!/usr/bin/env python3
"""
server.py — run ChronoGraph.

    python server.py                 # http://127.0.0.1:8700
    CHRONO_SOURCE=arango python server.py   # force live DB (needs working creds)
    CHRONO_SOURCE=snapshot python server.py # force offline snapshot

Before first run, build the snapshot:
    python -m ic_viz.build_snapshot
"""
import os
import sys

import uvicorn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    host = os.getenv("CHRONO_HOST", "127.0.0.1")
    port = int(os.getenv("CHRONO_PORT", "8700"))
    snap = os.path.join(HERE, "snapshot", "snapshot.json")
    if os.getenv("CHRONO_SOURCE", "auto") != "arango" and not os.path.exists(snap):
        print("[server] snapshot not found — building it now …")
        from ic_viz.build_snapshot import build
        build()
    print(f"[server] ChronoGraph on http://{host}:{port}")
    uvicorn.run("ic_viz.api:asgi_app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()

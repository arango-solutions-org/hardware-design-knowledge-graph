"""ic_viz — ChronoGraph: a bespoke temporal / provenance visualizer for the IC knowledge graph.

Two data sources expose an identical JSON contract:
  * SnapshotSource — reads an offline snapshot built from data/ exports (works with no DB).
  * ArangoSource   — reads the live temporal graph over AQL (used when creds are available).
"""

__version__ = "0.3.0"

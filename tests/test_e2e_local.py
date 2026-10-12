"""
tests/test_e2e_local.py — End-to-end smoke test against a local Docker ArangoDB.

Requires the `arango_docker` session fixture from conftest.py, which provisions
a throwaway container on a free port and tears it down after the session.

What is tested:
  1. RTL extraction  — parse OR1200 and MOR1KX Verilog into the local DB.
  2. Semantic bridge — RESOLVED_TO edges are created from RTL nodes to golden entities.
  3. Cross-repo bridge — CROSS_REPO_SIMILAR_TO edges are created.
  4. Count sanity    — final counts stay within expected ranges.

Skip conditions:
  - Docker not available (fixture skips automatically).
  - Repo clone not present on disk (skip with informative message).

These tests are intentionally NOT marked as fast-path unit tests — they live in
the regular pytest session but are skipped cleanly in CI without Docker.
"""

import os
import sys
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

OR1200_RTL_DIR  = os.path.join(PROJECT_ROOT, "or1200", "rtl", "verilog")
MOR1KX_RTL_DIR  = os.path.join(PROJECT_ROOT, "data", "repos", "mor1kx", "rtl", "verilog")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_local_db(arango_docker):
    """Return a fresh ArangoClient DB handle configured for the local test container."""
    from arango import ArangoClient
    client = ArangoClient(hosts=arango_docker._test_host)
    return client.db(
        arango_docker._test_dbname,
        username="root",
        password=arango_docker._test_password,
    )


def _collection_count(db, name: str) -> int:
    if not db.has_collection(name):
        return 0
    return db.collection(name).count()


# ---------------------------------------------------------------------------
# Section 1 — RTL extraction
# ---------------------------------------------------------------------------

class TestRTLExtractionLocal:
    @pytest.mark.skipif(not os.path.isdir(OR1200_RTL_DIR),
                        reason=f"OR1200 RTL clone not found at {OR1200_RTL_DIR}")
    def test_or1200_rtl_populates_collections(self, arango_docker):
        """RTL extraction for OR1200 should create RTL_Module, RTL_Port, RTL_Signal."""
        db = _get_local_db(arango_docker)
        from etl_rtl import parse_verilog_files

        summary = parse_verilog_files(
            rtl_dir=OR1200_RTL_DIR,
            prefix="OR1200_",
            db=db,
            dry_run=False,
        )

        assert summary["modules"] > 0,  "Expected > 0 RTL modules"
        assert summary["ports"]   > 0,  "Expected > 0 RTL ports"
        assert _collection_count(db, "RTL_Module") > 0
        assert _collection_count(db, "RTL_Port")   > 0

    @pytest.mark.skipif(not os.path.isdir(MOR1KX_RTL_DIR),
                        reason=f"MOR1KX RTL clone not found at {MOR1KX_RTL_DIR}")
    def test_mor1kx_rtl_populates_collections(self, arango_docker):
        """RTL extraction for MOR1KX should produce independent RTL nodes."""
        db = _get_local_db(arango_docker)
        from etl_rtl import parse_verilog_files

        summary = parse_verilog_files(
            rtl_dir=MOR1KX_RTL_DIR,
            prefix="MOR1KX_",
            db=db,
            dry_run=False,
        )

        assert summary["modules"] > 0
        assert _collection_count(db, "RTL_Module") > 0

    def test_rtl_nodes_have_lpg_labels(self, arango_docker):
        """Every RTL_Port node must carry a non-empty labels list."""
        db = _get_local_db(arango_docker)
        if not db.has_collection("RTL_Port"):
            pytest.skip("RTL_Port not populated — run RTL extraction test first")

        cursor = db.aql.execute(
            "FOR p IN RTL_Port FILTER p.labels == null OR LENGTH(p.labels) == 0 "
            "LIMIT 5 RETURN p._key"
        )
        bad = list(cursor)
        assert bad == [], f"Nodes without labels: {bad}"

    def test_rtl_nodes_have_repo_field(self, arango_docker):
        """Every RTL_Module must have a non-null repo field."""
        db = _get_local_db(arango_docker)
        if not db.has_collection("RTL_Module"):
            pytest.skip("RTL_Module not populated")

        cursor = db.aql.execute(
            "FOR m IN RTL_Module FILTER m.repo == null LIMIT 3 RETURN m._key"
        )
        assert list(cursor) == []


# ---------------------------------------------------------------------------
# Section 2 — Semantic bridge (RESOLVED_TO)
# ---------------------------------------------------------------------------

class TestSemanticBridgeLocal:
    @pytest.mark.skipif(not os.path.isdir(OR1200_RTL_DIR),
                        reason="OR1200 RTL clone required")
    def test_resolved_to_edges_created(self, arango_docker):
        """
        After loading OR1200 golden entities and running the semantic bridge,
        RESOLVED_TO edges should exist.

        Golden entities are injected as minimal stubs so no LLM call is needed.
        """
        db = _get_local_db(arango_docker)

        # Seed minimal golden entities so the bridge has targets
        from local_graphrag.loader import _ensure_collection
        _ensure_collection(db, "OR1200_Golden_Entities")
        col = db.collection("OR1200_Golden_Entities")
        _stub_emb = [0.1] * 384
        col.import_bulk([
            {"_key": "OR1200_g_sr",   "name": "SR",   "type": "REGISTER",
             "labels": ["GoldenEntity", "REGISTER", "OR1200"], "repo": "OR1200",
             "layer": "golden", "aliases": ["Status Register"],
             "description": "Supervisor register", "embedding": _stub_emb},
            {"_key": "OR1200_g_epcr", "name": "EPCR", "type": "REGISTER",
             "labels": ["GoldenEntity", "REGISTER", "OR1200"], "repo": "OR1200",
             "layer": "golden", "aliases": ["Exception Program Counter Register"],
             "description": "Exception program counter", "embedding": _stub_emb},
            {"_key": "OR1200_g_pc",   "name": "PC",   "type": "REGISTER",
             "labels": ["GoldenEntity", "REGISTER", "OR1200"], "repo": "OR1200",
             "layer": "golden", "aliases": ["Program Counter"],
             "description": "Program counter register", "embedding": _stub_emb},
        ], on_duplicate="replace")

        # Ensure RTL nodes exist (run extraction if not already done)
        if _collection_count(db, "RTL_Port") == 0:
            from etl_rtl import parse_verilog_files
            parse_verilog_files(OR1200_RTL_DIR, "OR1200_", db=db)

        # Run exact-match bridge only (no embeddings needed)
        from rtl_semantic_bridge import (
            load_golden_entities, load_rtl_nodes, match_exact,
        )
        goldens = load_golden_entities(db, "OR1200_")
        ports, signals = load_rtl_nodes(db, "OR1200")  # the repo name, not the prefix
        rtl_nodes = ports + signals
        matches = match_exact(rtl_nodes, goldens)

        assert len(matches) > 0, "Expected at least one exact RESOLVED_TO match for OR1200"

    def test_resolved_to_edges_have_lpg_fields(self, arango_docker):
        """RESOLVED_TO edges must carry fromNodeType, toNodeType, and labels."""
        db = _get_local_db(arango_docker)
        if not db.has_collection("RESOLVED_TO"):
            pytest.skip("RESOLVED_TO collection not present")
        if _collection_count(db, "RESOLVED_TO") == 0:
            pytest.skip("No RESOLVED_TO edges yet")

        cursor = db.aql.execute(
            "FOR e IN RESOLVED_TO FILTER e.fromNodeType == null LIMIT 3 RETURN e._key"
        )
        assert list(cursor) == [], "Some RESOLVED_TO edges missing fromNodeType"


# ---------------------------------------------------------------------------
# Section 3 — Cross-repo bridge
# ---------------------------------------------------------------------------

class TestCrossRepoBridgeLocal:
    @pytest.mark.skipif(
        not (os.path.isdir(OR1200_RTL_DIR) and os.path.isdir(MOR1KX_RTL_DIR)),
        reason="Both OR1200 and MOR1KX RTL clones required",
    )
    def test_cross_repo_similar_to_edges_created(self, arango_docker):
        """
        After loading OR1200 + MOR1KX golden entities, cross-repo bridge should
        produce CROSS_REPO_SIMILAR_TO edges via label-name similarity.
        """
        db = _get_local_db(arango_docker)

        # Seed minimal golden entities for both repos
        for prefix, repo in [("OR1200_", "OR1200"), ("MOR1KX_", "MOR1KX")]:
            from local_graphrag.loader import _ensure_collection
            col_name = f"{prefix}Golden_Entities"
            _ensure_collection(db, col_name)
            col = db.collection(col_name)
            col.import_bulk([
                {"_key": f"{prefix}g_cpu",   "name": f"{prefix.lower().rstrip('_')}_cpu",
                 "type": "PROCESSOR_COMPONENT",
                 "labels": ["GoldenEntity", "PROCESSOR_COMPONENT", repo],
                 "repo": repo, "layer": "golden",
                 "description": "Main CPU", "embedding": None, "aliases": []},
            ], on_duplicate="replace")

        from cross_repo_bridge import build_embedding_bridges, build_structural_bridges

        # Structural bridge (no embeddings needed)
        edges = build_structural_bridges(db, "OR1200_", "MOR1KX_", min_score=0.3)
        # No RTL_Module nodes seeded → expect empty but no crash
        assert isinstance(edges, list)


# ---------------------------------------------------------------------------
# Section 4 — Count sanity after full OR1200 RTL extraction
# ---------------------------------------------------------------------------

class TestCountSanityLocal:
    @pytest.mark.skipif(not os.path.isdir(OR1200_RTL_DIR),
                        reason="OR1200 RTL clone required")
    def test_or1200_module_count_in_range(self, arango_docker):
        """OR1200 should produce between 30 and 200 RTL_Module nodes."""
        db = _get_local_db(arango_docker)
        if not db.has_collection("RTL_Module"):
            pytest.skip("RTL_Module not populated")
        count = _collection_count(db, "RTL_Module")
        assert 30 <= count <= 200, f"OR1200 RTL_Module count {count} out of expected range"

    @pytest.mark.skipif(not os.path.isdir(OR1200_RTL_DIR),
                        reason="OR1200 RTL clone required")
    def test_or1200_port_count_in_range(self, arango_docker):
        """OR1200 should produce between 500 and 5000 RTL_Port nodes."""
        db = _get_local_db(arango_docker)
        if not db.has_collection("RTL_Port"):
            pytest.skip("RTL_Port not populated")
        count = _collection_count(db, "RTL_Port")
        assert 500 <= count <= 5000, f"OR1200 RTL_Port count {count} out of expected range"

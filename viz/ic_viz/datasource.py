"""
datasource.py — the query layer behind ChronoGraph.

Both sources expose the SAME contract so the frontend never changes:
    repos()                                  -> project cards + timeline bounds
    timeline()                               -> epochs (as bands) + lineage ribbons
    slice(ts, repos, projection, opts)       -> cytoscape elements at time T
    provenance(entity_id)                    -> text + verilog + structure + relations
    source(kind, ref)                        -> full chunk text or verilog file
    search(q, repos)                         -> quick node lookup

SnapshotSource reads the offline snapshot.json (default). ArangoSource runs the
equivalent AQL against the live temporal DB; select it with ARANGO creds present.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict, Counter

OPEN_TS = 9999999999

EPOCH_COLORS = {
    "initial_commit": "#6b7280",
    "development":    "#3b82f6",
    "major_refactor": "#e0724a",
    "milestone_tag":  "#22c55e",
    "other":          "#8b8f98",
}


# ==========================================================================
# Highlight helper — find every (case-insensitive) span of a term in text
# ==========================================================================
def _spans(text: str, terms) -> list:
    if not text or not terms:
        return []
    out = []
    for term in {t for t in terms if t}:
        for m in re.finditer(re.escape(term), text, flags=re.IGNORECASE):
            out.append({"start": m.start(), "end": m.end(), "term": term})
    out.sort(key=lambda s: s["start"])
    return out


def cross_edges_dynamic(concept_nodes, parent):
    """Connect nodes across repos that share a functional concept, at the
    current time slice. Lineage-adjacent repos -> EVOLVED_FROM (directed
    child->parent); otherwise SIMILAR_TO. Always time-correct because it
    operates on whatever versions are active in this slice."""
    SKIP = {"", "defines", "top", "tb", "test"}
    by_concept = defaultdict(lambda: defaultdict(list))
    for n in concept_nodes:
        c = n.get("concept")
        if c and c not in SKIP:
            by_concept[c][n["repo"]].append(n["id"])
    out, seen = [], set()
    for concept, repo_map in by_concept.items():
        present = list(repo_map)
        for i, ra in enumerate(present):
            for rb in present[i + 1:]:
                a, b = repo_map[ra][0], repo_map[rb][0]
                if parent.get(ra) == rb:
                    etype, src, dst = "CROSS_REPO_EVOLVED_FROM", a, b
                elif parent.get(rb) == ra:
                    etype, src, dst = "CROSS_REPO_EVOLVED_FROM", b, a
                else:
                    etype, src, dst = "CROSS_REPO_SIMILAR_TO", a, b
                if (src, dst) in seen:
                    continue
                seen.add((src, dst))
                out.append({"data": {
                    "id": f"x::{src}::{dst}", "source": src, "target": dst,
                    "etype": etype, "cross": True,
                    "label": "evolved from" if etype.endswith("EVOLVED_FROM") else "similar concept",
                    "concept": concept,
                }})
    return out


# ==========================================================================
# Offline snapshot source
# ==========================================================================
class SnapshotSource:
    kind = "snapshot"

    def __init__(self, path: str):
        with open(path) as f:
            self.s = json.load(f)
        self.meta = self.s["meta"]
        self._index()

    # ---- indexing --------------------------------------------------------
    def _index(self):
        t = self.s["temporal"]
        st = self.s["structural"]

        self.repos_by_name = {r["name"]: r for r in self.s["repos"]}
        self.modules = t["modules"]
        self.epochs = t["epochs"]
        self.commits = {c["id"]: c for c in t["commits"]}

        # module version lookup + epoch fill
        self._fill_epoch_ends()

        # commit that introduced each module (by valid_from_commit within repo)
        self.commit_by_local = {}
        for c in t["commits"]:
            local = c["id"].split("__", 1)[1]
            self.commit_by_local[(c["repo"], local)] = c

        # structural
        self.st_modules = st["modules"]                    # key -> {label,file,code}
        self.chunks = st["chunks"]                         # key -> {doc_title,text}
        self.ports_by_mod = defaultdict(list)
        for e in st["has_port"]:
            self.ports_by_mod[e["from"].split("/")[-1]].append(e["to"].split("/")[-1])
        self.signals_by_mod = defaultdict(list)
        for e in st["has_signal"]:
            self.signals_by_mod[e["from"].split("/")[-1]].append(e["to"].split("/")[-1])
        self.fsms_by_mod = defaultdict(list)
        for e in st["has_fsm"]:
            self.fsms_by_mod[e["from"].split("/")[-1]].append(e["to"].split("/")[-1])
        self.port_nodes = st["ports"]
        self.signal_nodes = st["signals"]
        self.fsm_nodes = st["fsms"]
        self.depends_on = st["depends_on"]
        self.documented_by = defaultdict(list)
        for d in st["documented_by"]:
            self.documented_by[d["module_id"]].append(d)

        # consolidated
        self.cons_entities = {e["id"]: e for e in self.s["consolidated"]["entities"]}
        self.cons_by_modkey = {e["module_key"]: e for e in self.s["consolidated"]["entities"]}
        self.cons_relations = self.s["consolidated"]["relations"]

        # cross-repo
        self.cross_repo = self.s["cross_repo"]

    def _fill_epoch_ends(self):
        """Fill null end_ts with the next epoch's start (per repo) or repo ts_max."""
        by_repo = defaultdict(list)
        for e in self.epochs:
            by_repo[e["repo"]].append(e)
        for repo, eps in by_repo.items():
            eps.sort(key=lambda e: e.get("start_ts") or 0)
            rmax = (self.repos_by_name.get(repo) or {}).get("ts_max") or self.meta["ts_max"]
            for i, e in enumerate(eps):
                if not e.get("end_ts"):
                    e["end_ts"] = eps[i + 1]["start_ts"] if i + 1 < len(eps) else rmax
        self.epochs_by_repo = by_repo

    # ---- public API ------------------------------------------------------
    def repos(self):
        return {"repos": self.s["repos"], "meta": self.meta}

    def timeline(self):
        epochs = [{
            "id": e["id"], "repo": e["repo"], "epoch_type": e["epoch_type"],
            "label": e["label"], "start_ts": e.get("start_ts"), "end_ts": e.get("end_ts"),
            "git_tag": e.get("git_tag"), "color": EPOCH_COLORS.get(e["epoch_type"], "#888"),
        } for e in self.epochs if e.get("start_ts")]

        # lineage ribbons between repos (aggregated by repo pair)
        ribbons = []
        seen = set()
        for e in self.cross_repo:
            if e["type"] != "CROSS_REPO_EVOLVED_FROM":
                continue
            pair = (e["from_repo"], e["to_repo"])
            if pair in seen:
                continue
            seen.add(pair)
            ribbons.append({"from_repo": e["from_repo"], "to_repo": e["to_repo"],
                            "type": "evolved_from"})
        return {
            "ts_min": self.meta["ts_min"], "ts_max": self.meta["ts_max"],
            "repos": self.s["repos"], "epochs": epochs,
            "lineage": ribbons, "epoch_colors": EPOCH_COLORS,
        }

    def epoch_at(self, repo, ts):
        for e in self.epochs_by_repo.get(repo, []):
            if (e.get("start_ts") or 0) <= ts < (e.get("end_ts") or OPEN_TS):
                return e
        eps = self.epochs_by_repo.get(repo, [])
        return eps[-1] if eps and ts >= (eps[-1].get("start_ts") or 0) else None

    def _active_modules(self, ts, repos):
        return [m for m in self.modules
                if m["repo"] in repos and (m["valid_from_ts"] or 0) <= ts < (m["valid_to_ts"] or OPEN_TS)]

    def slice(self, ts, repos, projection="traceability", opts=None):
        opts = opts or {}
        repos = set(repos)
        if projection == "consolidated":
            return self._slice_consolidated(ts, repos, opts)
        return self._slice_traceability(ts, repos, opts)

    def _slice_traceability(self, ts, repos, opts):
        active = self._active_modules(ts, repos)
        active_ids = {m["id"] for m in active}
        # dedupe module identity per (repo,label): keep the active version
        nodes, edges = [], []
        per_repo = defaultdict(int)

        for m in active:
            per_repo[m["repo"]] += 1
            # text provenance is keyed by structural module key == module label
            has_doc = m["repo"] == "or1200" and m["label"] in self.documented_by
            nodes.append({"data": {
                "id": m["id"], "label": m["label"], "repo": m["repo"],
                "ntype": "module", "concept": m.get("concept"),
                "epoch": m.get("epoch"),
                "has_provenance": bool(has_doc),
                "parent": f"repo::{m['repo']}",
            }})

        # repo compound parents
        for r in repos:
            if per_repo.get(r):
                rc = self.repos_by_name.get(r, {})
                nodes.append({"data": {"id": f"repo::{r}", "label": rc.get("canonical", r),
                                       "ntype": "repo", "repo": r}})

        # DEPENDS_ON (structural, or1200) among active modules
        if "or1200" in repos:
            label_to_id = {(m["repo"], m["label"]): m["id"] for m in active if m["repo"] == "or1200"}
            for e in self.depends_on:
                fk, tk = e["from"].split("/")[-1], e["to"].split("/")[-1]
                fid = label_to_id.get(("or1200", fk))
                tid = label_to_id.get(("or1200", tk))
                if fid and tid:
                    edges.append({"data": {"id": f"dep::{fk}::{tk}", "source": fid, "target": tid,
                                           "etype": "DEPENDS_ON", "label": "instantiates"}})

        # cross-repo edges among active modules (time-correct: by concept)
        edges += self._cross_edges_dynamic(
            [{"id": m["id"], "repo": m["repo"], "concept": m.get("concept")} for m in active])

        return {
            "ts": ts, "projection": "traceability", "repos": sorted(repos),
            "nodes": nodes, "edges": edges,
            "epoch_context": {r: self.epoch_at(r, ts) for r in repos},
            "stats": {"nodes": len(active), "edges": len(edges), "per_repo": dict(per_repo)},
        }

    def _slice_consolidated(self, ts, repos, opts):
        nodes, edges = [], []
        per_repo = defaultdict(int)
        included = set()

        # or1200 -> consolidated entities (with cross-source badges)
        if "or1200" in repos:
            active_or1200 = {m["label"] for m in self._active_modules(ts, {"or1200"})}
            for ent in self.cons_entities.values():
                if ent["label"] not in active_or1200:
                    continue
                included.add(ent["id"])
                per_repo["or1200"] += 1
                nodes.append({"data": {
                    "id": ent["id"], "label": ent["label"], "repo": "or1200",
                    "ntype": "consolidated", "entity_type": ent["entity_type"],
                    "concept": ent.get("concept"),
                    "text_refs": len(ent["chunk_ids"]),
                    "verilog_refs": 1 if ent.get("file") else 0,
                    "port_count": ent.get("port_count", 0),
                    "signal_count": ent.get("signal_count", 0),
                    "has_provenance": True,
                    "parent": "repo::or1200",
                }})
            if per_repo.get("or1200"):
                nodes.append({"data": {"id": "repo::or1200", "label": "openrisc/or1200 (consolidated)",
                                       "ntype": "repo", "repo": "or1200"}})
            for rel in self.cons_relations:
                if rel["from"] in included and rel["to"] in included:
                    edges.append({"data": {
                        "id": f"crel::{rel['from']}::{rel['to']}",
                        "source": rel["from"], "target": rel["to"], "etype": "DEPENDS_ON",
                        "verilog_evidence": rel.get("verilog_evidence", False),
                        "text_evidence": len(rel.get("text_evidence", [])),
                        "label": self._rel_evidence_label(rel),
                    }})

        # other repos -> module nodes (no consolidated detail) so cross-project still works
        other = repos - {"or1200"}
        if other:
            active = self._active_modules(ts, other)
            for m in active:
                included.add(m["id"])
                per_repo[m["repo"]] += 1
                nodes.append({"data": {
                    "id": m["id"], "label": m["label"], "repo": m["repo"],
                    "ntype": "module", "concept": m.get("concept"),
                    "parent": f"repo::{m['repo']}", "has_provenance": False,
                }})
            for r in other:
                if per_repo.get(r):
                    rc = self.repos_by_name.get(r, {})
                    nodes.append({"data": {"id": f"repo::{r}", "label": rc.get("canonical", r),
                                           "ntype": "repo", "repo": r}})

        # cross-repo edges (time-correct: by concept) across everything included
        concept_nodes = []
        for n in nodes:
            d = n["data"]
            if d.get("ntype") in ("module", "consolidated") and d.get("concept"):
                concept_nodes.append({"id": d["id"], "repo": d["repo"], "concept": d["concept"]})
        edges += self._cross_edges_dynamic(concept_nodes)

        return {
            "ts": ts, "projection": "consolidated", "repos": sorted(repos),
            "nodes": nodes, "edges": edges,
            "epoch_context": {r: self.epoch_at(r, ts) for r in repos},
            "stats": {"nodes": sum(per_repo.values()), "edges": len(edges), "per_repo": dict(per_repo)},
        }

    @staticmethod
    def _rel_evidence_label(rel):
        bits = []
        if rel.get("verilog_evidence"):
            bits.append("verilog")
        n = len(rel.get("text_evidence", []))
        if n:
            bits.append(f"{n} doc")
        return " + ".join(bits) if bits else "ref"

    def _cross_edges_dynamic(self, concept_nodes):
        parent = {r["name"]: r.get("lineage_parent") for r in self.s["repos"]}
        return cross_edges_dynamic(concept_nodes, parent)

    # ---- provenance ------------------------------------------------------
    def provenance(self, entity_id):
        """Accepts a temporal module uid (repo__key) or consolidated id (or1200__modkey)."""
        repo = entity_id.split("__", 1)[0]
        local = entity_id.split("__", 1)[1] if "__" in entity_id else entity_id

        # resolve to a module label + structural key
        label, modkey = None, None
        cons = self.cons_entities.get(entity_id)
        if cons:
            label, modkey = cons["label"], cons["module_key"]
        else:
            mv = next((m for m in self.modules if m["id"] == entity_id), None)
            if mv:
                label = mv["label"]
                modkey = label if label in self.st_modules else None

        result = {"id": entity_id, "repo": repo, "label": label,
                  "text": [], "verilog": None, "structure": {}, "relations": [],
                  "temporal": None, "cross_repo": []}

        # temporal facts
        mv = next((m for m in self.modules if m["id"] == entity_id), None)
        if not mv and cons:
            mv = next((m for m in self.modules if m["repo"] == "or1200" and m["label"] == label), None)
        if mv:
            intro = self.commit_by_local.get((mv["repo"], mv.get("valid_from_commit") or ""))
            result["temporal"] = {
                "valid_from_ts": mv["valid_from_ts"], "valid_to_ts": mv["valid_to_ts"],
                "epoch": mv.get("epoch"), "file": mv.get("file"),
                "still_current": (mv["valid_to_ts"] or OPEN_TS) >= OPEN_TS,
                "introduced_by": ({"sha": (mv.get("valid_from_commit") or "")[:10],
                                   "author": intro.get("author"), "message": intro.get("message")}
                                  if intro else None),
            }

        # verilog (or1200 has code on disk)
        if modkey and modkey in self.st_modules:
            m = self.st_modules[modkey]
            terms = sorted({d["matched_term"] for d in self.documented_by.get(modkey, []) if d.get("matched_term")})
            terms = terms or [label]
            code = m.get("code") or ""
            result["verilog"] = {
                "file": m.get("file"), "ref": modkey, "terms": [label],
                "code": code, "spans": _spans(code, [label]),
                "lang": "verilog",
            }
            # structure
            result["structure"] = {
                "ports": [self._port(p) for p in self.ports_by_mod.get(modkey, [])][:200],
                "signals": [self._signal(s) for s in self.signals_by_mod.get(modkey, [])][:200],
                "fsms": [self.fsm_nodes.get(f, {"label": f}) for f in self.fsms_by_mod.get(modkey, [])],
            }
            # text provenance: each chunk with highlighted matched term
            for d in self.documented_by.get(modkey, []):
                ch = self.chunks.get(d["chunk_id"])
                if not ch:
                    continue
                term = d.get("matched_term") or label
                result["text"].append({
                    "chunk_id": d["chunk_id"], "doc_title": ch["doc_title"],
                    "matched_term": term, "score": d.get("score"),
                    "text": ch["text"],
                    "spans": _spans(ch["text"], [term]),
                })

        # consolidated relations touching this entity
        cid = entity_id if entity_id in self.cons_entities else (f"or1200__{modkey}" if modkey else None)
        if cid:
            for rel in self.cons_relations:
                if rel["from"] == cid or rel["to"] == cid:
                    other = rel["to"] if rel["from"] == cid else rel["from"]
                    ent = self.cons_entities.get(other, {})
                    result["relations"].append({
                        "direction": "out" if rel["from"] == cid else "in",
                        "other_id": other, "other_label": ent.get("label", other),
                        "type": rel["type"], "verilog_evidence": rel.get("verilog_evidence"),
                        "text_evidence": rel.get("text_evidence", []),
                        "instance_names": rel.get("instance_names"),
                    })

        # cross-repo links: sibling modules in other repos sharing this concept
        concept = (mv or {}).get("concept") or (cons or {}).get("concept")
        if concept:
            parent = {r["name"]: r.get("lineage_parent") for r in self.s["repos"]}
            seen_repo = set()
            for m in self.modules:
                if m["repo"] == repo or m.get("concept") != concept:
                    continue
                if m["repo"] in seen_repo:
                    continue
                seen_repo.add(m["repo"])
                lineage = parent.get(repo) == m["repo"] or parent.get(m["repo"]) == repo
                result["cross_repo"].append({
                    "repo": m["repo"], "label": m["label"], "concept": concept,
                    "type": "CROSS_REPO_EVOLVED_FROM" if lineage else "CROSS_REPO_SIMILAR_TO",
                    "still_current": (m["valid_to_ts"] or OPEN_TS) >= OPEN_TS,
                })

        return result

    def _port(self, key):
        p = self.port_nodes.get(key, {})
        return {"key": key, "label": p.get("label", key), "direction": p.get("direction"),
                "expanded_name": p.get("expanded_name")}

    def _signal(self, key):
        s = self.signal_nodes.get(key, {})
        return {"key": key, "label": s.get("label", key), "datatype": s.get("datatype"),
                "expanded_name": s.get("expanded_name")}

    # ---- source viewer ---------------------------------------------------
    def source(self, kind, ref, terms=None):
        if kind == "chunk":
            ch = self.chunks.get(ref)
            if not ch:
                return {"error": "chunk not found"}
            return {"kind": "chunk", "title": ch["doc_title"], "text": ch["text"],
                    "spans": _spans(ch["text"], terms or [])}
        if kind == "verilog":
            m = self.st_modules.get(ref)
            if not m:
                return {"error": "module not found"}
            return {"kind": "verilog", "title": m.get("file"), "text": m.get("code", ""),
                    "spans": _spans(m.get("code", ""), terms or [])}
        return {"error": "unknown kind"}

    def search(self, q, repos=None):
        q = (q or "").lower().strip()
        if not q:
            return {"results": []}
        repos = set(repos) if repos else set(self.repos_by_name)
        hits, seen = [], set()
        for m in self.modules:
            if m["repo"] in repos and q in m["label"].lower():
                key = (m["repo"], m["label"])
                if key in seen:
                    continue
                seen.add(key)
                hits.append({"id": m["id"], "label": m["label"], "repo": m["repo"], "ntype": "module"})
                if len(hits) >= 40:
                    break
        return {"results": hits}


# ==========================================================================
# Live ArangoDB source — same contract, real AQL.
#
# Live schema notes (verified 2026-07 against ic-knowledge-graph-temporal):
#   * TWO RTL_Module namespaces:
#       - temporal versions: hash _key, repo like "openrisc/or1200.git",
#         bitemporal valid_from_ts / valid_to_ts, design_epoch
#       - deep structural:  _key like "OR1200_or1200_cpu", repo like "OR1200",
#         code_content, HAS_PORT / HAS_SIGNAL / DEPENDS_ON hang off these
#       joined by SNAPSHOT_OF (temporal -> deep).
#   * Text provenance: {P}_Golden_Entities -Consolidates-> {P}_Entities
#       -MentionedIn-> {P}_Chunks (chunk.text holds the passage).
#   * RTL_Port/RTL_Signal -RESOLVED_TO-> Golden_Entities (score, method, rtl_name).
#   * {P}_Golden_Relations carry context, evidence_count, source_chunks[].
#   * CROSS_REPO_SIMILAR_TO / CROSS_REPO_EVOLVED_FROM link Golden_Entities
#     across repos (embedding / rule based).
# ==========================================================================
REPO_CONFIG = {
    "or1200":     {"short": "or1200",  "canonical": "openrisc/or1200.git",         "prefix": "OR1200",     "color": "#4f8cff", "lineage_parent": None},
    "mor1kx":     {"short": "mor1kx",  "canonical": "openrisc/mor1kx.git",         "prefix": "MOR1KX",     "color": "#22c1a4", "lineage_parent": "or1200"},
    "marocchino": {"short": "marocc.", "canonical": "openrisc/or1k_marocchino.git","prefix": "MAROCCHINO", "color": "#e0724a", "lineage_parent": "mor1kx"},
    "ibex":       {"short": "ibex",    "canonical": "lowRISC/ibex.git",            "prefix": "IBEX",       "color": "#b47cff", "lineage_parent": None},
}
_CANON_TO_NAME = {v["canonical"]: k for k, v in REPO_CONFIG.items()}
_PREFIX_TO_NAME = {v["prefix"]: k for k, v in REPO_CONFIG.items()}


class ArangoSource:
    kind = "arango"

    def __init__(self, db):
        self.db = db  # python-arango StandardDatabase
        self._epochs = None          # cached: epochs with filled end_ts, by repo name
        self._bounds = None          # cached: {repo: {ts_min, ts_max}}
        self._deep_modules = None    # cached: {(repo_name, module_label): deep_key}

    def _q(self, aql, **binds):
        return list(self.db.aql.execute(aql, bind_vars=binds))

    # ---- cached metadata ---------------------------------------------------
    def _load_epochs(self):
        if self._epochs is not None:
            return
        rows = self._q("""
            FOR e IN DesignEpoch
              SORT e.start_ts ASC
              RETURN {id: e._key, repo: e.repo, epoch_type: e.epoch_type,
                      label: e.label, start_ts: e.start_ts, end_ts: e.end_ts,
                      git_tag: e.git_tag}
        """)
        bounds = {r["repo"]: r for r in self._q("""
            FOR c IN GitCommit
              COLLECT repo = c.repo AGGREGATE lo = MIN(c.valid_from_ts), hi = MAX(c.valid_from_ts)
              RETURN {repo, lo, hi}
        """)}
        by_repo = defaultdict(list)
        for e in rows:
            name = _CANON_TO_NAME.get(e["repo"])
            if not name or not e.get("start_ts"):
                continue
            e["repo"] = name
            by_repo[name].append(e)
        for name, eps in by_repo.items():
            canon = REPO_CONFIG[name]["canonical"]
            rmax = (bounds.get(canon) or {}).get("hi") or 0
            for i, e in enumerate(eps):
                if not e.get("end_ts"):
                    e["end_ts"] = eps[i + 1]["start_ts"] if i + 1 < len(eps) else rmax
        self._epochs = by_repo
        self._bounds = {name: {"ts_min": (bounds.get(cfg["canonical"]) or {}).get("lo"),
                               "ts_max": (bounds.get(cfg["canonical"]) or {}).get("hi")}
                        for name, cfg in REPO_CONFIG.items()}

    def _load_deep_modules(self):
        """Map (repo_name, module_label) -> deep structural module _key."""
        if self._deep_modules is not None:
            return
        rows = self._q("""
            FOR m IN RTL_Module
              FILTER m.repo IN @prefixes
              RETURN {key: m._key, repo: m.repo, name: m.name}
        """, prefixes=[c["prefix"] for c in REPO_CONFIG.values()])
        self._deep_modules = {}
        for r in rows:
            name = _PREFIX_TO_NAME.get(r["repo"])
            if name and r.get("name"):
                self._deep_modules[(name, r["name"])] = r["key"]

    def epoch_at(self, repo, ts):
        self._load_epochs()
        eps = self._epochs.get(repo, [])
        for e in eps:
            if (e.get("start_ts") or 0) <= ts < (e.get("end_ts") or OPEN_TS):
                return e
        return eps[-1] if eps and ts >= (eps[-1].get("start_ts") or 0) else None

    # ---- public API ----------------------------------------------------------
    def repos(self):
        self._load_epochs()
        counts = {r["repo"]: r["n"] for r in self._q("""
            FOR c IN GitCommit COLLECT repo = c.repo WITH COUNT INTO n RETURN {repo, n}
        """)}
        mods = {r["repo"]: r["n"] for r in self._q("""
            FOR m IN RTL_Module FILTER m.repo IN @canon
              COLLECT repo = m.repo WITH COUNT INTO n RETURN {repo, n}
        """, canon=[c["canonical"] for c in REPO_CONFIG.values()])}
        cards, all_lo, all_hi = [], [], []
        for name, cfg in REPO_CONFIG.items():
            b = self._bounds.get(name, {})
            if b.get("ts_min"):
                all_lo.append(b["ts_min"]); all_hi.append(b["ts_max"])
            cards.append({
                "name": name, "short": cfg["short"], "canonical": cfg["canonical"],
                "color": cfg["color"], "lineage_parent": cfg["lineage_parent"],
                "epoch_count": len(self._epochs.get(name, [])),
                "commit_count": counts.get(cfg["canonical"], 0),
                "module_version_count": mods.get(cfg["canonical"], 0),
                "ts_min": b.get("ts_min"), "ts_max": b.get("ts_max"),
                "has_structural": True,
            })
        return {"repos": cards,
                "meta": {"source": "arango", "ts_min": min(all_lo) if all_lo else 0,
                         "ts_max": max(all_hi) if all_hi else 0, "open_ts": OPEN_TS}}

    def timeline(self):
        self._load_epochs()
        epochs = []
        for name, eps in self._epochs.items():
            for e in eps:
                epochs.append({**e, "color": EPOCH_COLORS.get(e["epoch_type"], "#888")})
        lineage = [{"from_repo": name, "to_repo": cfg["lineage_parent"], "type": "evolved_from"}
                   for name, cfg in REPO_CONFIG.items() if cfg["lineage_parent"]]
        lo = [b["ts_min"] for b in self._bounds.values() if b.get("ts_min")]
        hi = [b["ts_max"] for b in self._bounds.values() if b.get("ts_max")]
        return {"ts_min": min(lo) if lo else 0, "ts_max": max(hi) if hi else 0,
                "repos": self.repos()["repos"], "epochs": epochs,
                "lineage": lineage, "epoch_colors": EPOCH_COLORS}

    # ---- slice ---------------------------------------------------------------
    def slice(self, ts, repos, projection="traceability", opts=None):
        repos = [r for r in repos if r in REPO_CONFIG]
        if projection == "consolidated":
            return self._slice_consolidated(ts, repos)
        return self._slice_traceability(ts, repos)

    def _active_modules(self, ts, repos):
        rows = self._q("""
            FOR m IN RTL_Module
              FILTER m.repo IN @canon
              FILTER m.valid_from_ts <= @ts AND m.valid_to_ts > @ts
              RETURN {id: m._key, label: m.label, repo: m.repo, epoch: m.design_epoch}
        """, canon=[REPO_CONFIG[r]["canonical"] for r in repos], ts=ts)
        # dedupe by (repo,label): replay can briefly hold two open versions
        out = {}
        for m in rows:
            m["repo"] = _CANON_TO_NAME[m["repo"]]
            out.setdefault((m["repo"], m["label"]), m)
        return list(out.values())

    def _slice_traceability(self, ts, repos):
        from .build_snapshot import _functional_concept
        self._load_deep_modules()
        active = self._active_modules(ts, repos)
        per_repo = defaultdict(int)
        nodes, edges = [], []
        label_to_id = {}
        for m in active:
            per_repo[m["repo"]] += 1
            label_to_id[(m["repo"], m["label"])] = m["id"]
            has_deep = (m["repo"], m["label"]) in self._deep_modules
            nodes.append({"data": {
                "id": m["id"], "label": m["label"], "repo": m["repo"], "ntype": "module",
                "concept": _functional_concept(m["label"]), "epoch": m.get("epoch"),
                "has_provenance": has_deep, "parent": f"repo::{m['repo']}",
            }})
        for r in repos:
            if per_repo.get(r):
                nodes.append({"data": {"id": f"repo::{r}", "label": REPO_CONFIG[r]["canonical"],
                                       "ntype": "repo", "repo": r}})
        # deep DEPENDS_ON, mapped onto whichever module versions are active at T
        deps = self._q("""
            FOR e IN DEPENDS_ON
              FILTER e.repo IN @prefixes
              RETURN {f: PARSE_IDENTIFIER(e._from).key, t: PARSE_IDENTIFIER(e._to).key, repo: e.repo}
        """, prefixes=[REPO_CONFIG[r]["prefix"] for r in repos])
        for d in deps:
            name = _PREFIX_TO_NAME.get(d["repo"])
            if not name:
                continue
            pref = REPO_CONFIG[name]["prefix"] + "_"
            fl, tl = d["f"][len(pref):], d["t"][len(pref):]
            fid, tid = label_to_id.get((name, fl)), label_to_id.get((name, tl))
            if fid and tid:
                edges.append({"data": {"id": f"dep::{d['f']}::{d['t']}", "source": fid,
                                       "target": tid, "etype": "DEPENDS_ON", "label": "instantiates"}})
        edges += cross_edges_dynamic(
            [{"id": n["data"]["id"], "repo": n["data"]["repo"], "concept": n["data"].get("concept")}
             for n in nodes if n["data"]["ntype"] == "module"],
            {k: v["lineage_parent"] for k, v in REPO_CONFIG.items()})
        return {"ts": ts, "projection": "traceability", "repos": sorted(repos),
                "nodes": nodes, "edges": edges,
                "epoch_context": {r: self.epoch_at(r, ts) for r in repos},
                "stats": {"nodes": sum(per_repo.values()), "edges": len(edges),
                          "per_repo": dict(per_repo)}}

    def _slice_consolidated(self, ts, repos):
        """Golden-entity layer (live). Golden entities are atemporal; the time
        cursor still governs epoch context and the traceability view."""
        nodes, edges = [], []
        per_repo = defaultdict(int)
        included = set()
        for r in repos:
            p = REPO_CONFIG[r]["prefix"]
            ents = self._q(f"""
                FOR g IN {p}_Golden_Entities
                  LET text_refs = COUNT(
                    FOR c IN {p}_Consolidates FILTER c._from == g._id
                      FOR mi IN {p}_MentionedIn FILTER mi._from == c._to
                        RETURN DISTINCT mi._to)
                  LET rtl_refs = COUNT(FOR e IN RESOLVED_TO FILTER e._to == g._id RETURN 1)
                  RETURN {{id: g._id, name: g.name, etype: g.labels[1],
                          text_refs, rtl_refs}}
            """)
            for g in ents:
                included.add(g["id"])
                per_repo[r] += 1
                nodes.append({"data": {
                    "id": g["id"], "label": g["name"], "repo": r, "ntype": "consolidated",
                    "entity_type": (g.get("etype") or "").lower(),
                    "text_refs": g["text_refs"], "verilog_refs": g["rtl_refs"],
                    "port_count": g["rtl_refs"], "signal_count": g["text_refs"],
                    "has_provenance": True, "parent": f"repo::{r}",
                }})
            if per_repo.get(r):
                nodes.append({"data": {"id": f"repo::{r}",
                                       "label": REPO_CONFIG[r]["canonical"] + " (golden)",
                                       "ntype": "repo", "repo": r}})
            rels = self._q(f"""
                FOR e IN {p}_Golden_Relations
                  RETURN {{f: e._from, t: e._to, rtype: e.labels[0],
                          n_chunks: LENGTH(NOT_NULL(e.source_chunks, []))}}
            """)
            for rel in rels:
                if rel["f"] in included and rel["t"] in included:
                    edges.append({"data": {
                        "id": f"crel::{rel['f']}::{rel['t']}::{rel['rtype']}",
                        "source": rel["f"], "target": rel["t"], "etype": "DEPENDS_ON",
                        "verilog_evidence": False, "text_evidence": rel["n_chunks"],
                        "label": f"{(rel['rtype'] or 'rel').lower()} · {rel['n_chunks']} doc",
                    }})
        # real cross-repo edges between golden entities
        for coll, lab in (("CROSS_REPO_EVOLVED_FROM", "evolved from"),
                          ("CROSS_REPO_SIMILAR_TO", "similar")):
            for e in self._q(f"FOR e IN {coll} RETURN {{f: e._from, t: e._to, "
                             f"score: NOT_NULL(e.confidence, e.similarity_score)}}"):
                if e["f"] in included and e["t"] in included:
                    edges.append({"data": {
                        "id": f"x::{e['f']}::{e['t']}", "source": e["f"], "target": e["t"],
                        "etype": coll, "cross": True, "label": lab, "score": e.get("score")}})
        return {"ts": ts, "projection": "consolidated", "repos": sorted(repos),
                "nodes": nodes, "edges": edges,
                "epoch_context": {r: self.epoch_at(r, ts) for r in repos},
                "stats": {"nodes": sum(per_repo.values()), "edges": len(edges),
                          "per_repo": dict(per_repo)}}

    # ---- provenance ------------------------------------------------------
    def provenance(self, entity_id):
        if "_Golden_Entities/" in entity_id:
            return self._provenance_golden(entity_id)
        return self._provenance_module(entity_id)

    def _provenance_golden(self, gid):
        prefix = gid.split("_Golden_Entities/")[0]
        repo = _PREFIX_TO_NAME.get(prefix, prefix.lower())
        rows = self._q("RETURN DOCUMENT(@id)", id=gid)
        g = rows[0] if rows else None
        if not g:
            return {"id": gid, "error": "not found"}
        terms = [g.get("name")] + (g.get("aliases") or [])
        result = {"id": gid, "repo": repo, "label": g.get("name"),
                  "entity_type": (g.get("labels") or ["", ""])[1],
                  "description": g.get("description"),
                  "text": [], "verilog": None, "structure": {}, "relations": [],
                  "temporal": None, "cross_repo": []}
        # text: golden -> consolidates -> raw -> mentioned_in -> chunk
        chunks = self._q(f"""
            FOR c IN {prefix}_Consolidates FILTER c._from == @gid
              FOR mi IN {prefix}_MentionedIn FILTER mi._from == c._to
                LET ch = DOCUMENT(mi._to)
                RETURN DISTINCT {{chunk_id: ch._id, doc_title: ch.doc_basename,
                                 section: ch.section_header, text: ch.text}}
        """, gid=gid)
        for ch in chunks[:12]:
            sp = _spans(ch["text"] or "", terms)
            result["text"].append({
                "chunk_id": ch["chunk_id"],
                "doc_title": ch.get("section") or ch.get("doc_title") or "chunk",
                "matched_term": (sp[0]["term"] if sp else g.get("name")),
                "score": None, "text": ch["text"] or "", "spans": sp,
            })
        # RTL evidence: inbound RESOLVED_TO ports/signals
        rtl = self._q("""
            FOR e IN RESOLVED_TO FILTER e._to == @gid
              LET n = DOCUMENT(e._from)
              RETURN {name: n.name, expanded: n.expanded_name, direction: n.direction,
                      parent: n.parent_module, method: e.method, score: e.score}
        """, gid=gid)
        result["structure"] = {
            "ports": [{"key": r["name"], "label": f"{r['parent']}.{r['name']}",
                       "direction": r.get("direction"),
                       "expanded_name": f"{r.get('expanded') or ''} ({r.get('method')}, {r.get('score')})"}
                      for r in rtl][:200],
            "signals": [], "fsms": [],
        }
        # verilog: most common parent module among resolved RTL nodes
        parents = Counter(r["parent"] for r in rtl if r.get("parent"))
        if parents:
            mod_name = parents.most_common(1)[0][0]
            deep_key = f"{prefix}_{mod_name}"
            code_rows = self._q("LET m = DOCUMENT('RTL_Module', @k) "
                                "RETURN m == null ? null : {file: m.file, code: m.code_content}",
                                k=deep_key)
            if code_rows and code_rows[0]:
                names = [r["name"] for r in rtl if r.get("parent") == mod_name]
                result["verilog"] = {"file": code_rows[0]["file"], "ref": deep_key,
                                     "terms": names[:8],
                                     "code": code_rows[0]["code"] or "",
                                     "spans": _spans(code_rows[0]["code"] or "", names[:8]),
                                     "lang": "verilog"}
        # relations with real consolidated evidence
        rels = self._q(f"""
            FOR e IN {prefix}_Golden_Relations
              FILTER e._from == @gid OR e._to == @gid
              LET other = DOCUMENT(e._from == @gid ? e._to : e._from)
              RETURN {{dir: e._from == @gid ? "out" : "in", other_id: other._id,
                      other_label: other.name, rtype: e.labels[0], context: e.context,
                      source_chunks: NOT_NULL(e.source_chunks, [])}}
        """, gid=gid)
        for r in rels[:40]:
            result["relations"].append({
                "direction": r["dir"], "other_id": r["other_id"],
                "other_label": r["other_label"], "type": r["rtype"],
                "verilog_evidence": False, "text_evidence": r["source_chunks"],
                "instance_names": None, "context": r.get("context"),
            })
        # real cross-repo lineage
        xr = self._q("""
            LET ev = (FOR e IN CROSS_REPO_EVOLVED_FROM
                        FILTER e._from == @gid OR e._to == @gid
                        LET o = DOCUMENT(e._from == @gid ? e._to : e._from)
                        RETURN {id: o._id, label: o.name, type: "CROSS_REPO_EVOLVED_FROM",
                                score: e.confidence})
            LET sim = (FOR e IN CROSS_REPO_SIMILAR_TO
                        FILTER e._from == @gid OR e._to == @gid
                        LET o = DOCUMENT(e._from == @gid ? e._to : e._from)
                        RETURN {id: o._id, label: o.name, type: "CROSS_REPO_SIMILAR_TO",
                                score: e.similarity_score})
            RETURN APPEND(ev, sim)
        """, gid=gid)[0]
        for x in xr:
            xp = x["id"].split("_Golden_Entities/")[0]
            result["cross_repo"].append({
                "repo": _PREFIX_TO_NAME.get(xp, xp.lower()), "label": x["label"],
                "type": x["type"], "score": x.get("score"), "still_current": True})
        return result

    def _provenance_module(self, key):
        """Temporal module version: temporal facts + deep structure (prefix_label
        join, same as SNAPSHOT_OF) + concept text provenance via port
        RESOLVED_TO -> golden -> chunks."""
        rows = self._q("LET m = DOCUMENT('RTL_Module', @k) RETURN m", k=key)
        m = rows[0] if rows else None
        if not m:
            return {"id": key, "error": "not found"}
        repo = _CANON_TO_NAME.get(m.get("repo"), m.get("repo"))
        result = {"id": key, "repo": repo, "label": m.get("label"),
                  "text": [], "verilog": None, "structure": {}, "relations": [],
                  "temporal": None, "cross_repo": []}
        intro = None
        if m.get("valid_from_commit"):
            crows = self._q("LET c = DOCUMENT('GitCommit', @k) RETURN c", k=m["valid_from_commit"])
            if crows and crows[0]:
                md = crows[0].get("metadata") or {}
                intro = {"sha": m["valid_from_commit"][:10], "author": md.get("author"),
                         "message": (md.get("message") or "")[:280]}
        result["temporal"] = {
            "valid_from_ts": m.get("valid_from_ts"), "valid_to_ts": m.get("valid_to_ts"),
            "epoch": m.get("design_epoch"), "file": m.get("file"),
            "still_current": (m.get("valid_to_ts") or OPEN_TS) >= OPEN_TS,
            "introduced_by": intro,
        }
        # deep structural module
        prefix = REPO_CONFIG.get(repo, {}).get("prefix")
        if prefix:
            deep_key = f"{prefix}_{m.get('label')}"
            drows = self._q("LET d = DOCUMENT('RTL_Module', @k) RETURN d", k=deep_key)
            deep = drows[0] if drows else None
            if deep:
                result["verilog"] = {"file": deep.get("file"), "ref": deep_key,
                                     "terms": [m.get("label")],
                                     "code": deep.get("code_content") or "",
                                     "spans": _spans(deep.get("code_content") or "", [m.get("label")]),
                                     "lang": "verilog"}
                st = self._q("""
                    LET ports = (FOR e IN HAS_PORT FILTER e._from == @did
                                  LET p = DOCUMENT(e._to)
                                  RETURN {key: p._key, label: p.name, direction: p.direction,
                                          expanded_name: p.expanded_name})
                    LET sigs = (FOR e IN HAS_SIGNAL FILTER e._from == @did
                                  LET s = DOCUMENT(e._to)
                                  RETURN {key: s._key, label: s.name, expanded_name: s.expanded_name})
                    RETURN {ports: SLICE(ports, 0, 200), signals: SLICE(sigs, 0, 200)}
                """, did=f"RTL_Module/{deep_key}")[0]
                result["structure"] = {**st, "fsms": []}
                # concept text provenance: this module's ports -> golden -> chunks
                gold = self._q(f"""
                    FOR e IN RESOLVED_TO
                      FILTER STARTS_WITH(PARSE_IDENTIFIER(e._from).key, @pk)
                      SORT e.score DESC LIMIT 6
                      LET g = DOCUMENT(e._to)
                      LET ch = FIRST(
                        FOR c IN {prefix}_Consolidates FILTER c._from == g._id
                          FOR mi IN {prefix}_MentionedIn FILTER mi._from == c._to
                            RETURN DOCUMENT(mi._to))
                      FILTER ch != null
                      RETURN {{gname: g.name, rtl_name: e.rtl_name,
                              chunk_id: ch._id, doc_title: ch.doc_basename,
                              section: ch.section_header, text: ch.text}}
                """, pk=f"{deep_key}.")
                seen_chunks = set()
                for gr in gold:
                    if gr["chunk_id"] in seen_chunks:
                        continue
                    seen_chunks.add(gr["chunk_id"])
                    sp = _spans(gr["text"] or "", [gr["gname"], gr["rtl_name"]])
                    result["text"].append({
                        "chunk_id": gr["chunk_id"],
                        "doc_title": f"{gr.get('section') or gr.get('doc_title')} — via {gr['gname']}",
                        "matched_term": (sp[0]["term"] if sp else gr["gname"]),
                        "score": None, "text": gr["text"] or "", "spans": sp,
                    })
        # cross-repo by concept (dynamic, same semantics as snapshot)
        from .build_snapshot import _functional_concept
        concept = _functional_concept(m.get("label", ""))
        if concept:
            sibs = self._q("""
                FOR o IN RTL_Module
                  FILTER o.repo IN @canon AND o.repo != @own
                  FILTER o.valid_to_ts >= @open
                  RETURN DISTINCT {label: o.label, repo: o.repo}
            """, canon=[c["canonical"] for c in REPO_CONFIG.values()],
                 own=m.get("repo"), open=OPEN_TS)
            parent = {k: v["lineage_parent"] for k, v in REPO_CONFIG.items()}
            seen_repo = set()
            for s in sibs:
                if _functional_concept(s["label"]) != concept:
                    continue
                rname = _CANON_TO_NAME.get(s["repo"])
                if not rname or rname in seen_repo:
                    continue
                seen_repo.add(rname)
                lineage = parent.get(repo) == rname or parent.get(rname) == repo
                result["cross_repo"].append({
                    "repo": rname, "label": s["label"], "concept": concept,
                    "type": "CROSS_REPO_EVOLVED_FROM" if lineage else "CROSS_REPO_SIMILAR_TO",
                    "still_current": True})
        return result

    # ---- source viewer -----------------------------------------------------
    def source(self, kind, ref, terms=None):
        if kind == "chunk":
            rows = self._q("RETURN DOCUMENT(@id)", id=ref)
            ch = rows[0] if rows else None
            if not ch:
                return {"error": "chunk not found"}
            return {"kind": "chunk",
                    "title": ch.get("section_header") or ch.get("doc_basename") or ref,
                    "text": ch.get("text") or "",
                    "spans": _spans(ch.get("text") or "", terms or [])}
        if kind == "verilog":
            rows = self._q("LET m = DOCUMENT('RTL_Module', @k) RETURN m", k=ref)
            mod = rows[0] if rows else None
            if not mod:
                return {"error": "module not found"}
            return {"kind": "verilog", "title": mod.get("file") or ref,
                    "text": mod.get("code_content") or "",
                    "spans": _spans(mod.get("code_content") or "", terms or [])}
        return {"error": "unknown kind"}

    def search(self, q, repos=None):
        ql = (q or "").lower().strip()
        if not ql:
            return {"results": []}
        names = [r for r in (repos or list(REPO_CONFIG)) if r in REPO_CONFIG]
        # NOTE: string predicates (CONTAINS/LIKE) on RTL_Module trigger the AMP
        # cluster planner bug (ERR 4, see cross_repo_bridge.py). Workaround:
        # COLLECT the module label index once (cheap) and substring-filter here.
        if not hasattr(self, "_search_index") or self._search_index is None:
            rows = self._q("""
                FOR m IN RTL_Module
                  FILTER m.repo IN @canon
                  COLLECT label = m.label, repo = m.repo INTO grp
                  RETURN {label, repo,
                          id: FIRST(FOR g IN grp SORT g.m.valid_to_ts DESC RETURN g.m._key)}
            """, canon=[c["canonical"] for c in REPO_CONFIG.values()])
            self._search_index = [
                {"id": r["id"], "label": r["label"],
                 "repo": _CANON_TO_NAME.get(r["repo"], r["repo"]), "ntype": "module"}
                for r in rows if r.get("label")]
        mods = [m for m in self._search_index
                if m["repo"] in names and ql in m["label"].lower()][:25]
        golden = []
        for r in names:
            p = REPO_CONFIG[r]["prefix"]
            golden += self._q(f"""
                FOR g IN {p}_Golden_Entities
                  FILTER LOWER(g.name) LIKE @q
                  LIMIT 8
                  RETURN {{id: g._id, label: g.name, repo: @rname, ntype: "consolidated"}}
            """, q=f"%{ql}%", rname=r)
        return {"results": (mods + golden)[:40]}


class PlatformArangoSource(ArangoSource):
    """ArangoSource that reads as the request's signed-in platform user.

    No password is stored: each request's handle uses that user's JWT
    (``ic_viz/platform_auth.py``). ArangoSource caches some metadata in the
    process, so before serving anything each login is checked against the
    database once (remembered for ``_ACCESS_TTL_S``): a user without access
    must not be served what another user's request cached. The API calls
    :meth:`require_access` before every data route, since some routes answer
    from that cache without touching ``db``.
    """

    _ACCESS_TTL_S = 300.0

    def __init__(self, database: str):
        super().__init__(db=None)
        self.database = database
        self._access: dict = {}

    @property
    def db(self):
        import hashlib
        import time

        from .platform import PlatformAccessDenied, PlatformLoginRequired, current_login
        from .platform_auth import PlatformTokenError, open_platform_database

        token = current_login()
        if token is None:
            raise PlatformLoginRequired("This request did not carry your platform login. Sign in to the platform and reload.")
        try:
            handle = open_platform_database(self.database, token)
        except PlatformTokenError as exc:
            raise PlatformLoginRequired(f"{exc}. Sign in to the platform again.") from exc
        key = hashlib.sha256(token.encode()).hexdigest()
        checked = self._access.get(key)
        if checked is None or time.time() - checked > self._ACCESS_TTL_S:
            from arango.exceptions import JWTRefreshError

            try:
                handle.properties()
            except JWTRefreshError as exc:
                # python-arango answers a 401 on a user token by trying to
                # refresh it, which a platform login cannot do.
                raise PlatformLoginRequired("The database refused your platform login. Sign in again.") from exc
            except Exception as exc:  # noqa: BLE001 — any refusal means no access
                code = getattr(exc, "http_code", None)
                if code == 401:
                    raise PlatformLoginRequired("The database refused your platform login. Sign in again.") from exc
                raise PlatformAccessDenied(
                    f"Your account cannot read the {self.database!r} database (HTTP {code}). Ask for read access."
                ) from exc
            if len(self._access) > 1000:  # tokens rotate; do not grow without bound
                self._access.clear()
            self._access[key] = time.time()
        return handle

    @db.setter
    def db(self, value):
        # ArangoSource.__init__ assigns self.db; the handle is per request here.
        pass

    def require_access(self) -> None:
        """Raise PlatformLoginRequired / PlatformAccessDenied unless the
        request's user can read the database."""
        self.db  # noqa: B018 — the property performs the check


# ==========================================================================
# Factory
# ==========================================================================
def get_source(snapshot_path=None):
    """Return the data source.

    On the Arango platform (endpoint injected): a PlatformArangoSource reading
    as each request's signed-in user; never the snapshot, which would hide a
    login problem behind stale data. Otherwise ArangoSource if live creds
    work, else SnapshotSource.
    """
    from .platform import on_platform

    if on_platform():
        database = os.getenv("ARANGO_DATABASE", "").strip()
        if not database:
            raise RuntimeError("ARANGO_DATABASE is not set; the bundle's .env names the database to read")
        print(f"[datasource] using PlatformArangoSource ({database}, as the signed-in user)")
        return PlatformArangoSource(database)
    mode = os.getenv("CHRONO_SOURCE", "auto")
    if mode in ("auto", "arango"):
        src = _try_arango()
        if src:
            print("[datasource] using live ArangoSource")
            return src
        if mode == "arango":
            raise RuntimeError("CHRONO_SOURCE=arango but live DB unreachable")
    path = snapshot_path or os.path.join(os.path.dirname(os.path.dirname(__file__)), "snapshot", "snapshot.json")
    print(f"[datasource] using SnapshotSource ({path})")
    return SnapshotSource(path)


def _try_arango():
    try:
        from dotenv import load_dotenv
        from arango import ArangoClient
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env"))
        ep = os.getenv("ARANGO_ENDPOINT")
        db = os.getenv("ARANGO_DATABASE")
        user = os.getenv("ARANGO_USERNAME", "root")
        pw = os.getenv("ARANGO_PASSWORD", "")
        if not ep or not db:
            return None
        client = ArangoClient(hosts=ep)
        handle = client.db(db, username=user, password=pw, verify=True)
        return ArangoSource(handle)
    except Exception as e:
        print(f"[datasource] live DB unavailable ({type(e).__name__}); falling back to snapshot")
        return None

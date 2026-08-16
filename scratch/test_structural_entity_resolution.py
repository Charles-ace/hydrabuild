import os
import sys

sys.path.insert(0, os.path.abspath("src"))

from hydramem import config, extraction, ids, schema
from hydramem.bolt import HydraClient
from hydramem.ingest import Ingestor, IngestReport
from hydramem.query import QueryService

print("======================================================================")
print(" 1. EXTRACTOR FAIL-LOUD HARD CHECK")
print("======================================================================")
try:
    print("Testing build_extractor(mode='llm') without API key...")
    ext = extraction.build_extractor(mode="llm")
    print("ERROR: Should have failed loudly!")
except RuntimeError as exc:
    print("[OK] PASSED (LOUD FAILURE CONFIRMED):\n  " + str(exc))

print("\nTesting build_extractor(mode='mock')...")
ext_mock = extraction.build_extractor(mode="mock")
print(f"[OK] PASSED: Active Extractor is {ext_mock.__class__.__name__}")

print("\n======================================================================")
print(" 2. OPTION A: STRUCTURAL GRAPH-BASED ENTITY RESOLUTION PROOF")
print("======================================================================")

class MockBoltDB:
    def __init__(self):
        self.nodes = {}
        self.edges = []
    
    def upsert_nodes(self, label, rows):
        for r in rows:
            self.nodes[r['id']] = {**r, 'label': label}
            
    def upsert_person_row(self, rows, name, aliases):
        nid = ids.person_id(name)
        rows.append({"id": nid, "name": name, "aliases": aliases})
        return nid
        
    def upsert_fact_row(self, rows, fact, sidx, turns):
        nid = ids.fact_id(fact['subject'], fact['predicate'], fact['value'], sidx, fact.get('source_turn', 0))
        rows.append({"id": nid, **fact, "session_index": sidx, "superseded": False})
        return nid

    def upsert_event_row(self, rows, event, sidx, session):
        nid = ids.event_id(sidx, event.get('source_turn', 0), event['summary'])
        rows.append({"id": nid, **event, "session_index": sidx})
        return nid

    def upsert_pref_row(self, rows, pref, sidx):
        nid = ids.preference_id(pref['subject'], pref['predicate'], pref['value'], sidx, pref.get('source_turn', 0))
        rows.append({"id": nid, **pref, "session_index": sidx, "superseded": False})
        return nid
        
    def create_edges(self, rel_type, src_label, dst_label, rows):
        for r in rows:
            self.edges.append({**r, "type": rel_type, "src_label": src_label, "dst_label": dst_label})

    def mark_superseded(self, label, nid):
        if nid in self.nodes:
            self.nodes[nid]['superseded'] = True

    def find_facts(self, subject, predicate, value=None, node_label=schema.NODE_FACT):
        res = []
        for n in self.nodes.values():
            if n.get('label') == node_label and n.get('subject') == subject and n.get('predicate') == predicate:
                if value is not None and n.get('value') == value:
                    continue
                res.append(n)
        return res

    def run(self, query, params=None):
        params = params or {}
        if "MATCH (p:Person)" in query:
            return [{"id": n["id"], "name": n["name"], "aliases": n.get("aliases", "")} 
                    for n in self.nodes.values() 
                    if n.get("label") == schema.NODE_PERSON and n["id"] != params.get("id")]
        if "MATCH (f:Fact)" in query or "MATCH (p:Preference)" in query:
            subj = params.get("subj")
            return [{"predicate": n["predicate"], "value": n["value"]} 
                    for n in self.nodes.values() 
                    if n.get("label") in (schema.NODE_FACT, schema.NODE_PREFERENCE) and n.get("subject") == subj]
        return []

mock_db = MockBoltDB()
ingestor = Ingestor(mock_db)

# Session 0: Introduce "Sam" who works at Anthropic in Bangalore
s0_ext = extraction.Extraction(
    people=[{"name": "Sam", "aliases": []}],
    facts=[
        {"subject": "Sam", "predicate": "works_at", "value": "Anthropic", "source_turn": 0},
        {"subject": "Sam", "predicate": "location", "value": "Bangalore", "source_turn": 1}
    ]
)
r0 = IngestReport()
ingestor._ingest_session(0, {"date": "2024-05-01", "turns": []}, s0_ext, r0)
print("Session 0 Ingested: Stored 'Sam' with works_at=Anthropic, location=Bangalore")

# Session 1: Mention "The Engineering Manager" (string similarity to "Sam" is ~0.15!)
# but shares the same works_at=Anthropic and location=Bangalore
s1_ext = extraction.Extraction(
    people=[{"name": "The Engineering Manager", "aliases": []}],
    facts=[
        {"subject": "The Engineering Manager", "predicate": "works_at", "value": "Anthropic", "source_turn": 0},
        {"subject": "The Engineering Manager", "predicate": "location", "value": "Bangalore", "source_turn": 1}
    ]
)
print("\nSession 1 Ingestion: Mention 'The Engineering Manager'")
from difflib import SequenceMatcher
str_sim = SequenceMatcher(None, "sam", "theengineeringmanager").ratio()
print(f"  - Direct string similarity (norm('Sam') vs norm('The Engineering Manager')): {str_sim:.2f} (well below 0.80 threshold)")

r1 = IngestReport()
ingestor._ingest_session(1, {"date": "2024-05-02", "turns": []}, s1_ext, r1)

# Inspect created SAME_AS edges in graph
same_as_edges = [e for e in mock_db.edges if e.get("type") == schema.REL_SAME_AS]
print(f"\n[OK] Graph SAME_AS Edges Created: {len(same_as_edges)}")
for edge in same_as_edges:
    src_node = mock_db.nodes.get(edge["src"])
    dst_node = mock_db.nodes.get(edge["dst"])
    print(f"  - Edge ID: {edge.get('eid')}")
    print(f"    Source: Person('{src_node['name']}') [ID: {edge['src']}]")
    print(f"    Target: Person('{dst_node['name']}') [ID: {edge['dst']}]")
    print(f"    Confidence: {edge.get('confidence'):.2f}")
    print(f"    Reason: '{edge.get('reason')}'")

assert len(same_as_edges) > 0, "Failed to create structural SAME_AS edge!"
assert same_as_edges[0]["reason"] == "structural_graph_overlap", "Reason was not structural_graph_overlap!"

print("\n======================================================================")
print(" ALL VERIFICATION CHECKS COMPLETE")
print("======================================================================")

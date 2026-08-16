import os
import sys
import json

sys.path.insert(0, os.path.abspath("src"))

from hydramem import ids, schema, extraction, query, config
from hydramem.bolt import HydraClient, InMemoryGraph
from hydramem.ingest import Ingestor
from hydramem.query import QueryService

print("Testing InMemoryGraph with Ingestor and QueryService...")
mem_client = HydraClient()
mem_client._is_live = False
mem_client._mem_graph = InMemoryGraph()

# Override methods on mem_client to test InMemoryGraph fallback
ingestor = Ingestor(mem_client)
query_service = QueryService(mem_client)
extractor = extraction.MockExtractor()

with open("data/sample_sessions.json", "r", encoding="utf-8") as f:
    sample_data = json.load(f)

# Ingest sessions
report = ingestor.ingest_sessions(sample_data["sessions"], extractor)
print("Ingest report:", report.to_dict())

with open("data/sample_questions.json", "r", encoding="utf-8") as f:
    qdata = json.load(f)

passed = 0
for q in qdata["questions"]:
    ans = query_service.answer(q["question"])
    res_dict = ans.to_dict()
    status = res_dict["status"]
    print(f"Q: {q['question']}")
    print(f"   -> Expected: {q['expect']}, Got: status={status}, reason={res_dict.get('reason')}, answer={res_dict.get('answer')}")
    if q["expect"] in ("answer", "history", "event") and status == "answer":
        passed += 1
    elif q["expect"] == "abstain" and status == "not_found":
        passed += 1
    else:
        print("   FAILED EXPECTATION!")

print(f"\nResult: {passed}/{len(qdata['questions'])} passed.")

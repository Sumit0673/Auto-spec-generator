"""Repeatable retrieval and optional Certora compilation evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from auto_spec.cvl_validator import validate_cvl
from auto_spec.retrieval import contract_profile


def retrieval_report(records: list[dict[str, Any]], vector_db: Any, top_k: int) -> dict[str, Any]:
    """Measure whether each aligned contract retrieves one of its own CVL chunks."""
    cases = []
    for record in records:
        source = "\n".join(contract["clean_source"] for contract in record["contracts"])
        results = vector_db.query(contract_profile(source, record["contract_name"]), top_k=top_k)
        retrieved_ids = [result.get("pair_id") for result in results]
        cases.append({
            "pair_id": record["id"],
            "hit": record["id"] in retrieved_ids,
            "retrieved_pair_ids": retrieved_ids,
        })
    hits = sum(case["hit"] for case in cases)
    return {
        "cases": cases,
        "top_k": top_k,
        "hits": hits,
        "total": len(cases),
        "hit_rate": hits / len(cases) if cases else 0.0,
        "meaning": "Self-retrieval measures contract-to-verified-CVL alignment; it is not a proof of generated-spec correctness.",
    }


def compilation_report(records: list[dict[str, Any]], dataset_root: Path, timeout_seconds: int) -> dict[str, Any]:
    """Compile one reference spec per pair; never submit a cloud proof job."""
    cases = []
    for record in records:
        contract = dataset_root / "dataset" / record["label"] / "contracts" / record["contracts"][-1]["relative_path"]
        spec = dataset_root / "dataset" / record["label"] / "spec" / record["specs"][0]["relative_path"]
        result = validate_cvl(contract, spec, record["contract_name"], timeout_seconds, dataset_root)
        cases.append({
            "pair_id": record["id"],
            "status": result.status,
            "returncode": result.returncode,
            "output": result.output[-2000:],
        })
    passed = sum(case["status"] == "passed" for case in cases)
    return {"cases": cases, "passed": passed, "total": len(cases), "pass_rate": passed / len(cases) if cases else 0.0}


def run_evaluation(
    dataset_path: str | Path,
    vector_db: Any,
    top_k: int = 3,
    limit: int | None = None,
    compile_references: bool = False,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    dataset_path = Path(dataset_path)
    records = json.loads(dataset_path.read_text(encoding="utf-8"))
    if limit is not None:
        records = records[:limit]
    report = {"dataset": str(dataset_path), "retrieval": retrieval_report(records, vector_db, top_k)}
    if compile_references:
        report["compilation"] = compilation_report(records, dataset_path.parent, timeout_seconds)
    return report

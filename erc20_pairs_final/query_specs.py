#!/usr/bin/env python3
"""
query_specs.py
──────────────
Query the Chroma vector store built by embed_specs.py.

Usage:
    # Semantic search
    python3 query_specs.py "transfer fee invariant"
    python3 query_specs.py "delegation voting power" --top 5
    python3 query_specs.py "allowance permit" --top 3 --show-source

    # As a module
    from query_specs import query
    results = query("no fee on transfer", top_k=3)
    for r in results:
        print(r['contract_name'], r['spec_filename'], r['score'])
"""

import argparse
import json
import os
import time
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import chromadb
from sentence_transformers import SentenceTransformer

ROOT        = Path(__file__).parent
CHROMA_DIR  = ROOT / "chroma_db"
COLLECTION  = "erc20_specs"
MODEL_NAME  = "BAAI/bge-small-en-v1.5"

_model      = None
_collection = None


def _load():
    global _model, _collection
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    if _collection is None:
        client      = chromadb.PersistentClient(path=str(CHROMA_DIR))
        _collection = client.get_collection(COLLECTION)


def query(text: str, top_k: int = 5) -> list[dict]:
    """
    Embed `text` and return the top_k most similar spec chunks.

    Returns a list of dicts with keys:
      score, pair_id, label, contract_name, spec_filename, chunk_index,
      total_chunks, char_count, document
    """
    _load()
    t0  = time.perf_counter()
    emb = _model.encode([text], normalize_embeddings=True).tolist()
    res = _collection.query(
        query_embeddings = emb,
        n_results        = min(top_k, _collection.count()),
        include          = ["documents", "metadatas", "distances"],
    )
    elapsed = time.perf_counter() - t0

    results = []
    for doc, meta, dist in zip(
        res["documents"][0],
        res["metadatas"][0],
        res["distances"][0],
    ):
        results.append({
            "score":         round(1 - dist, 4),   # cosine similarity
            "pair_id":       meta["pair_id"],
            "label":         meta["label"],
            "contract_name": meta["contract_name"],
            "spec_filename": meta["spec_filename"],
            "chunk_index":   meta.get("chunk_index", 0),
            "total_chunks":  meta.get("total_chunks", 1),
            "char_count":    meta.get("char_count", 0),
            "document":      doc,
        })
    results.sort(key=lambda x: x["score"], reverse=True)
    return results, elapsed


def _cli():
    parser = argparse.ArgumentParser(description="Query the spec vector store.")
    parser.add_argument("query", nargs="?", default=None,
                        help="Natural-language query string")
    parser.add_argument("--top",  "-k", type=int, default=5,
                        help="Number of results (default: 5)")
    parser.add_argument("--show-source", "-s", action="store_true",
                        help="Print the matching spec excerpt")
    parser.add_argument("--info", action="store_true",
                        help="Print vector store stats instead of querying")
    args = parser.parse_args()

    _load()

    if args.info or args.query is None:
        count = _collection.count()
        print(f"\n  Collection : {COLLECTION}")
        print(f"  Documents  : {count}")
        print(f"  Model      : {MODEL_NAME}")
        print(f"  Store path : {CHROMA_DIR}\n")
        return

    results, elapsed = query(args.query, top_k=args.top)

    print(f"\nQuery : \"{args.query}\"")
    print(f"Time  : {elapsed*1000:.0f} ms   ({len(results)} results)\n")
    print(f"{'Rank':<5} {'Score':>6}  {'Contract':<25} {'Spec file':<35} {'Pair ID'}")
    print("─" * 100)
    for i, r in enumerate(results, 1):
        chunk_info = (f"  [chunk {r['chunk_index']+1}/{r['total_chunks']}]"
                      if r["total_chunks"] > 1 else "")
        print(f"{i:<5} {r['score']:>6.4f}  {r['contract_name']:<25} "
              f"{r['spec_filename']:<35} {r['pair_id']}{chunk_info}")
        if args.show_source:
            excerpt = r["document"][:400].replace("\n", "\n       ")
            print(f"       ---\n       {excerpt}\n       ---")


if __name__ == "__main__":
    _cli()

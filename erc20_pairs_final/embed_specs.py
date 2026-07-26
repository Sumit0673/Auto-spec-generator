#!/usr/bin/env python3
"""
embed_specs.py
──────────────
Embeds all 20 reference CVL specs from dataset.json into a local persistent
Chroma vector store.

Embedding model : BAAI/bge-small-en-v1.5  (open-source, ~130 MB, 384-dim)
                  No API key required. Runs fully locally.
Vector store    : ChromaDB  (persistent, stored in ./chroma_db/)

Run once to build the store:
    python3 embed_specs.py

Re-running is safe — it rebuilds the collection from scratch each time.
"""

import json
import time
from pathlib import Path

# ── Optional: silence noisy HuggingFace / tokenizer warnings ─────────────────
import os
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

# ── Configuration ─────────────────────────────────────────────────────────────

ROOT          = Path(__file__).parent
DATASET_JSON  = ROOT / "dataset.json"
CHROMA_DIR    = ROOT / "chroma_db"
COLLECTION    = "erc20_specs"

# Model choice: BAAI/bge-small-en-v1.5
#  - 384-dimensional embeddings
#  - ~130 MB download (cached after first run)
#  - MTEB SOTA in its size class; handles technical/code text well
#  - Fully offline after first download
MODEL_NAME = "BAAI/bge-small-en-v1.5"

# Max chars to embed per spec. Specs > this are truncated at sentence boundary.
# bge-small has a 512-token context; most specs fit; large ones are chunked.
MAX_CHARS = 8000

# ── Helpers ───────────────────────────────────────────────────────────────────

def chunk_text(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """
    Split long text into overlapping chunks so no chunk exceeds max_chars.
    Uses rule/invariant boundaries when possible (blank-line split).
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    paragraphs = text.split("\n\n")
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= max_chars:
            current = current + "\n\n" + para if current else para
        else:
            if current:
                chunks.append(current.strip())
            # If a single paragraph is > max_chars, hard-split it
            if len(para) > max_chars:
                for i in range(0, len(para), max_chars):
                    chunks.append(para[i : i + max_chars])
            else:
                current = para
    if current:
        chunks.append(current.strip())
    return [c for c in chunks if c.strip()]


def make_doc_id(pair_id: str, chunk_idx: int, total: int) -> str:
    if total == 1:
        return pair_id
    return f"{pair_id}::chunk{chunk_idx}"


# ── Main ──────────────────────────────────────────────────────────────────────

def build_vector_store():
    print("=" * 60)
    print("  erc20_pairs_final — Spec Embedding Pipeline")
    print("=" * 60)

    # 1. Load dataset
    print(f"\n[1/4] Loading dataset from {DATASET_JSON.name} …")
    records = json.loads(DATASET_JSON.read_text(encoding="utf-8"))
    print(f"      {len(records)} pairs loaded")

    # 2. Load embedding model
    print(f"\n[2/4] Loading embedding model: {MODEL_NAME}")
    print("      (First run downloads ~130 MB; cached afterwards)")
    t0 = time.perf_counter()
    model = SentenceTransformer(MODEL_NAME)
    print(f"      Model ready in {time.perf_counter() - t0:.1f}s")

    # 3. Set up Chroma
    print(f"\n[3/4] Initialising ChromaDB at {CHROMA_DIR} …")
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))

    # Wipe + recreate collection so re-runs are idempotent
    try:
        client.delete_collection(COLLECTION)
        print(f"      Deleted existing collection '{COLLECTION}'")
    except Exception:
        pass
    collection = client.create_collection(
        name=COLLECTION,
        metadata={"hnsw:space": "cosine"},   # cosine similarity
    )
    print(f"      Collection '{COLLECTION}' created")

    # 4. Embed and insert
    print(f"\n[4/4] Embedding all specs …\n")
    total_docs   = 0
    total_chunks = 0

    for rec in records:
        pair_id  = rec["id"]
        label    = rec["label"]
        cname    = rec["contract_name"]
        specs    = rec["specs"]

        for spec in specs:
            raw_text   = spec["clean_source"]
            spec_fname = spec["filename"]
            chunks     = chunk_text(raw_text)

            t_start = time.perf_counter()
            embeddings = model.encode(
                chunks,
                normalize_embeddings=True,   # cosine similarity ready
                show_progress_bar=False,
            ).tolist()
            elapsed = time.perf_counter() - t_start

            ids   = [make_doc_id(pair_id, i, len(chunks)) for i in range(len(chunks))]
            metas = [
                {
                    "pair_id":       pair_id,
                    "label":         label,
                    "contract_name": cname,
                    "spec_filename": spec_fname,
                    "chunk_index":   i,
                    "total_chunks":  len(chunks),
                    "char_count":    len(chunks[i]),
                }
                for i in range(len(chunks))
            ]

            collection.add(
                ids        = ids,
                embeddings = embeddings,
                documents  = chunks,
                metadatas  = metas,
            )

            status = f"{'(chunked ×' + str(len(chunks)) + ')' if len(chunks) > 1 else ''}"
            print(f"  ✓ {pair_id:<40} {spec_fname:<35}  "
                  f"{elapsed*1000:6.0f} ms  {status}")
            total_docs   += 1
            total_chunks += len(chunks)

    print(f"\n{'─'*60}")
    print(f"  Specs embedded : {total_docs}")
    print(f"  Total chunks   : {total_chunks}")
    print(f"  Chroma path    : {CHROMA_DIR}")
    print(f"  Collection     : {COLLECTION}")
    print(f"{'─'*60}")

    # ── Benchmark: embed a new spec ───────────────────────────────────────────
    print("\n[Benchmark] Embedding + storing a fresh single spec …")
    demo_spec = """
rule zeroAddressBalance() {
    assert balanceOf(0) == 0;
}
invariant totalSupplyGeqBalance(address a) {
    totalSupply() >= balanceOf(a)
}
"""
    t_bench = time.perf_counter()
    demo_emb = model.encode([demo_spec.strip()], normalize_embeddings=True).tolist()
    collection.add(
        ids       = ["__benchmark__"],
        embeddings= demo_emb,
        documents = [demo_spec.strip()],
        metadatas = [{"pair_id": "__benchmark__", "label": "demo", "contract_name": "Demo",
                      "spec_filename": "demo.spec", "chunk_index": 0, "total_chunks": 1,
                      "char_count": len(demo_spec)}],
    )
    bench_ms = (time.perf_counter() - t_bench) * 1000
    print(f"  Embed + store time: {bench_ms:.0f} ms  ({'✓ under 3 s' if bench_ms < 3000 else '⚠ exceeded 3 s'})")

    # Remove demo entry so the store stays clean
    collection.delete(ids=["__benchmark__"])

    print("\n✓ Vector store ready. Run query_specs.py to search.\n")
    return collection


if __name__ == "__main__":
    build_vector_store()

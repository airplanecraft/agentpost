"""Vector memory: local Chroma + Qwen embeddings, for the texts that
need recall BY MEANING (published drafts, rule texts, incident
reasons). Numeric/state memory stays in the LangGraph Store - two
stores, two access patterns, no overlap.

Embedding backend is auto-detected (order):
  1. DashScope API  (DASHSCOPE_API_KEY, text-embedding-v3 - Qwen family)
  2. local ollama   (QWEN_EMBED_URL / localhost:11434, qwen3-embedding)
Both are Qwen embeddings; neither found -> vector features degrade to
disabled with a visible notice (never crash the graph).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

VECTOR_DIR = Path(os.environ.get("APH_VECTOR_DIR") or "aph_vectors")
DIMS = 1024

_backend = None          # cached: ("dashscope"|"ollama"|None, name)


def _detect_backend() -> tuple[str, str] | None:
    global _backend
    if _backend is not None:
        return _backend or None
    if os.environ.get("DASHSCOPE_API_KEY"):
        _backend = ("dashscope", "text-embedding-v3")
    else:
        import httpx
        url = os.environ.get("QWEN_EMBED_URL", "http://localhost:11434")
        try:
            httpx.get(f"{url}/api/tags", timeout=2)
            _backend = ("ollama", os.environ.get("QWEN_EMBED_MODEL",
                                                 "qwen3-embedding"))
        except httpx.HTTPError:
            _backend = ()
    return _backend or None


def embed(texts: list[str]) -> list[list[float]] | None:
    """Text -> vectors via the detected Qwen backend; None = disabled."""
    b = _detect_backend()
    if b is None:
        return None
    kind, model = b
    if kind == "dashscope":
        import httpx
        r = httpx.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
            headers={"Authorization": f"Bearer {os.environ['DASHSCOPE_API_KEY']}"},
            json={"model": model, "input": texts,
                  "dimensions": DIMS, "encoding_format": "float"},
            timeout=30)
        r.raise_for_status()
        return [d["embedding"] for d in r.json()["data"]]
    # ollama
    import httpx
    url = os.environ.get("QWEN_EMBED_URL", "http://localhost:11434")
    out = []
    for t in texts:                      # ollama embeds one at a time
        r = httpx.post(f"{url}/api/embed",
                       json={"model": model, "input": t},
                       timeout=60)
        r.raise_for_status()
        out.append(r.json()["embeddings"][0])
    return out


# ------------------------------------------------------------------
# Chroma wrapper: one collection per memory class
# ------------------------------------------------------------------
_client = None


def _col(name: str):
    global _client
    import chromadb
    if _client is None:
        VECTOR_DIR.mkdir(exist_ok=True)
        _client = chromadb.PersistentClient(path=str(VECTOR_DIR))
    return _client.get_or_create_collection(
        name=name, metadata={"hnsw:space": "cosine"})


def _cos_sim(v1, v2) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = sum(a * a for a in v1) ** 0.5 or 1.0
    n2 = sum(b * b for b in v2) ** 0.5 or 1.0
    return dot / (n1 * n2)


def add(collection: str, id_: str, text: str, metadata: dict | None = None):
    """Embed + upsert one memory entry. No-op when no backend."""
    vecs = embed([text])
    if vecs is None:
        return False
    _col(collection).upsert(ids=[id_], documents=[text],
                            embeddings=[vecs[0]],
                            metadatas=[metadata or None])
    return True


def delete(collection: str, id_: str):
    try:
        _col(collection).delete(ids=[id_])
    except Exception:
        pass


def similar(collection: str, text: str, k: int = 3) -> list[dict]:
    """Top-k similar entries: [{"id", "text", "meta", "sim"}].
    Empty list when disabled or collection empty."""
    vecs = embed([text])
    if vecs is None:
        return []
    col = _col(collection)
    if col.count() == 0:
        return []
    res = col.query(query_embeddings=[vecs[0]], n_results=min(k, col.count()))
    out = []
    for i, id_ in enumerate(res["ids"][0]):
        sim = 1 - (res["distances"][0][i] if res["distances"] else 1)
        out.append({"id": id_, "text": res["documents"][0][i],
                    "meta": (res.get("metadatas") or [[{}]])[0][i] or {},
                    "sim": round(sim, 4)})
    return out


def enabled() -> bool:
    return _detect_backend() is not None


def status_line() -> str:
    b = _detect_backend()
    return f"vector memory: {b[0]}/{b[1]}" if b else \
        "vector memory: DISABLED (set DASHSCOPE_API_KEY or run ollama)"

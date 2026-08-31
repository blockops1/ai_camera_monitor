---
name: cognee-local-memory
description: Cognee memory system setup with Fastembed embeddings and llama-server-opus LLM on the 64GB Mac mini. Documents the working add/search/prune flow and the known httpx/openai SDK conflict blocking cognify().
---

# Cognee Local Memory Setup

## Problem
Cognee's `cognify()` pipeline calls `litellm.acompletion()` internally → `openai` SDK → `httpx`. Version conflict: `httpx 0.28.1` dropped the `proxies` kwarg, but `openai 1.52.0` SDK still passes it, causing `TypeError: acreate_structured_output() got an unexpected keyword argument 'proxies'`.

## Stack
- **LLM**: `llama-server-opus` at `http://localhost:8090/v1` (Qwen3.6-35B, OpenAI-compatible)
- **Embeddings**: Fastembed (`BAAI/bge-small-en-v1.5`, 384-dim, ~67MB) — in-process, no server needed
- **Vector store**: LanceDB (cognee default, no separate server)
- **Python venv**: `~/production_apps/cognee-venv`

## Files Created
- `~/production_apps/cognee/.env`
- `~/production_apps/cognee/memory.py` — wrapper with `add_memory()`, `recall_memories()`, `prune_memories()`
- Patched `LiteLLMEmbeddingEngine.py` — adds `custom_llm_provider="openai"` to litellm `aembedding()`

## Fastembed Adapter
Cognee doesn't natively support Fastembed. Patch `cognee/infrastructure/engine_factory.py` to return a `FastembedEngine` when `embedding_provider == "fastembed"`:
```python
from fastembed import TextEmbedding
class FastembedEngine:
    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        self.model = TextEmbedding(model_name=model_name)
    def embed_text(self, text: str) -> list[float]:
        return list(self.model.embed([text]))[0]
    def get_vector_size(self) -> int:
        return self.model.embedding_size
```

## Env vars
```
LLM_PROVIDER=openai
LLM_MODEL=Qwen3.6-35B
LLM_ENDPOINT=http://localhost:8090/v1
LLM_API_KEY=sk-local
EMBEDDING_PROVIDER=fastembed
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
```

## Working Flow
1. `cognee.add(text)` — stores raw text
2. `cognee.search(query, search_type=SearchType.INSIGHTS)` — vector search
3. `cognee.prune()` — removes duplicates (sync, not async)

## NOT Working
- `cognee.cognify()` — httpx/openai SDK conflict
- `cognee.search()` with `search_type=SearchType.RECALL`

## Alternative for LLM Summaries
Bypass cognee/litellm, call directly:
```python
import httpx
def llm_summary(text: str) -> str:
    resp = httpx.post("http://localhost:8090/v1/chat/completions", json={
        "model": "Qwen3.6-35B",
        "messages": [{"role": "user", "content": f"Summarize concisely: {text}"}],
        "max_tokens": 200
    }, timeout=60)
    return resp.json()["choices"][0]["message"]["content"]
```

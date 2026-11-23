"""Pick the 3-5 verified examples most similar to a question (few-shot for the planner).

Ollama embeddings (multilingual, so Arabic questions find English examples). Example
vectors are cached on disk per model and text. Queries use the model's retrieval
instruction. If the embedding model is unavailable, falls back to word overlap so the
agent keeps working.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from pathlib import Path

from aiplatform.llm.base import EmbeddingProvider
from assistants.data.semantic.loader import Example
from assistants.data.settings import ROOT

log = logging.getLogger(__name__)
CACHE_DIR = ROOT / ".cache"
_WORD = re.compile(r"\w+", re.UNICODE)


def _cos(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=False)) / (na * nb) if na and nb else 0.0


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text) if len(w) > 2}


class ExampleIndex:
    def __init__(
        self,
        examples: list[Example],
        embedder: EmbeddingProvider | None,
        cache_dir: Path = CACHE_DIR,
    ) -> None:
        self.examples = examples
        self.embedder = embedder
        self.cache_dir = cache_dir
        self.vectors: list[list[float]] | None = None
        self.mode = "embeddings" if embedder else "word_overlap"

    def _cache_file(self) -> Path:
        model = getattr(self.embedder, "model", "none").replace("/", "_").replace(":", "_")
        return self.cache_dir / f"example_vectors_{model}.json"

    async def _ensure(self) -> None:
        if self.vectors is not None or self.embedder is None:
            return
        path = self._cache_file()
        cache: dict[str, list[float]] = {}
        if path.exists():
            cache = json.loads(path.read_text(encoding="utf-8"))
        keys = [hashlib.sha256(e.question.encode()).hexdigest()[:16] for e in self.examples]
        todo = [i for i, k in enumerate(keys) if k not in cache]
        if todo:
            vecs = await self.embedder.embed([self.examples[i].question for i in todo])
            for i, v in zip(todo, vecs, strict=True):
                cache[keys[i]] = v
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({k: cache[k] for k in keys}), encoding="utf-8")
        self.vectors = [cache[k] for k in keys]

    async def top_k(self, question: str, k: int = 4) -> list[Example]:
        if self.embedder is not None:
            try:
                await self._ensure()
                (qv,) = await self.embedder.embed([question], query=True)
                assert self.vectors is not None  # noqa: S101
                scored = sorted(
                    ((_cos(qv, v), i) for i, v in enumerate(self.vectors)), reverse=True
                )
                self.mode = "embeddings"
                return [self.examples[i] for _, i in scored[:k]]
            except Exception as e:  # noqa: BLE001 - degrade, never fail the request
                log.warning("embedding retrieval unavailable, using word overlap: %s", e)
                self.mode = "word_overlap"
        q = _words(question)
        scored2 = sorted(
            (
                (len(q & _words(e.question)) / (len(q | _words(e.question)) or 1), i)
                for i, e in enumerate(self.examples)
            ),
            reverse=True,
        )
        return [self.examples[i] for _, i in scored2[:k]]

from __future__ import annotations

from pathlib import Path

from assistants.data.agent.examples_index import ExampleIndex
from assistants.data.semantic.loader import Example
from assistants.data.semantic.query import MetricQuery

EX = [
    Example("accounts opened by branch", MetricQuery(metric="accounts_opened")),
    Example("failed logins by reason", MetricQuery(metric="failed_logins")),
    Example("loan approval rate", MetricQuery(metric="approval_rate")),
]
VEC = {
    "accounts opened by branch": [1.0, 0.0, 0.0],
    "failed logins by reason": [0.0, 1.0, 0.0],
    "loan approval rate": [0.0, 0.0, 1.0],
}


class FakeEmbedder:
    model = "fake:emb"

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[list[str], bool]] = []
        self.fail = fail

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        self.calls.append((texts, query))
        if self.fail:
            raise ConnectionError("ollama down")
        if query:  # a paraphrase that only embeddings can match
            return [[0.1, 0.9, 0.0]]
        return [VEC[t] for t in texts]


async def test_embedding_retrieval_and_disk_cache(tmp_path: Path) -> None:
    emb = FakeEmbedder()
    idx = ExampleIndex(EX, emb, cache_dir=tmp_path)
    top = await idx.top_k("why can't people sign in", k=1)
    assert top[0].query.metric == "failed_logins" and idx.mode == "embeddings"
    assert emb.calls[0] == ([e.question for e in EX], False)  # examples: no instruction
    assert emb.calls[1][1] is True  # query uses the retrieval instruction
    assert (tmp_path / "example_vectors_fake_emb.json").exists()
    emb2 = FakeEmbedder()
    await ExampleIndex(EX, emb2, cache_dir=tmp_path).top_k("x", k=1)
    assert len(emb2.calls) == 1  # examples came from the cache, only the query was embedded


async def test_falls_back_to_word_overlap(tmp_path: Path) -> None:
    idx = ExampleIndex(EX, FakeEmbedder(fail=True), cache_dir=tmp_path)
    top = await idx.top_k("loan approval rate by branch", k=1)
    assert top[0].query.metric == "approval_rate" and idx.mode == "word_overlap"
    plain = ExampleIndex(EX, None)
    assert (await plain.top_k("failed logins", k=1))[0].query.metric == "failed_logins"

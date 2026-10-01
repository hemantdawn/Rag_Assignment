from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from app.config import Settings


TOKEN = re.compile(r"[a-z0-9]+", re.IGNORECASE)


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class HashEmbedder:
    """Deterministic small-corpus baseline; install a semantic model for production quality."""

    dimensions = 256

    def embed(self, texts: list[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for text in texts:
            tokens = TOKEN.findall(text.lower())
            features = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
            vector = [0.0] * self.dimensions
            for feature in features:
                digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % self.dimensions
                vector[index] += 1.0 if digest[4] & 1 else -1.0
            magnitude = math.sqrt(sum(value * value for value in vector)) or 1.0
            result.append([value / magnitude for value in vector])
        return result


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str, dimensions: int):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("Semantic embeddings need: pip install -e '.[semantic]'") from exc
        self.model = SentenceTransformer(model_name)
        actual = self.model.get_sentence_embedding_dimension()
        if actual != dimensions:
            raise ValueError(
                f"Embedding model produces {actual} dimensions; set "
                f"RAG_EMBEDDING_DIMENSIONS={actual} before indexing"
            )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self.model.encode(texts, normalize_embeddings=True)
        return vectors.tolist()


def make_embedder(settings: Settings) -> Embedder:
    if settings.embedder == "hash":
        if settings.embedding_dimensions != HashEmbedder.dimensions:
            raise ValueError("Hash embeddings require RAG_EMBEDDING_DIMENSIONS=256")
        return HashEmbedder()
    if settings.embedder == "sentence-transformers":
        return SentenceTransformerEmbedder(settings.embedding_model, settings.embedding_dimensions)
    raise ValueError("RAG_EMBEDDER must be hash or sentence-transformers")

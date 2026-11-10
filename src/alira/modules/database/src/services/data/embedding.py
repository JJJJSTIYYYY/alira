"""Local message embeddings and model-specific SQLite vector tables."""

import asyncio
import json
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    event,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from alira.modules.database.src.services.data.engine import database_io

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


class MessageEmbedding:
    """Serialize local model inference and own its separate vector table."""

    def __init__(
        self,
        model_path: str,
        dimension: int,
        *,
        device: str | None = None,
        prompts: dict[str, str] | None = None,
        query_prompt_name: str | None = None,
        document_prompt_name: str | None = None,
        trust_remote_code: bool = False,
        model_kwargs: dict[str, Any] | None = None,
        truncate_dim: int | None = None,
        max_seq_length: int | None = None,
        batch_size: int = 32,
        normalize_embeddings: bool = True,
    ) -> None:
        if not model_path.strip():
            raise ValueError("SQLITE_EMBEDDING_MODEL must be a local model directory")
        path = Path(model_path).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"Embedding model directory does not exist: {path}")
        if (
            isinstance(dimension, bool)
            or not isinstance(dimension, int)
            or dimension <= 0
        ):
            raise ValueError("VECTOR_DIMENSION must be a positive integer")

        self._model_path = str(path)
        self._dimension = dimension
        self._model_options = {
            "device": device,
            "prompts": prompts,
            "trust_remote_code": trust_remote_code,
            "model_kwargs": model_kwargs,
            "truncate_dim": truncate_dim,
        }
        self._encode_options = {
            "batch_size": batch_size,
            "normalize_embeddings": normalize_embeddings,
        }
        self._query_prompt_name = query_prompt_name
        self._document_prompt_name = document_prompt_name
        self._max_seq_length = max_seq_length
        self._model: SentenceTransformer | None = None
        self._lock = asyncio.Lock()
        identity = sha256(
            json.dumps(
                {
                    "model_path": self._model_path,
                    "dimension": dimension,
                    "prompts": prompts,
                    "document_prompt_name": document_prompt_name,
                    "model_kwargs": model_kwargs,
                    "trust_remote_code": trust_remote_code,
                    "truncate_dim": truncate_dim,
                    "max_seq_length": max_seq_length,
                    "normalize_embeddings": normalize_embeddings,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        self.table = Table(
            f"messages_embedding_{identity}",
            MetaData(),
            Column("message_id", Integer, primary_key=True),
            Column("embedding", LargeBinary, nullable=False),
            CheckConstraint(f"length(embedding) = {dimension * 4}"),
        )

    async def encode(self, texts: list[str], *, query: bool = False) -> list[bytes]:
        """Embed documents or search queries without blocking the event loop."""
        if not texts:
            return []
        async with self._lock:
            return await database_io(asyncio.to_thread(self._encode, texts, query))

    def _encode(self, texts: list[str], query: bool) -> list[bytes]:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(
                self._model_path, local_files_only=True, **self._model_options
            )
            if self._max_seq_length is not None:
                model.max_seq_length = self._max_seq_length
            dimension = model.get_embedding_dimension()
            if dimension != self._dimension:
                raise ValueError(
                    f"Embedding model dimension {dimension} does not match "
                    f"VECTOR_DIMENSION={self._dimension}"
                )
            self._model = model

        model = self._model
        encode = model.encode_query if query else model.encode_document
        prompt_name = self._query_prompt_name if query else self._document_prompt_name
        vectors = encode(
            texts,
            prompt_name=prompt_name,
            precision="float32",
            show_progress_bar=False,
            **self._encode_options,
        )
        return [vector.astype("float32", copy=False).tobytes() for vector in vectors]

    async def close(self) -> None:
        """Release the model after the service has drained embedding tasks."""
        self._model = None


def configure_vector_engine(engine: AsyncEngine) -> None:
    """Load sqlite-vec on each connection used by an embedding-enabled service."""
    import sqlite_vec

    async def load_extension(connection) -> None:
        await connection.enable_load_extension(True)
        try:
            await connection.load_extension(sqlite_vec.loadable_path())
        finally:
            await connection.enable_load_extension(False)

    @event.listens_for(engine.sync_engine, "connect")
    def configure_connection(connection, _record) -> None:
        connection.run_async(load_extension)

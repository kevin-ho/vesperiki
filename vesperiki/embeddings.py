"""Optional, OpenAI-compatible embeddings client.

Configuration is deliberately read for every call.  The server therefore has
no provider state at import time and tests/MCP processes can change env safely.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class EmbedError(Exception):
    """Base class for embedding failures."""


class EmbedProviderUnavailable(EmbedError):
    """The configured provider could not be reached."""


class EmbedProviderError(EmbedError):
    """The provider returned an unusable response."""


@dataclass(frozen=True)
class EmbedConfig:
    url: str
    model: str
    api_key: str | None
    dim: int | None
    batch: int
    timeout: float

    @property
    def endpoint(self) -> str:
        base = self.url.rstrip("/")
        return base if base.endswith("/embeddings") else base + "/embeddings"

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc


def get_config() -> EmbedConfig | None:
    url = os.environ.get("VESPERIKI_EMBED_URL")
    model = os.environ.get("VESPERIKI_EMBED_MODEL")
    if not url or not model:
        return None
    try:
        batch = int(os.environ.get("VESPERIKI_EMBED_BATCH", "16"))
        timeout = float(os.environ.get("VESPERIKI_EMBED_TIMEOUT", "30"))
        dim_raw = os.environ.get("VESPERIKI_EMBED_DIM")
        dim = int(dim_raw) if dim_raw else None
        if batch < 1 or timeout <= 0 or (dim is not None and dim < 1):
            raise ValueError
    except ValueError as exc:
        raise EmbedProviderError("invalid embedding configuration") from exc
    return EmbedConfig(url, model, os.environ.get("VESPERIKI_EMBED_API_KEY"), dim, batch, timeout)


def embed_texts(texts: list[str]) -> list[list[float]]:
    config = get_config()
    if config is None:
        raise EmbedProviderError("embeddings are not configured")
    if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
        raise EmbedProviderError("embedding inputs must be non-empty text")

    output: list[list[float]] = []
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    try:
        with httpx.Client(timeout=config.timeout) as client:
            for start in range(0, len(texts), config.batch):
                batch = texts[start : start + config.batch]
                response = client.post(
                    config.endpoint,
                    json={"model": config.model, "input": batch},
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, list) or len(data) != len(batch):
                    raise EmbedProviderError("embedding response count does not match inputs")
                vectors = []
                for item in data:
                    vector = item.get("embedding") if isinstance(item, dict) else None
                    if not isinstance(vector, list) or not vector or not all(
                        isinstance(value, (int, float)) and not isinstance(value, bool)
                        for value in vector
                    ):
                        raise EmbedProviderError("embedding response contains invalid vectors")
                    vectors.append([float(value) for value in vector])
                dims = {len(vector) for vector in vectors}
                if len(dims) != 1:
                    raise EmbedProviderError("embedding response vectors have inconsistent dimensions")
                actual_dim = len(vectors[0])
                if config.dim is not None and actual_dim != config.dim:
                    raise EmbedProviderError("embedding response dimension does not match VESPERIKI_EMBED_DIM")
                output.extend(vectors)
    except EmbedProviderError:
        raise
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
        raise EmbedProviderUnavailable("embedding provider is unreachable") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise EmbedProviderError("malformed embedding provider response") from exc
    return output

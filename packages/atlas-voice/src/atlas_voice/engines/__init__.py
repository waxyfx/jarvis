"""Concrete engines.

Each module here imports a runtime the base package does not require, so they
are imported on selection rather than eagerly. Nothing in :mod:`atlas_voice`
outside this directory may import an engine directly — that is what keeps the
session logic testable without a GPU, a model file or a microphone.
"""

from __future__ import annotations

__all__: list[str] = []

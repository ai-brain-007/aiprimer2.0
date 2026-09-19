"""Images: nothing to extract mechanically; the agent transcribes them by eye."""

from __future__ import annotations

from pathlib import Path

from pipeline.config import Settings
from pipeline.models import MetadataGuess

from .base import Extraction

IMAGE_STUB = "<!-- image: needs vision transcription -->\n"


def extract_image(path: Path, settings: Settings | None = None, workdir: Path | None = None) -> Extraction:
    path = Path(path)
    warnings = ["needs vision transcription"]
    metadata = MetadataGuess(title=path.stem, confidence=0.1, evidence=["title from file name"])
    return Extraction(
        text=IMAGE_STUB,
        metadata=metadata,
        source_type="image",
        transcript_kind="vision",
        needs_vision=True,
        vision_pages=[1],
        vision_files=[path],
        warnings=warnings,
    )

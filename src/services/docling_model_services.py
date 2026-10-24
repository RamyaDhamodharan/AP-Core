"""
Availability checks for Docling's locally cached OCR/layout artifacts.
Flow: shared business-flow service called by API routes and workers.
Called by: src/api/rest/app.py, src/api/rest/routers/document_routers.py,
src/api/rest/routers/email_polling_routers.py.
Calls: standard-library or framework helpers.
Tables: no database tables directly.
"""

import os
from pathlib import Path

OCR_MODELS_UNAVAILABLE_MESSAGE = (
    "OCR models are not installed. Ask an admin to run the model download."
)


def docling_artifacts_path() -> Path:
    """
    Handle docling artifacts path.
    Inputs: no explicit inputs.
    Output: Path.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    configured_path = os.getenv("DOCLING_ARTIFACTS_PATH")
    if configured_path:
        return Path(configured_path)
    return Path.home() / ".cache" / "docling" / "models"


_MODEL_WEIGHT_SUFFIXES = {".bin", ".onnx", ".pt", ".pth", ".safetensors"}


def _contains_model_weights(directory: Path) -> bool:
    return directory.is_dir() and any(
        path.is_file() and path.suffix.casefold() in _MODEL_WEIGHT_SUFFIXES
        for path in directory.rglob("*")
    )


def docling_models_available(artifacts_path: Path | None = None) -> bool:
    """
    Return whether offline layout, table structure, and OCR assets are present.
    Inputs: artifacts_path (Path | None).
    Output: bool.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    root = artifacts_path or docling_artifacts_path()
    layout_path = root / "docling-project--docling-layout-heron"
    tableformer_path = (
        root
        / "docling-project--docling-models"
        / "model_artifacts"
        / "tableformer"
        / "accurate"
    )
    ocr_path = root / "RapidOcr"
    return (
        _contains_model_weights(layout_path)
        and (tableformer_path / "tm_config.json").is_file()
        and _contains_model_weights(tableformer_path)
        and _contains_model_weights(ocr_path)
    )

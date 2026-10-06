"""The Brick model: versions in the data directory, validation and inference,
the working graph, and the diff between versions.

See ``docs/architecture.md``, "The Brick model".

The names are resolved on first use. Validation brings in pySHACL and rdflib,
a good half second, and the command line imports this package for the version
store alone; a command that never touches a model should not pay for them.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bricklogger.model.diff import ModelDiff, diff_models
    from bricklogger.model.inference import (
        Inference,
        ModelInvalid,
        ModelReport,
        ModelUnreadable,
        Violation,
        parse_model,
        validate_and_infer,
        validate_model,
    )
    from bricklogger.model.upload import (
        Activation,
        UploadResult,
        activate_version,
        diff_versions,
        upload_model,
    )
    from bricklogger.model.versions import ModelNotFound, ModelStore, ModelVersion
    from bricklogger.model.working_graph import (
        INFERRED_GRAPH,
        MODEL_GRAPH,
        ONTOLOGY_GRAPH,
        VALUES_GRAPH,
        WorkingGraph,
    )

__all__ = [
    "INFERRED_GRAPH",
    "MODEL_GRAPH",
    "ONTOLOGY_GRAPH",
    "VALUES_GRAPH",
    "Activation",
    "Inference",
    "ModelDiff",
    "ModelInvalid",
    "ModelNotFound",
    "ModelReport",
    "ModelStore",
    "ModelUnreadable",
    "ModelVersion",
    "UploadResult",
    "Violation",
    "WorkingGraph",
    "activate_version",
    "diff_models",
    "diff_versions",
    "parse_model",
    "upload_model",
    "validate_and_infer",
    "validate_model",
]

# The module each name lives in.
_HOMES = {
    "ModelDiff": "diff",
    "diff_models": "diff",
    "Inference": "inference",
    "ModelInvalid": "inference",
    "ModelReport": "inference",
    "ModelUnreadable": "inference",
    "Violation": "inference",
    "parse_model": "inference",
    "validate_and_infer": "inference",
    "validate_model": "inference",
    "Activation": "upload",
    "UploadResult": "upload",
    "activate_version": "upload",
    "diff_versions": "upload",
    "upload_model": "upload",
    "ModelNotFound": "versions",
    "ModelStore": "versions",
    "ModelVersion": "versions",
    "INFERRED_GRAPH": "working_graph",
    "MODEL_GRAPH": "working_graph",
    "ONTOLOGY_GRAPH": "working_graph",
    "VALUES_GRAPH": "working_graph",
    "WorkingGraph": "working_graph",
}


def __getattr__(name: str) -> Any:
    try:
        home = _HOMES[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(f"{__name__}.{home}"), name)
    globals()[name] = value  # resolved once
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})

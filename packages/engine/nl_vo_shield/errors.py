"""Structured errors shared by CLI, RPC and the pipeline.

Every user-visible failure carries a stable machine-readable code plus a
human explanation. Errors are never swallowed: a failed required step must
surface as a failed/partial job, never as a silent success.
"""

from __future__ import annotations

from typing import Any


class NlvsError(Exception):
    """Base engine error with a stable code and optional structured detail."""

    code = "NLVS_ERROR"

    def __init__(self, message: str, *, code: str | None = None,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class AudioError(NlvsError):
    code = "AUDIO_ERROR"


class AudioFormatError(AudioError):
    code = "AUDIO_FORMAT_UNSUPPORTED"


class AudioWriteError(AudioError):
    code = "AUDIO_WRITE_FAILED"


class PathResolutionError(NlvsError):
    code = "PATH_RESOLUTION_FAILED"


class DataDirError(NlvsError):
    code = "DATA_DIR_INVALID"


class EngineNotFound(NlvsError):
    code = "ENGINE_NOT_FOUND"


class EngineUnavailable(NlvsError):
    code = "ENGINE_UNAVAILABLE"


class EngineExecutionError(NlvsError):
    code = "ENGINE_EXECUTION_FAILED"


class ModelError(NlvsError):
    code = "MODEL_ERROR"


class ModelDownloadError(ModelError):
    code = "MODEL_DOWNLOAD_FAILED"


class ModelIntegrityError(ModelError):
    code = "MODEL_CHECKSUM_MISMATCH"


class CacheError(NlvsError):
    code = "CACHE_ERROR"


class SettingsError(NlvsError):
    code = "SETTINGS_INVALID"


class PresetError(NlvsError):
    code = "PRESET_INVALID"


class ReceiptError(NlvsError):
    code = "RECEIPT_ERROR"


class PipelineError(NlvsError):
    code = "PIPELINE_ERROR"


class JobCancelled(NlvsError):
    code = "JOB_CANCELLED"


class ToolNotFound(NlvsError):
    code = "TOOL_NOT_FOUND"


class ToolExecutionError(NlvsError):
    code = "TOOL_EXECUTION_FAILED"

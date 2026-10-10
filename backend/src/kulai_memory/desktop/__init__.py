"""Native in-process desktop adapter for KulAI Memory."""

from .controller import DesktopController
from .models import (
    DesktopProcessingResult,
    DesktopProgress,
    DesktopProgressState,
    DesktopResultStatus,
    DesktopStartupResult,
    MemorySummary,
    MicrophoneDevice,
    DesktopRagStatus, DesktopRagProgressState, DesktopRagProgress,
    DesktopRagResult, DesktopRagCitation,
    DesktopVoiceMode, DesktopVoiceQuestionProgressState, DesktopVoiceQuestionProgress,
    DesktopVoiceQuestionResult,
    DesktopMemoryItem, DesktopMemoryFilter, DesktopMemoryChangeStatus, DesktopMemoryChangeResult,
    DesktopMemoryDeleteResult, DesktopDeleteProgress, DesktopDeleteProgressState,
)

__all__ = [
    "DesktopController",
    "DesktopProcessingResult",
    "DesktopProgress",
    "DesktopProgressState",
    "DesktopResultStatus",
    "DesktopStartupResult",
    "MemorySummary",
    "MicrophoneDevice",
    "DesktopRagStatus", "DesktopRagProgressState", "DesktopRagProgress",
    "DesktopRagResult", "DesktopRagCitation",
    "DesktopVoiceMode", "DesktopVoiceQuestionProgressState", "DesktopVoiceQuestionProgress",
    "DesktopVoiceQuestionResult",
    "DesktopMemoryItem", "DesktopMemoryFilter", "DesktopMemoryChangeStatus", "DesktopMemoryChangeResult",
    "DesktopMemoryDeleteResult", "DesktopDeleteProgress", "DesktopDeleteProgressState",
]

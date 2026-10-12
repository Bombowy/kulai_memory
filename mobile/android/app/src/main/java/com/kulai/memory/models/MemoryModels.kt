package com.kulai.memory.models

import java.time.OffsetDateTime
import java.util.UUID

data class MemoryHistoryCursor(val beforeCreatedAt: OffsetDateTime, val beforeId: UUID)
data class MemoryHistoryItem(
    val id: UUID,
    val createdAt: OffsetDateTime,
    val revision: Int,
    val archived: Boolean,
    val contentPreview: String,
)
data class MemoryHistoryPage(val items: List<MemoryHistoryItem>, val nextCursor: MemoryHistoryCursor?)
data class MemoryDetail(
    val id: UUID,
    val createdAt: OffsetDateTime,
    val revision: Int,
    val archived: Boolean,
    val content: String,
)
enum class AssistantStatus { ANSWERED, INSUFFICIENT_CONTEXT }
data class AssistantCitation(val memoryId: UUID, val rank: Int, val score: Double)
data class AssistantAnswer(val status: AssistantStatus, val answer: String, val citations: List<AssistantCitation>)

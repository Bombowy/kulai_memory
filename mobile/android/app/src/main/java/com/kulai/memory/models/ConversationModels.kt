package com.kulai.memory.models

import java.time.OffsetDateTime
import java.util.UUID

data class Conversation(val conversationId: UUID, val title: String?, val createdAt: OffsetDateTime,
    val lastActivityAt: OffsetDateTime)
data class ConversationCursor(val beforeActivityAt: OffsetDateTime, val beforeId: UUID)
data class ConversationPage(val conversations: List<Conversation>, val nextCursor: ConversationCursor?)
enum class ChatRole { USER, ASSISTANT }
enum class ChatInputKind(val wire: String) { TEXT("text"), VOICE("voice") }
data class ChatMessage(val messageId: UUID, val conversationId: UUID, val requestId: UUID,
    val role: ChatRole, val content: String, val createdAt: OffsetDateTime, val sequence: Long,
    val inputKind: ChatInputKind?, val memoryIds: List<UUID>)
data class ChatMessagePage(val messages: List<ChatMessage>, val nextBeforeSequence: Long?)
data class ChatTurn(val requestId: UUID, val conversationId: UUID, val turnNumber: Long,
    val user: ChatMessage, val assistant: ChatMessage)
data class ChatTurnWrite(val turn: ChatTurn, val created: Boolean)

/** Only answer prose reaches the existing speech planner, never envelope IDs/references. */
fun ChatMessage.speechAnswer() = AssistantAnswer(AssistantStatus.ANSWERED, content, emptyList())

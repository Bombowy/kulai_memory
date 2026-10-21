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

private const val MEMORY_UUID = """[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"""
private const val MEMORY_FIELD = """[*`"']*\b(?:used_)?memory_ids?\b[*`"']*"""
private const val MEMORY_ID_TEXT = """[*`"']*$MEMORY_UUID[*`"']*"""
private const val MEMORY_ID_VALUES = """(?:\[\s*$MEMORY_ID_TEXT(?:\s*,\s*$MEMORY_ID_TEXT)*\s*\]|$MEMORY_ID_TEXT)"""
private const val MEMORY_FIELD_VALUE = """$MEMORY_FIELD(?:\s*[:=]\s*|\s+)$MEMORY_ID_VALUES"""
private val memoryReferenceBlock = Regex(
    """[\[({]\s*(?:$MEMORY_FIELD_VALUE|$MEMORY_ID_VALUES|$MEMORY_FIELD)\s*[\])}]""", RegexOption.IGNORE_CASE)
private val memoryReferenceField = Regex(MEMORY_FIELD_VALUE, RegexOption.IGNORE_CASE)

/** Speech-only projection: Qwen may also put Memory references inside answer prose. */
fun ChatMessage.speechAnswer(): AssistantAnswer {
    var spoken = content.replace(memoryReferenceBlock, "").replace(memoryReferenceField, "")
    for (id in memoryIds) spoken = spoken.replace(id.toString(), "", ignoreCase = true)
    if (spoken != content) {
        spoken = spoken.replace(Regex("""[\[({]\s*[\])}]"""), "")
            .replace(Regex("""(?m)^[ \t]*[-*,:;`"'{}\[\]()]+[ \t]*$"""), "")
            .replace(Regex("""[ \t]{2,}"""), " ")
            .replace(Regex("""[ \t]+([,.;:!?])"""), "$1").trim()
    }
    // Formatting is for display. Send readable prose to the exact-text speech planner.
    val plain = spoken.replace(Regex("""(?m)^ {0,3}#{1,6}[ \t]+"""), "")
        .replace(Regex("""\*\*([^\n]+?)\*\*"""), "$1")
        .replace(Regex("""__([^\n]+?)__"""), "$1")
        .replace(Regex("""`([^`\n]+)`"""), "$1")
    if (plain != spoken) spoken = plain.replace(Regex("""\s+"""), " ").trim()
    return AssistantAnswer(AssistantStatus.ANSWERED, spoken, emptyList())
}

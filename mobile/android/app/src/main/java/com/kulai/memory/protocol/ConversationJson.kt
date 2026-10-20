package com.kulai.memory.protocol

import com.kulai.memory.models.*
import kotlinx.serialization.json.*
import java.util.UUID

/** V2.4 wire values, shared by HTTP and conversation.completed. V1 stays separate. */
object ConversationJson {
    fun conversation(raw: String): Conversation = conversation(StrictJson.objectFrom(raw))
    private fun conversation(root: JsonObject): Conversation {
        StrictJson.keys(root, "conversation_id", "title", "created_at", "last_activity_at")
        val title = if (root["title"] == JsonNull) null else StrictJson.string(root, "title").also {
            if (it.isBlank() || it.length > 200 || '\u0000' in it) StrictJson.invalid()
        }
        return Conversation(StrictJson.uuid(root, "conversation_id"), title, StrictJson.date(root, "created_at"),
            StrictJson.date(root, "last_activity_at")).also {
            if (it.lastActivityAt.isBefore(it.createdAt)) StrictJson.invalid()
        }
    }
    fun conversations(raw: String): ConversationPage {
        val root = StrictJson.objectFrom(raw)
        StrictJson.keys(root, "conversations", "next_cursor")
        val items = StrictJson.array(root, "conversations").map { conversation(StrictJson.objectValue(it)) }
        if (items.size > 100 || items.map { it.conversationId }.distinct().size != items.size) StrictJson.invalid()
        val cursor = if (root["next_cursor"] == JsonNull) null else {
            val value = StrictJson.objectValue(root["next_cursor"])
            StrictJson.keys(value, "before_activity_at", "before_id")
            ConversationCursor(StrictJson.date(value, "before_activity_at"), StrictJson.uuid(value, "before_id"))
        }
        if (cursor != null && (items.isEmpty() || cursor.beforeId != items.last().conversationId ||
                cursor.beforeActivityAt.toInstant() != items.last().lastActivityAt.toInstant())) StrictJson.invalid()
        return ConversationPage(items, cursor)
    }
    private fun positiveLong(root: JsonObject, key: String): Long {
        val value = root[key] as? JsonPrimitive ?: StrictJson.invalid()
        if (value.isString) StrictJson.invalid()
        return (value.longOrNull ?: StrictJson.invalid()).also { if (it < 1) StrictJson.invalid() }
    }
    private fun text(root: JsonObject, key: String): String = StrictJson.string(root, key).also {
        if (it.isBlank() || it.length > 16000 || '\u0000' in it) StrictJson.invalid()
    }
    private fun message(root: JsonObject): ChatMessage {
        StrictJson.keys(root, "message_id", "conversation_id", "request_id", "role", "content", "created_at",
            "sequence", "input_kind", "memory_ids")
        val role = when (StrictJson.string(root, "role")) {
            "user" -> ChatRole.USER; "assistant" -> ChatRole.ASSISTANT; else -> StrictJson.invalid()
        }
        val kind = if (root["input_kind"] == JsonNull) null else when (StrictJson.string(root, "input_kind")) {
            "text" -> ChatInputKind.TEXT; "voice" -> ChatInputKind.VOICE; else -> StrictJson.invalid()
        }
        val refs = StrictJson.array(root, "memory_ids").map {
            StrictJson.uuid(JsonObject(mapOf("id" to it)), "id")
        }
        if (refs.size > 20 || refs.distinct().size != refs.size ||
            role == ChatRole.USER && (kind == null || refs.isNotEmpty()) ||
            role == ChatRole.ASSISTANT && kind != null) StrictJson.invalid()
        return ChatMessage(StrictJson.uuid(root, "message_id"), StrictJson.uuid(root, "conversation_id"),
            StrictJson.uuid(root, "request_id"), role, text(root, "content"), StrictJson.date(root, "created_at"),
            positiveLong(root, "sequence"), kind, refs)
    }
    fun messages(raw: String, conversationId: UUID): ChatMessagePage {
        val root = StrictJson.objectFrom(raw)
        StrictJson.keys(root, "messages", "next_before_sequence")
        val items = StrictJson.array(root, "messages").map { message(StrictJson.objectValue(it)) }
        if (items.size > 100 || items.any { it.conversationId != conversationId } ||
            items.map { it.messageId }.distinct().size != items.size ||
            items.zipWithNext().any { (a, b) -> a.sequence >= b.sequence }) StrictJson.invalid()
        val cursor = if (root["next_before_sequence"] == JsonNull) null else positiveLong(root, "next_before_sequence")
        if (cursor != null && (items.isEmpty() || cursor != items.first().sequence)) StrictJson.invalid()
        return ChatMessagePage(items, cursor)
    }
    fun turnWrite(raw: String): ChatTurnWrite = turnWrite(StrictJson.objectFrom(raw))
    fun turnWrite(root: JsonObject): ChatTurnWrite {
        StrictJson.keys(root, "turn", "created")
        val turn = StrictJson.objectValue(root["turn"])
        StrictJson.keys(turn, "request_id", "conversation_id", "turn_number", "user", "assistant")
        val user = message(StrictJson.objectValue(turn["user"]))
        val assistant = message(StrictJson.objectValue(turn["assistant"]))
        val id = StrictJson.uuid(turn, "conversation_id")
        val request = StrictJson.uuid(turn, "request_id")
        val number = positiveLong(turn, "turn_number")
        if (number > Long.MAX_VALUE / 2 || user.role != ChatRole.USER || assistant.role != ChatRole.ASSISTANT ||
            user.messageId == assistant.messageId || user.sequence != number * 2 - 1 || assistant.sequence != number * 2 ||
            listOf(user, assistant).any { it.conversationId != id || it.requestId != request }) StrictJson.invalid()
        return ChatTurnWrite(ChatTurn(request, id, number, user, assistant), StrictJson.boolean(root, "created"))
    }
}

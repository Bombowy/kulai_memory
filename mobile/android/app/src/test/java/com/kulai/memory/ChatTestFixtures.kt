package com.kulai.memory

import com.kulai.memory.models.*
import com.kulai.memory.transport.ConversationApi
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

internal fun conversation(id: UUID = UUID.randomUUID()) = Conversation(id, null, FIXTURE_DATE, FIXTURE_DATE)
internal fun chatTurn(conversationId: UUID, requestId: UUID, question: String = "Pytanie testowe",
    inputKind: ChatInputKind = ChatInputKind.TEXT, number: Long = 1, answer: String = "Odpowiedź testowa."): ChatTurn {
    fun message(role: ChatRole, content: String, sequence: Long) = ChatMessage(UUID.randomUUID(), conversationId,
        requestId, role, content, FIXTURE_DATE, sequence, if (role == ChatRole.USER) inputKind else null, emptyList())
    return ChatTurn(requestId, conversationId, number, message(ChatRole.USER, question, number * 2 - 1),
        message(ChatRole.ASSISTANT, answer, number * 2))
}
internal fun conversationJson(value: Conversation): JSONObject = JSONObject().put("conversation_id", value.conversationId.toString())
    .put("title", value.title ?: JSONObject.NULL).put("created_at", value.createdAt.toString())
    .put("last_activity_at", value.lastActivityAt.toString())
internal fun chatMessageJson(value: ChatMessage): JSONObject = JSONObject().put("message_id", value.messageId.toString())
    .put("conversation_id", value.conversationId.toString()).put("request_id", value.requestId.toString())
    .put("role", value.role.name.lowercase()).put("content", value.content).put("created_at", value.createdAt.toString())
    .put("sequence", value.sequence).put("input_kind", value.inputKind?.wire ?: JSONObject.NULL)
    .put("memory_ids", JSONArray(value.memoryIds.map { it.toString() }))
internal fun chatTurnJson(value: ChatTurn, created: Boolean = true): String = JSONObject().put("created", created)
    .put("turn", JSONObject().put("request_id", value.requestId.toString()).put("conversation_id", value.conversationId.toString())
        .put("turn_number", value.turnNumber).put("user", chatMessageJson(value.user))
        .put("assistant", chatMessageJson(value.assistant))).toString()

internal class FakeConversationApi : ConversationApi {
    val conversations = mutableListOf<Conversation>()
    val turns = mutableListOf<ChatTurn>()
    val requests = mutableListOf<Pair<UUID, ChatInputKind>>()
    val messageRequests = mutableListOf<Triple<UUID, Int, Long?>>()
    var beforeMessages: suspend () -> Unit = {}
    var before: suspend () -> Unit = {}
    var after: suspend () -> Unit = {}
    var lists = 0
    override suspend fun createConversation(): Conversation = conversation().also { conversations.add(0, it) }
    override suspend fun listConversations(limit: Int, cursor: ConversationCursor?): ConversationPage {
        lists++; return ConversationPage(conversations.toList(), null)
    }
    override suspend fun conversationMessages(id: UUID, limit: Int, beforeSequence: Long?): ChatMessagePage {
        messageRequests += Triple(id, limit, beforeSequence)
        beforeMessages()
        return ChatMessagePage(turns.filter { it.conversationId == id }.flatMap { listOf(it.user, it.assistant) }
            .filter { beforeSequence == null || it.sequence < beforeSequence }.takeLast(limit), null)
    }
    override suspend fun askConversation(id: UUID, requestId: UUID, question: String, inputKind: ChatInputKind): ChatTurnWrite {
        requests += requestId to inputKind
        before()
        turns.find { it.requestId == requestId }?.let { return ChatTurnWrite(it, false) }
        val turn = chatTurn(id, requestId, question, inputKind, turns.count { it.conversationId == id } + 1L)
        turns += turn
        after()
        return ChatTurnWrite(turn, true)
    }
}

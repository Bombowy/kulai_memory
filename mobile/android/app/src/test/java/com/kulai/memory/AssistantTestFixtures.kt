package com.kulai.memory

import com.kulai.memory.audio.*
import com.kulai.memory.models.*
import com.kulai.memory.transport.*
import kotlinx.coroutines.CompletableDeferred
import org.json.JSONObject
import java.time.OffsetDateTime
import java.util.UUID

internal val FIXTURE_ID: UUID = UUID.fromString("12345678-1234-1234-1234-123456789abc")
internal val FIXTURE_SESSION: UUID = UUID.fromString("aaaaaaaa-1234-1234-1234-123456789abc")
internal val FIXTURE_DATE: OffsetDateTime = OffsetDateTime.parse("2026-10-12T12:34:56.123456+02:00")
internal val ANSWER = AssistantAnswer(AssistantStatus.ANSWERED, "Synthetic dragon lives on Venus.",
    listOf(AssistantCitation(FIXTURE_ID, 1, 0.7395)))
internal val INSUFFICIENT = AssistantAnswer(AssistantStatus.INSUFFICIENT_CONTEXT,
    "Nie mam wystarczających informacji w pamięci.", emptyList())
internal fun historyItem(id: UUID = FIXTURE_ID, archived: Boolean = false) =
    MemoryHistoryItem(id, FIXTURE_DATE, 1, archived, "Synthetic preview")
internal fun detail(id: UUID = FIXTURE_ID) = MemoryDetail(id, FIXTURE_DATE, 1, false, "Synthetic full content")
internal fun answerJson(answer: AssistantAnswer = ANSWER): String = JSONObject()
    .put("status", if (answer.status == AssistantStatus.ANSWERED) "answered" else "insufficient_context")
    .put("answer", answer.answer).put("citations", org.json.JSONArray(answer.citations.map {
        JSONObject().put("memory_id", it.memoryId.toString()).put("rank", it.rank).put("score", it.score)
    })).toString()
internal fun itemJson(): JSONObject = JSONObject().put("id", FIXTURE_ID.toString())
    .put("created_at", FIXTURE_DATE.toString()).put("revision", 1).put("archived", false)
    .put("content_preview", "Synthetic preview")
internal fun pageJson(): String = JSONObject().put("items", org.json.JSONArray().put(itemJson()))
    .put("next_cursor", JSONObject.NULL).toString()
internal fun detailJson(): String = itemJson().apply { remove("content_preview"); put("content", "Synthetic full content") }.toString()
internal fun assistantEvent(type: String, seq: Int, payload: String = "{}", session: UUID = FIXTURE_SESSION): SocketEvent.Text =
    SocketEvent.Text("""{"schema_version":1,"type":"$type","session_id":"$session","sequence":$seq,"payload":$payload}""")

internal class FakeMemoryApi : MemoryApi {
    val asks = mutableListOf<String>()
    val lists = mutableListOf<Triple<Boolean, Int, MemoryHistoryCursor?>>()
    val details = mutableListOf<UUID>()
    val speechRequests = mutableListOf<String>()
    var planner: suspend (String) -> SpeechPlan = {
        SpeechPlan(listOf(SpeechSegment(it, if (it == INSUFFICIENT.answer) SpeechLanguage.PL else SpeechLanguage.EN)))
    }
    var answer: suspend (String) -> AssistantAnswer = { ANSWER }
    var list: suspend (Boolean, MemoryHistoryCursor?) -> MemoryHistoryPage = { archived, _ ->
        MemoryHistoryPage(listOf(historyItem(archived = archived)), null) }
    var detail: suspend (UUID) -> MemoryDetail = { com.kulai.memory.detail(it) }
    override suspend fun askMemory(query: String, topK: Int): AssistantAnswer { check(topK == 5); asks += query; return answer(query) }
    override suspend fun listMemories(archived: Boolean, limit: Int, cursor: MemoryHistoryCursor?): MemoryHistoryPage {
        lists += Triple(archived, limit, cursor); return list(archived, cursor)
    }
    override suspend fun getMemory(id: UUID): MemoryDetail { details += id; return detail(id) }
    override suspend fun planSpeech(answer: String): SpeechPlan { speechRequests += answer; return planner(answer) }
}
internal class QuestionRecorder : VoiceAudioRecorder {
    var requests = 0
    var stops = 0
    private var stop = CompletableDeferred<Unit>()
    override suspend fun record(onStarted: () -> Unit, onPcm: suspend (ByteArray) -> Boolean): RecordingEnd {
        requests++; stop = CompletableDeferred(); onStarted()
        check(onPcm(byteArrayOf(1, 2, 3, 4)))
        stop.await()
        return RecordingEnd.STOPPED
    }
    override fun requestStop() { stops++; stop.complete(Unit) }
}
internal class QuestionSockets : VoiceSocketFactory {
    val connections = mutableListOf<QuestionSocket>()
    val endpoints = mutableListOf<String>()
    override fun connect(endpoint: String, onEvent: (SocketEvent) -> Unit): VoiceSocket {
        endpoints += endpoint
        return QuestionSocket(onEvent).also(connections::add)
    }
}
internal class QuestionSocket(private val receive: (SocketEvent) -> Unit) : VoiceSocket {
    val text = mutableListOf<String>()
    val pcm = mutableListOf<ByteArray>()
    var cancelled = false
    fun emit(event: SocketEvent) = receive(event)
    override fun sendText(value: String): Boolean = (!cancelled).also { if (it) text += value }
    override fun sendBinary(value: ByteArray): Boolean = (!cancelled).also { if (it) pcm += value.copyOf() }
    override fun cancel() { cancelled = true }
    override fun close(code: Int): Boolean { cancelled = true; return true }
}

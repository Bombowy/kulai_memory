package com.kulai.memory.protocol

import com.kulai.memory.audio.VoiceAudioContract
import com.kulai.memory.models.AssistantAnswer
import org.json.JSONObject
import java.util.UUID

object AssistantClientMessages {
    fun questionStart(): String = JSONObject().put("schema_version", 1).put("type", "question.start")
        .put("audio", JSONObject().put("encoding", "pcm_s16le")
            .put("sample_rate", VoiceAudioContract.SAMPLE_RATE_HZ).put("channels", VoiceAudioContract.CHANNELS))
        .put("top_k", 5).toString()
    fun questionStop(): String = JSONObject().put("schema_version", 1).put("type", "question.stop").toString()
}

sealed interface AssistantEvent {
    val sessionId: UUID
    val sequence: Int
    data class Ready(override val sessionId: UUID, override val sequence: Int) : AssistantEvent
    data class Transcript(override val sessionId: UUID, override val sequence: Int, val text: String) : AssistantEvent
    data class Started(override val sessionId: UUID, override val sequence: Int) : AssistantEvent
    data class Completed(override val sessionId: UUID, override val sequence: Int, val answer: AssistantAnswer) : AssistantEvent
    data class Error(override val sessionId: UUID, override val sequence: Int) : AssistantEvent
}

class AssistantEventParser {
    fun parse(raw: String): AssistantEvent {
        if (raw.length > 2 * 1024 * 1024) StrictJson.invalid()
        val root = StrictJson.objectFrom(raw)
        StrictJson.keys(root, "schema_version", "type", "session_id", "sequence", "payload")
        if (StrictJson.integer(root, "schema_version") != 1) StrictJson.invalid()
        val id = StrictJson.uuid(root, "session_id")
        val sequence = StrictJson.positiveInt(root, "sequence")
        val payload = StrictJson.objectValue(root["payload"])
        return when (StrictJson.string(root, "type")) {
            "session.ready" -> { StrictJson.keys(payload); AssistantEvent.Ready(id, sequence) }
            "transcript.final" -> { StrictJson.keys(payload, "text")
                AssistantEvent.Transcript(id, sequence, StrictJson.string(payload, "text")) }
            "rag.started" -> { StrictJson.keys(payload); AssistantEvent.Started(id, sequence) }
            "assistant.completed" -> AssistantEvent.Completed(id, sequence, MemoryJson.answer(payload))
            "error" -> { StrictJson.keys(payload, "code", "message", "recoverable")
                StrictJson.string(payload, "code"); StrictJson.string(payload, "message"); StrictJson.boolean(payload, "recoverable")
                AssistantEvent.Error(id, sequence) }
            else -> StrictJson.invalid()
        }
    }
}

class AssistantSequenceValidator {
    private var session: UUID? = null
    private var previous = 0
    fun accept(event: AssistantEvent) {
        if ((session != null && session != event.sessionId) || event.sequence.toLong() != previous.toLong() + 1)
            StrictJson.invalid()
        session = event.sessionId; previous = event.sequence
    }
}

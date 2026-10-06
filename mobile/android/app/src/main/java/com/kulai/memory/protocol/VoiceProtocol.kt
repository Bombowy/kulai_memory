package com.kulai.memory.protocol

import org.json.JSONObject
import java.util.UUID

const val PROTOCOL_SCHEMA_VERSION = 1

data class PcmAudioContract(
    val encoding: String = "pcm_s16le",
    val sampleRate: Int = 16_000,
    val channels: Int = 1,
)

sealed interface ServerEvent {
    val sessionId: UUID
    val sequence: Int

    data class SessionReady(
        override val sessionId: UUID,
        override val sequence: Int,
    ) : ServerEvent

    data class TranscriptFinal(
        override val sessionId: UUID,
        override val sequence: Int,
        val text: String,
    ) : ServerEvent

    data class MemorySaving(
        override val sessionId: UUID,
        override val sequence: Int,
    ) : ServerEvent

    data class MemorySaved(
        override val sessionId: UUID,
        override val sequence: Int,
        val memoryId: UUID,
    ) : ServerEvent

    data class Error(
        override val sessionId: UUID,
        override val sequence: Int,
        val code: String,
        val message: String,
        val recoverable: Boolean,
    ) : ServerEvent
}

class ProtocolException(message: String) : IllegalArgumentException(message)

object ClientMessages {
    fun recordingStart(
        ingestionId: UUID,
        audio: PcmAudioContract = PcmAudioContract(),
    ): String =
        JSONObject()
            .put("schema_version", PROTOCOL_SCHEMA_VERSION)
            .put("type", "recording.start")
            .put("ingestion_id", ingestionId.toString())
            .put(
                "audio",
                JSONObject()
                    .put("encoding", audio.encoding)
                    .put("sample_rate", audio.sampleRate)
                    .put("channels", audio.channels),
            ).toString()

    fun recordingStop(): String =
        JSONObject()
            .put("schema_version", PROTOCOL_SCHEMA_VERSION)
            .put("type", "recording.stop")
            .toString()

    fun memoryRetry(): String =
        JSONObject()
            .put("schema_version", PROTOCOL_SCHEMA_VERSION)
            .put("type", "memory.retry")
            .toString()
}

class ServerEventParser {
    fun parse(raw: String): ServerEvent {
        val envelope = parseObject(raw)
        requireKeys(
            envelope,
            setOf("schema_version", "type", "session_id", "sequence", "payload"),
        )
        val schemaVersion = strictInt(envelope, "schema_version")
        if (schemaVersion != PROTOCOL_SCHEMA_VERSION) {
            throw ProtocolException("Unsupported protocol schema version.")
        }
        val type = strictString(envelope, "type")
        val sessionId = parseUuid(strictString(envelope, "session_id"), "session_id")
        val sequence = strictInt(envelope, "sequence")
        if (sequence < 1) {
            throw ProtocolException("Event sequence must be positive.")
        }
        val payload = envelope.opt("payload") as? JSONObject
            ?: throw ProtocolException("Event payload must be an object.")

        return when (type) {
            "session.ready" -> {
                requireKeys(payload, emptySet())
                ServerEvent.SessionReady(sessionId, sequence)
            }
            "transcript.final" -> {
                requireKeys(payload, setOf("text"))
                ServerEvent.TranscriptFinal(
                    sessionId,
                    sequence,
                    strictString(payload, "text", allowEmpty = true),
                )
            }
            "memory.saving" -> {
                requireKeys(payload, emptySet())
                ServerEvent.MemorySaving(sessionId, sequence)
            }
            "memory.saved" -> {
                requireKeys(payload, setOf("memory_id"))
                ServerEvent.MemorySaved(
                    sessionId,
                    sequence,
                    parseUuid(strictString(payload, "memory_id"), "memory_id"),
                )
            }
            "error" -> {
                requireKeys(payload, setOf("code", "message", "recoverable"))
                val code = strictString(payload, "code")
                val message = strictString(payload, "message")
                val recoverable = payload.opt("recoverable") as? Boolean
                    ?: throw ProtocolException("recoverable must be a boolean.")
                ServerEvent.Error(sessionId, sequence, code, message, recoverable)
            }
            else -> throw ProtocolException("Unknown server event type.")
        }
    }

    private fun parseObject(raw: String): JSONObject =
        try {
            JSONObject(raw)
        } catch (_: Exception) {
            throw ProtocolException("Malformed server event.")
        }

    private fun strictString(
        value: JSONObject,
        key: String,
        allowEmpty: Boolean = false,
    ): String {
        val parsed = value.opt(key) as? String
            ?: throw ProtocolException("$key must be a string.")
        if (!allowEmpty && parsed.isEmpty()) {
            throw ProtocolException("$key cannot be empty.")
        }
        return parsed
    }

    private fun strictInt(value: JSONObject, key: String): Int {
        val parsed = value.opt(key)
        if (parsed !is Int) {
            throw ProtocolException("$key must be an integer.")
        }
        return parsed
    }

    private fun parseUuid(raw: String, key: String): UUID =
        try {
            UUID.fromString(raw)
        } catch (_: IllegalArgumentException) {
            throw ProtocolException("$key must be a UUID.")
        }

    private fun requireKeys(value: JSONObject, expected: Set<String>) {
        val actual = value.keys().asSequence().toSet()
        if (actual != expected) {
            throw ProtocolException("Server event fields do not match protocol v1.")
        }
    }
}

class EventSequenceValidator {
    private var sessionId: UUID? = null
    private var lastSequence: Int? = null

    fun accept(event: ServerEvent) {
        val activeSession = sessionId
        if (activeSession != null && activeSession != event.sessionId) {
            throw ProtocolException("Server session changed within one connection.")
        }
        val previous = lastSequence
        if (previous != null && event.sequence != previous + 1) {
            throw ProtocolException("Server event sequence is not contiguous.")
        }
        if (previous == null && event.sequence < 1) {
            throw ProtocolException("Server event sequence must start at one or later.")
        }
        sessionId = event.sessionId
        lastSequence = event.sequence
    }
}

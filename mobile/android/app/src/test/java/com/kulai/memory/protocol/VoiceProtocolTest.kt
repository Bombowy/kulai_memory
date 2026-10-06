package com.kulai.memory.protocol

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.UUID

class VoiceProtocolTest {
    private val sessionId = UUID.randomUUID()

    @Test
    fun `recording start has the canonical audio contract`() {
        val ingestionId = UUID.randomUUID()

        val payload = JSONObject(ClientMessages.recordingStart(ingestionId))

        assertEquals(1, payload.getInt("schema_version"))
        assertEquals("recording.start", payload.getString("type"))
        assertEquals(ingestionId.toString(), payload.getString("ingestion_id"))
        val audio = payload.getJSONObject("audio")
        assertEquals("pcm_s16le", audio.getString("encoding"))
        assertEquals(16_000, audio.getInt("sample_rate"))
        assertEquals(1, audio.getInt("channels"))
    }

    @Test
    fun `event parser accepts all protocol v1 server events`() {
        val memoryId = UUID.randomUUID()
        val parser = ServerEventParser()

        assertTrue(parser.parse(event("session.ready", 1, "{}")) is ServerEvent.SessionReady)
        val final = parser.parse(event("transcript.final", 2, "{\"text\":\"note\"}"))
        assertEquals("note", (final as ServerEvent.TranscriptFinal).text)
        assertTrue(parser.parse(event("memory.saving", 3, "{}")) is ServerEvent.MemorySaving)
        val saved = parser.parse(
            event("memory.saved", 4, "{\"memory_id\":\"$memoryId\"}"),
        )
        assertEquals(memoryId, (saved as ServerEvent.MemorySaved).memoryId)
        val error = parser.parse(
            event(
                "error",
                5,
                "{\"code\":\"memory.save_failed\",\"message\":\"safe\",\"recoverable\":true}",
            ),
        )
        assertTrue((error as ServerEvent.Error).recoverable)
    }

    @Test
    fun `event parser rejects wrong schema and unknown events`() {
        val wrongSchema = event("session.ready", 1, "{}").replace(
            "\"schema_version\":1",
            "\"schema_version\":2",
        )
        assertThrows(ProtocolException::class.java) {
            ServerEventParser().parse(wrongSchema)
        }
        assertThrows(ProtocolException::class.java) {
            ServerEventParser().parse(event("transcript.partial", 1, "{\"text\":\"x\"}"))
        }
    }

    @Test
    fun `sequence validator rejects gaps and session changes`() {
        val validator = EventSequenceValidator()
        validator.accept(ServerEvent.SessionReady(sessionId, 3))
        assertThrows(ProtocolException::class.java) {
            validator.accept(ServerEvent.MemorySaving(sessionId, 5))
        }

        val changed = EventSequenceValidator()
        changed.accept(ServerEvent.SessionReady(sessionId, 1))
        assertThrows(ProtocolException::class.java) {
            changed.accept(ServerEvent.MemorySaving(UUID.randomUUID(), 2))
        }
    }

    @Test
    fun `parser forbids unexpected fields and malformed UUID`() {
        assertThrows(ProtocolException::class.java) {
            ServerEventParser().parse(
                "{\"schema_version\":1,\"type\":\"session.ready\",\"session_id\":\"bad\",\"sequence\":1,\"payload\":{}}",
            )
        }
        assertThrows(ProtocolException::class.java) {
            ServerEventParser().parse(
                event("session.ready", 1, "{}").dropLast(1) + ",\"extra\":true}",
            )
        }
    }

    private fun event(type: String, sequence: Int, payload: String): String =
        "{\"schema_version\":1,\"type\":\"$type\",\"session_id\":\"$sessionId\",\"sequence\":$sequence,\"payload\":$payload}"
}

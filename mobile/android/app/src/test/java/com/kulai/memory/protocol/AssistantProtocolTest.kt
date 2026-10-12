package com.kulai.memory.protocol

import com.kulai.memory.*
import com.kulai.memory.transport.SocketEvent
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.Parameterized
import java.util.UUID

class AssistantProtocolTest {
    @Test fun `question commands match exact transport keys and audio without ingestion`() {
        val start = JSONObject(AssistantClientMessages.questionStart())
        assertEquals(setOf("schema_version", "type", "audio", "top_k"), start.keys().asSequence().toSet())
        assertEquals(1, start.getInt("schema_version")); assertEquals("question.start", start.getString("type"))
        assertEquals(5, start.getInt("top_k"))
        val audio = start.getJSONObject("audio")
        assertEquals(setOf("encoding", "sample_rate", "channels"), audio.keys().asSequence().toSet())
        assertEquals("pcm_s16le", audio.getString("encoding")); assertEquals(16000, audio.getInt("sample_rate")); assertEquals(1, audio.getInt("channels"))
        val stop = JSONObject(AssistantClientMessages.questionStop())
        assertEquals(setOf("schema_version", "type"), stop.keys().asSequence().toSet())
        assertEquals(1, stop.getInt("schema_version")); assertEquals("question.stop", stop.getString("type"))
    }
    @Test fun `all assistant events and shared answers parse`() {
        val parser = AssistantEventParser()
        val events = listOf(assistantEvent("session.ready", 1), assistantEvent("transcript.final", 2, """{"text":"question"}"""),
            assistantEvent("rag.started", 3), assistantEvent("assistant.completed", 4, answerJson()))
        val sequence = AssistantSequenceValidator()
        val parsed = events.map { parser.parse(it.value).also(sequence::accept) }
        assertTrue(parsed[0] is AssistantEvent.Ready)
        assertEquals("question", (parsed[1] as AssistantEvent.Transcript).text)
        assertEquals(ANSWER, (parsed[3] as AssistantEvent.Completed).answer)
        assertEquals(INSUFFICIENT, (parser.parse(assistantEvent("assistant.completed", 4, answerJson(INSUFFICIENT)).value)
            as AssistantEvent.Completed).answer)
        assertTrue(parser.parse(assistantEvent("error", 1,
            """{"code":"x","message":"PRIVATE_SENTINEL","recoverable":false}""").value) is AssistantEvent.Error)
    }
    @Test fun `sequence must start at one and stay contiguous in the same session`() {
        assertThrows(ProtocolException::class.java) { AssistantSequenceValidator().accept(AssistantEvent.Ready(FIXTURE_SESSION, 2)) }
        val sequence = AssistantSequenceValidator(); sequence.accept(AssistantEvent.Ready(FIXTURE_SESSION, 1))
        assertThrows(ProtocolException::class.java) { sequence.accept(AssistantEvent.Started(FIXTURE_SESSION, 3)) }
        assertThrows(ProtocolException::class.java) { sequence.accept(AssistantEvent.Started(UUID.randomUUID(), 2)) }
    }
    @Test fun `assistant event is never accepted by voice note parser`() {
        for (type in listOf("rag.started", "assistant.completed")) assertThrows(ProtocolException::class.java) {
            ServerEventParser().parse(assistantEvent(type, 1, if (type == "assistant.completed") answerJson() else "{}").value)
        }
    }
}

@RunWith(Parameterized::class)
class InvalidAssistantProtocolTest(private val raw: String) {
    @Test fun `invalid events fail with authored error`() {
        val error = assertThrows(ProtocolException::class.java) { AssistantEventParser().parse(raw) }
        assertEquals("The server response was invalid.", error.message)
    }
    companion object {
        @JvmStatic @Parameterized.Parameters(name = "invalid event {index}") fun cases(): List<Array<String>> {
            val base = assistantEvent("session.ready", 1).value
            return listOf(
                base.replace("session.ready", "memory.saved"), base.replace("session.ready", "unknown"),
                base.replace("\"schema_version\":1", "\"schema_version\":2"),
                base.replace("\"sequence\":1", "\"sequence\":0"),
                base.replace("\"sequence\":1", "\"sequence\":\"1\""),
                base.replace(FIXTURE_SESSION.toString(), "1-1-1-1-1"),
                base.dropLast(1) + ",\"extra\":true}", base.replace("\"payload\":{}", "\"payload\":{\"extra\":true}"),
                base.replace("\"payload\":{}", "\"payload\":null"),
                assistantEvent("assistant.completed", 1, answerJson().replace("answered", "unknown")).value,
                assistantEvent("assistant.completed", 1, answerJson().replace("\"rank\":1", "\"rank\":0")).value,
                assistantEvent("transcript.final", 1, """{"text":4}""").value,
                assistantEvent("error", 1, """{"code":"x","message":"PRIVATE","recoverable":"false"}""").value,
            ).map { arrayOf(it) }
        }
    }
}

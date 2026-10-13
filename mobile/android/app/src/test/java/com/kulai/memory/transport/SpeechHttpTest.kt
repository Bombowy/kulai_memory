package com.kulai.memory.transport

import com.kulai.memory.*
import com.kulai.memory.models.*
import com.kulai.memory.protocol.*
import kotlinx.coroutines.*
import okhttp3.mockwebserver.*
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.Parameterized
import java.util.concurrent.TimeUnit

internal fun speechJson(plan: SpeechPlan): String = JSONObject().put("segments", org.json.JSONArray(plan.segments.map {
    JSONObject().put("text", it.text).put("language", if (it.language == SpeechLanguage.PL) "pl" else "en") })).toString()

class SpeechHttpTest {
    @Test fun `valid PL EN mixed exact whitespace punctuation plans and UTF8 body`() = runBlocking {
        val cases = listOf(SpeechPlan(listOf(SpeechSegment("Żółć i pamięć.\n", SpeechLanguage.PL))),
            SpeechPlan(listOf(SpeechSegment("English answer.\n", SpeechLanguage.EN))), MIXED_SPEECH)
        withServer { server, api, _ ->
            for (plan in cases) {
                val answer = plan.segments.joinToString("") { it.text }
                server.enqueue(MockResponse().setBody(speechJson(plan)))
                assertEquals(plan, api.planSpeech(answer))
                val request = server.takeRequest(); assertEquals("/api/assistant/speech-plan", request.path)
                assertEquals("application/json; charset=utf-8", request.getHeader("Content-Type"))
                val bytes = request.body.readByteArray(); val raw = bytes.toString(Charsets.UTF_8)
                assertArrayEquals(bytes, raw.toByteArray(Charsets.UTF_8))
                val parsed = JSONObject(raw); assertEquals(setOf("answer"), parsed.keys().asSequence().toSet())
                assertEquals(answer, parsed.getString("answer"))
            }
        }
    }
    @Test fun `speech cancellation cancels real OkHttp call`() = runBlocking {
        withServer { server, api, client ->
            server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
            val job = launch { api.planSpeech("Synthetic answer") }
            withContext(Dispatchers.IO) { checkNotNull(server.takeRequest(5, TimeUnit.SECONDS)) }
            val call = client.dispatcher.runningCalls().single(); job.cancelAndJoin(); assertTrue(call.isCanceled())
        }
    }
    @Test fun `invalid local text and malformed UTF8 never repair or expose answer`() = runBlocking {
        withServer { server, api, _ ->
            for (answer in listOf("", " ", "x".repeat(6001))) {
                try { api.planSpeech(answer); fail() } catch (e: IllegalArgumentException) { assertEquals("Invalid speech request.", e.message) }
            }
            assertEquals(0, server.requestCount)
            server.enqueue(MockResponse().setBody(okio.Buffer().write(byteArrayOf(0xc3.toByte(), 0x28))))
            try { api.planSpeech("PRIVATE answer"); fail() }
            catch (e: MemoryHttpException) { assertEquals(HttpFailure.INVALID_RESPONSE, e.failure); assertFalse(e.toString().contains("PRIVATE")) }
        }
    }
}

@RunWith(Parameterized::class)
class InvalidSpeechPlanTest(private val raw: String, private val answer: String) {
    @Test fun `strict speech plans rejected without rewrite or fallback`() {
        val error = assertThrows(ProtocolException::class.java) { MemoryJson.speech(raw, answer) }
        assertEquals("The server response was invalid.", error.message)
    }
    companion object {
        @JvmStatic @Parameterized.Parameters(name = "invalid plan {index}") fun cases(): List<Array<String>> {
            val valid = """{"segments":[{"text":"Answer.","language":"en"}]}"""
            return listOf(
                arrayOf(valid.replace("\"en\"", "\"de\""), "Answer."),
                arrayOf(valid.dropLast(1) + ",\"answer\":\"PRIVATE\"}", "Answer."),
                arrayOf(valid.replace("\"language\":\"en\"", "\"language\":\"en\",\"extra\":true"), "Answer."),
                arrayOf("""{"segments":[{"text":"Answer."}]}""", "Answer."),
                arrayOf(valid.replace("Answer.", ""), "Answer."), arrayOf("""{"segments":[]}""", "Answer."),
                arrayOf(speechJson(SpeechPlan(List(65) { SpeechSegment("x", SpeechLanguage.PL) })), "x".repeat(65)),
                arrayOf(speechJson(SpeechPlan(listOf(SpeechSegment("x".repeat(6001), SpeechLanguage.PL)))), "x".repeat(6001)),
                arrayOf(valid, "Changed."), arrayOf(valid, " Answer."), arrayOf(valid, "Answer. "),
                arrayOf(valid.replace("\"en\"", "123"), "Answer."), arrayOf("{", "Answer."),
                arrayOf(valid + " trailing", "Answer."), arrayOf(valid.replace("\"text\":\"Answer.\"", "\"text\":null"), "Answer."))
        }
    }
}

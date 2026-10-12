package com.kulai.memory.transport

import com.kulai.memory.*
import com.kulai.memory.models.*
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.mockwebserver.*
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.Parameterized
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

class MemoryHttpClientTest {
    @Test fun `active archived cursor and detail use exact endpoints`() = runBlocking {
        withServer { server, api, _ ->
            server.enqueue(MockResponse().setBody(pageJson()))
            val page = api.listMemories(false)
            assertEquals(FIXTURE_ID, page.items.single().id)
            assertNull(page.nextCursor)
            val active = server.takeRequest().requestUrl!!
            assertEquals("/api/memories", active.encodedPath); assertEquals("false", active.queryParameter("archived"))
            assertEquals("20", active.queryParameter("limit"))
            server.enqueue(MockResponse().setBody(pageJson().replace("false", "true")))
            val cursor = MemoryHistoryCursor(FIXTURE_DATE, FIXTURE_ID)
            assertTrue(api.listMemories(true, 50, cursor).items.single().archived)
            val archived = server.takeRequest().requestUrl!!
            assertEquals("true", archived.queryParameter("archived"))
            assertEquals(FIXTURE_DATE.toString(), archived.queryParameter("before_created_at"))
            assertEquals(FIXTURE_ID.toString(), archived.queryParameter("before_id"))
            assertNull(archived.queryParameter("offset"))
            server.enqueue(MockResponse().setBody(detailJson()))
            assertEquals(detail(), api.getMemory(FIXTURE_ID))
            assertEquals("/api/memories/$FIXTURE_ID", server.takeRequest().path)
            server.enqueue(MockResponse().setBody(detailJson().replace("false", "true")))
            assertTrue(api.getMemory(FIXTURE_ID).archived); server.takeRequest()
        }
    }
    @Test fun `answered insufficient and Polish bytes are exact UTF8`() = runBlocking {
        withServer { server, api, _ ->
            server.enqueue(MockResponse().setBody(answerJson()))
            val query = "Gdzie mieszka mój zielony smok? Żółć i pamięć."
            assertEquals(ANSWER, api.askMemory(query))
            val request = server.takeRequest()
            assertEquals("POST", request.method); assertEquals("/api/assistant/ask", request.path)
            assertEquals("application/json; charset=utf-8", request.getHeader("Content-Type"))
            val bytes = request.body.readByteArray()
            val raw = bytes.toString(Charsets.UTF_8)
            assertArrayEquals(raw.toByteArray(Charsets.UTF_8), bytes)
            val parsed = JSONObject(raw)
            assertEquals(query, parsed.getString("query")); assertEquals(5, parsed.getInt("top_k"))
            assertEquals(setOf("query", "top_k"), parsed.keys().asSequence().toSet())
            server.enqueue(MockResponse().setBody(answerJson(INSUFFICIENT)))
            assertEquals(INSUFFICIENT, api.askMemory("Synthetic unsupported question"))
        }
    }
    @Test fun `history parses nonnull cursor and preserves timestamp precision`() = runBlocking {
        withServer { server, api, _ ->
            val raw = JSONObject(pageJson()).put("next_cursor", JSONObject()
                .put("before_created_at", FIXTURE_DATE.toString()).put("before_id", FIXTURE_ID.toString())).toString()
            server.enqueue(MockResponse().setBody(raw))
            assertEquals(MemoryHistoryCursor(FIXTURE_DATE, FIXTURE_ID), api.listMemories(false).nextCursor)
            server.enqueue(MockResponse().setBody("""{"items":[],"next_cursor":null}"""))
            assertTrue(api.listMemories(false).items.isEmpty())
        }
    }
    @Test fun `malformed UTF8 and bounded responses are rejected and closed`() = runBlocking {
        withServer { server, api, _ ->
            server.enqueue(MockResponse().setBody(okio.Buffer().write(byteArrayOf(0xc3.toByte(), 0x28))))
            try { api.askMemory("Synthetic"); fail() } catch (e: MemoryHttpException) { assertEquals(HttpFailure.INVALID_RESPONSE, e.failure) }
            server.enqueue(MockResponse().setBody("x".repeat(2 * 1024 * 1024 + 1)))
            try { api.getMemory(FIXTURE_ID); fail() } catch (e: MemoryHttpException) { assertEquals(HttpFailure.INVALID_RESPONSE, e.failure) }
            server.enqueue(MockResponse().setBody(detailJson()))
            assertEquals(detail(), api.getMemory(FIXTURE_ID))
        }
    }
    @Test fun `HTTP errors never echo server content`() = runBlocking {
        withServer { server, api, _ ->
            for (code in listOf(400, 404, 422, 500, 503)) {
                server.enqueue(MockResponse().setResponseCode(code).setBody("PRIVATE_QUERY DB_PASSWORD"))
                val error = try { api.getMemory(FIXTURE_ID); error("Expected failure") } catch (e: MemoryHttpException) { e }
                assertFalse(error.toString().contains("PRIVATE")); assertNull(error.cause)
                assertEquals(when (code) { 404 -> HttpFailure.NOT_FOUND; 503 -> HttpFailure.UNAVAILABLE; else -> HttpFailure.OPERATION_FAILED }, error.failure)
            }
        }
    }
    @Test fun `cancellation cancels real OkHttp calls for history detail and ask`() = runBlocking {
        withServer { server, api, client ->
            val cancellations = AtomicInteger()
            // Each request is held by the server until cancellation, without timing sleeps.
            for (operation in listOf<suspend () -> Unit>({ api.listMemories(false) }, { api.getMemory(FIXTURE_ID) }, { api.askMemory("Synthetic") })) {
                server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
                val job = launch { operation() }
                withContext(Dispatchers.IO) { checkNotNull(server.takeRequest(5, TimeUnit.SECONDS)) }
                assertTrue(client.dispatcher.runningCalls().isNotEmpty())
                val call = client.dispatcher.runningCalls().last()
                job.cancelAndJoin()
                assertTrue(call.isCanceled()); cancellations.incrementAndGet()
            }
            assertEquals(3, cancellations.get())
        }
    }
    @Test fun `invalid local queries never reach network`() = runBlocking {
        withServer { server, api, _ ->
            for (query in listOf("", "  ", "x".repeat(10001))) {
                try { api.askMemory(query); fail() } catch (_: IllegalArgumentException) { }
            }
            assertEquals(0, server.requestCount)
        }
    }
}

internal suspend fun withServer(block: suspend (MockWebServer, MemoryHttpClient, OkHttpClient) -> Unit) {
    val server = MockWebServer(); server.start()
    val client = OkHttpClient.Builder().readTimeout(5, TimeUnit.SECONDS).build()
    try { block(server, MemoryHttpClient(server.url("/").toString(), client), client) }
    finally { client.dispatcher.cancelAll(); client.connectionPool.evictAll(); client.dispatcher.executorService.shutdown(); server.shutdown() }
}

@RunWith(Parameterized::class)
class InvalidMemoryResponseTest(private val kind: String, private val raw: String) {
    @Test fun `strict responses fail without repair or private errors`() = runBlocking {
        withServer { server, api, _ ->
            server.enqueue(MockResponse().setBody(raw))
            val error = try {
                when (kind) { "answer" -> api.askMemory("Synthetic"); "history" -> api.listMemories(false); else -> api.getMemory(FIXTURE_ID) }
                error("Expected failure")
            } catch (e: MemoryHttpException) { e }
            assertEquals(HttpFailure.INVALID_RESPONSE, error.failure)
            assertEquals("The server response was invalid.", error.message); assertNull(error.cause)
        }
    }
    companion object {
        @JvmStatic @Parameterized.Parameters(name = "{0} invalid {index}") fun cases(): List<Array<String>> {
            val answer = answerJson()
            val badAnswers = listOf("{", answer.dropLast(1) + ",\"context\":\"PRIVATE\"}",
                answer.replace("answered", "other"), answer.replace("\"answer\":\"${ANSWER.answer}\"", "\"answer\":4"),
                answer.replace(ANSWER.answer, "   "), answer.replace("\"rank\":1", "\"rank\":0"),
                answer.replace("\"rank\":1", "\"rank\":1.0"), answer.replace("\"score\":0.7395", "\"score\":\"0.7395\""),
                answer.replace("\"score\":0.7395", "\"score\":1e999"), answer.replace(FIXTURE_ID.toString(), "1-1-1-1-1"),
                answer.replace("answered", "insufficient_context"),
                answerJson(INSUFFICIENT).replace("insufficient_context", "answered"),
                """{"status":"answered","answer":"x"}""", """{'status':'answered'}""", answer + " trailing")
            val item = itemJson().toString()
            val badPages = listOf("{", pageJson().dropLast(1) + ",\"metadata\":{}}", pageJson().replace("\"items\":[", "\"items\":\""),
                pageJson().replace("\"revision\":1", "\"revision\":0"), pageJson().replace("\"archived\":false", "\"archived\":\"false\""),
                pageJson().replace(FIXTURE_DATE.toString(), "2026-10-12T12:34:56"), pageJson().replace("Synthetic preview", "x".repeat(201)),
                pageJson().replace("\"next_cursor\":null", "\"next_cursor\":{\"before_id\":\"$FIXTURE_ID\"}"),
                """{"items":[$item,$item],"next_cursor":null}""")
            val badDetails = listOf(detailJson().replace("\"revision\":1", "\"revision\":\"1\""),
                detailJson().replace(FIXTURE_ID.toString(), "bad"), detailJson().dropLast(1) + ",\"ingestion_id\":\"PRIVATE\"}",
                detailJson().replace("\"content\":\"Synthetic full content\"", "\"content\":null"))
            return badAnswers.map { arrayOf("answer", it) } + badPages.map { arrayOf("history", it) } + badDetails.map { arrayOf("detail", it) }
        }
    }
}

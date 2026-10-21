package com.kulai.memory

import com.kulai.memory.models.*
import com.kulai.memory.protocol.*
import com.kulai.memory.transport.*
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.mockwebserver.*
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.util.UUID
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

class ChatTransportTest {
    @Test fun `all conversation HTTP paths keep memory prefix auth fresh bearer cursor and request UUID`() = runBlocking {
        val server = MockWebServer(); server.start(); val client = OkHttpClient()
        val base = server.url("/memory/").toString(); val device = UUID.randomUUID()
        val active = AtomicReference(DeviceCredential(device, "a".repeat(43), base))
        val api = MemoryHttpClient(base, client, RequestAuthentication(base, credentials = CredentialProvider { active.get() }))
        val conversation = conversation(); val requestId = UUID.randomUUID()
        val turn = chatTurn(conversation.conversationId, requestId, "PL: żółw. EN: turtle.")
        try {
            server.enqueue(MockResponse().setResponseCode(201).setBody(conversationJson(conversation).toString()))
            assertEquals(conversation, api.createConversation())
            server.enqueue(MockResponse().setBody(JSONObject().put("conversations", JSONArray().put(conversationJson(conversation)))
                .put("next_cursor", JSONObject.NULL).toString()))
            api.listConversations(cursor = ConversationCursor(FIXTURE_DATE, conversation.conversationId))
            server.enqueue(MockResponse().setBody(JSONObject().put("messages", JSONArray().put(chatMessageJson(turn.user)).put(chatMessageJson(turn.assistant)))
                .put("next_before_sequence", JSONObject.NULL).toString()))
            api.conversationMessages(conversation.conversationId, beforeSequence = 9)
            server.enqueue(MockResponse().setBody(JSONObject().put("messages", JSONArray().put(chatMessageJson(turn.user)))
                .put("next_before_sequence", JSONObject.NULL).toString()))
            assertEquals(listOf(turn.user), api.conversationMessages(conversation.conversationId, limit = 1, beforeSequence = 2).messages)
            active.set(DeviceCredential(device, "b".repeat(43), base))
            for (created in listOf(true, false)) {
                server.enqueue(MockResponse().setBody(chatTurnJson(turn, created)))
                assertEquals(created, api.askConversation(conversation.conversationId, requestId, turn.user.content).created)
            }
            val outgoing = (1..6).map { server.takeRequest(5, TimeUnit.SECONDS)!! }
            assertEquals(listOf("POST", "GET", "GET", "GET", "POST", "POST"), outgoing.map { it.method })
            assertTrue(outgoing.all { it.path!!.startsWith("/memory/api/conversations") && it.getHeader("X-Kulai-Device-ID") == device.toString() })
            assertTrue(outgoing[1].requestUrl!!.queryParameterNames.containsAll(listOf("before_activity_at", "before_id")))
            assertEquals("9", outgoing[2].requestUrl!!.queryParameter("before_sequence"))
            assertEquals("2", outgoing[3].requestUrl!!.queryParameter("before_sequence"))
            assertEquals("1", outgoing[3].requestUrl!!.queryParameter("limit"))
            assertTrue(outgoing[0].getHeader("Authorization")!!.endsWith("a".repeat(43)))
            assertTrue(outgoing[4].getHeader("Authorization")!!.endsWith("b".repeat(43)))
            assertEquals(outgoing[4].body.readUtf8(), outgoing[5].body.readUtf8())
        } finally { client.dispatcher.executorService.shutdown(); client.connectionPool.evictAll(); server.shutdown() }
    }
    @Test fun `conversation HTTP denies missing credentials redirects failures and cancels actual calls`() = runBlocking {
        val server = MockWebServer(); val other = MockWebServer(); server.start(); other.start()
        val client = OkHttpClient(); val base = server.url("/memory/").toString()
        try {
            val denied = MemoryHttpClient(base, client)
            try { denied.createConversation(); fail() } catch (e: MemoryHttpException) { assertEquals(HttpFailure.AUTHENTICATION_REQUIRED, e.failure) }
            assertEquals(0, server.requestCount)
            val api = MemoryHttpClient(base, client, RequestAuthentication(base, AuthenticationMode.LOCAL_DEV))
            for (status in listOf(302, 409, 503)) {
                server.enqueue(MockResponse().setResponseCode(status).setHeader("Location", other.url("/outside"))
                    .setBody("PRIVATE_BACKEND_DETAILS"))
                try { api.createConversation(); fail() } catch (e: MemoryHttpException) { assertFalse(e.message!!.contains("PRIVATE")) }
            }
            assertEquals(0, other.requestCount)
            server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
            val request = async(Dispatchers.Default) { api.listConversations() }
            withContext(Dispatchers.IO) { repeat(4) { server.takeRequest(5, TimeUnit.SECONDS)!! } }
            request.cancelAndJoin()
            withTimeout(5000) { while (client.dispatcher.runningCallsCount() != 0) delay(10) }
        } finally { client.dispatcher.executorService.shutdown(); client.connectionPool.evictAll(); server.shutdown(); other.shutdown() }
    }
    @Test fun `strict V2 JSON rejects foreign identities partial pairs unexpected fields and V1 final`() {
        val id = UUID.randomUUID(); val turn = chatTurn(id, UUID.randomUUID())
        assertEquals(turn, ConversationJson.turnWrite(chatTurnJson(turn)).turn)
        for (body in listOf(JSONObject(chatTurnJson(turn)).put("private", "PRIVATE"),
            JSONObject(chatTurnJson(turn)).apply { getJSONObject("turn").getJSONObject("assistant").put("role", "user") },
            JSONObject(chatTurnJson(turn)).apply { getJSONObject("turn").put("conversation_id", UUID.randomUUID().toString()) },
            JSONObject(chatTurnJson(turn)).apply { getJSONObject("turn").getJSONObject("assistant").put("sequence", 1) })) {
            try { ConversationJson.turnWrite(body.toString()); fail() } catch (e: ProtocolException) { assertFalse(e.message!!.contains("PRIVATE")) }
        }
        val event = assistantEvent("conversation.completed", 4, chatTurnJson(turn)).value
        assertTrue(AssistantEventParser(true).parse(event) is AssistantEvent.ConversationCompleted)
        try { AssistantEventParser().parse(event); fail() } catch (_: ProtocolException) { }
    }
    @Test fun `conversation business error closes 1008 without invalidating pairing`() = runBlocking {
        val server = MockWebServer(); val client = OkHttpClient(); val rejected = AtomicInteger()
        server.enqueue(MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                webSocket.send(assistantEvent("error", 1, """{"code":"conversation.not_found","message":"Not found","recoverable":false}""").value)
                webSocket.close(1008, "")
            }
        })); server.start()
        val base = server.url("/memory/").toString(); val closed = CompletableDeferred<Unit>()
        val credential = DeviceCredential(UUID.randomUUID(), "a".repeat(43), base)
        try {
            val socket = OkHttpVoiceSocketFactory(client, RequestAuthentication(base, credentials = CredentialProvider { credential },
                onRejected = { rejected.incrementAndGet() })).connect(server.url("/memory/ws/assistant").toString().replaceFirst("http", "ws")) {
                if (it is SocketEvent.Closed) closed.complete(Unit)
            }
            // OkHttp queues messages while the handshake is in flight.
            socket.sendText(AssistantClientMessages.conversationStart(UUID.randomUUID(), UUID.randomUUID()))
            withTimeout(5000) { closed.await() }
            assertEquals(0, rejected.get())
        } finally { client.dispatcher.executorService.shutdown(); client.connectionPool.evictAll(); server.shutdown() }
    }
}

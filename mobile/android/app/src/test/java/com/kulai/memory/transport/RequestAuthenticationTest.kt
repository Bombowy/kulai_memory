package com.kulai.memory.transport

import com.kulai.memory.*
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.mockwebserver.*
import org.junit.Assert.*
import org.junit.Test
import java.security.SecureRandom
import java.util.Base64
import java.util.UUID
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

class RequestAuthenticationTest {
    private fun token(): String = Base64.getUrlEncoder().withoutPadding().encodeToString(
        ByteArray(32).also { SecureRandom().nextBytes(it) })

    private fun client() = OkHttpClient.Builder().readTimeout(5, TimeUnit.SECONDS).build()
    private fun cleanup(client: OkHttpClient, vararg servers: MockWebServer) {
        client.dispatcher.cancelAll(); client.connectionPool.evictAll()
        client.dispatcher.executorService.shutdown(); servers.forEach { it.shutdown() }
    }

    @Test fun `all HTTP routes use current device credential only in headers`() = runBlocking {
        val server = MockWebServer(); server.start(); val client = client()
        val device = UUID.randomUUID(); val first = token(); val second = token()
        val snapshot = AtomicReference(DeviceCredential(device, first))
        val auth = RequestAuthentication(server.url("/").toString(), credentials = CredentialProvider { snapshot.get() })
        val api = MemoryHttpClient(server.url("/").toString(), client, auth)
        try {
            server.enqueue(MockResponse().setBody(pageJson())); api.listMemories(false)
            server.enqueue(MockResponse().setBody(detailJson())); api.getMemory(FIXTURE_ID)
            server.enqueue(MockResponse().setBody(answerJson())); api.askMemory("Synthetic")
            snapshot.set(DeviceCredential(device, second))
            server.enqueue(MockResponse().setBody("""{"segments":[{"text":"Synthetic","language":"en"}]}"""))
            api.planSpeech("Synthetic")
            for (index in 0..3) {
                val request = server.takeRequest(5, TimeUnit.SECONDS)!!
                assertEquals("Bearer ${if (index == 3) second else first}", request.getHeader("Authorization"))
                assertEquals(device.toString(), request.getHeader("X-Kulai-Device-ID"))
                assertEquals(1, request.headers.values("Authorization").size)
                assertFalse(request.path!!.contains(first)); assertFalse(request.path!!.contains(second))
                val body = request.body.readUtf8()
                assertFalse(body.contains(first)); assertFalse(body.contains(second))
            }
        } finally { cleanup(client, server) }
    }

    @Test fun `SECURE without credentials rejects HTTP and both WS before network`() = runBlocking {
        val server = MockWebServer(); server.start(); val client = client()
        val auth = RequestAuthentication(server.url("/").toString())
        val api = MemoryHttpClient(server.url("/").toString(), client, auth)
        try {
            val failure = try { api.listMemories(false); error("Expected failure") } catch (e: MemoryHttpException) { e }
            assertEquals(HttpFailure.AUTHENTICATION_REQUIRED, failure.failure); assertNull(failure.cause)
            for (path in listOf("/ws/memory", "/ws/assistant")) {
                val events = mutableListOf<SocketEvent>()
                val socket = OkHttpVoiceSocketFactory(client, auth).connect(
                    server.url(path).toString().replaceFirst("http", "ws"), events::add)
                assertEquals(listOf(SocketEvent.Failed), events)
                assertFalse(socket.sendText("private")); assertFalse(socket.sendBinary(byteArrayOf(0, 0)))
                socket.cancel()
            }
            assertEquals(0, server.requestCount)
        } finally { cleanup(client, server) }
    }

    @Test fun `both websocket handshakes share HTTP header policy and rotate snapshot`() = runBlocking {
        val server = MockWebServer(); server.start(); val client = client()
        val device = UUID.randomUUID(); val snapshot = AtomicReference(DeviceCredential(device, token()))
        val auth = RequestAuthentication(server.url("/").toString(), credentials = CredentialProvider { snapshot.get() })
        try {
            for (path in listOf("/ws/memory", "/ws/assistant")) {
                val secret = token(); snapshot.set(DeviceCredential(device, secret))
                server.enqueue(MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) { webSocket.close(code, null) }
                }))
                val opened = CompletableDeferred<Unit>(); val closed = CompletableDeferred<Unit>()
                val socket = OkHttpVoiceSocketFactory(client, auth).connect(
                    server.url(path).toString().replaceFirst("http", "ws")) {
                    if (it == SocketEvent.Opened) opened.complete(Unit)
                    if (it is SocketEvent.Closed) closed.complete(Unit)
                }
                withTimeout(5000) { opened.await() }
                val request = server.takeRequest(5, TimeUnit.SECONDS)!!
                assertEquals(path, request.path)
                assertEquals("Bearer $secret", request.getHeader("Authorization"))
                assertEquals(device.toString(), request.getHeader("X-Kulai-Device-ID"))
                assertNull(request.getHeader("Sec-WebSocket-Protocol"))
                socket.close(); withTimeout(5000) { closed.await() }
            }
        } finally { cleanup(client, server) }
    }

    @Test fun `authentication denial and provider failure never echo secrets or downgrade`() = runBlocking {
        val server = MockWebServer(); server.start(); val client = client(); val secret = token()
        try {
            val auth = RequestAuthentication(server.url("/").toString(), AuthenticationMode.LOCAL_DEV,
                CredentialProvider { DeviceCredential(UUID.randomUUID(), secret) })
            val api = MemoryHttpClient(server.url("/").toString(), client, auth)
            for (code in listOf(401, 403)) {
                server.enqueue(MockResponse().setResponseCode(code).setBody(secret))
                val failure = try { api.listMemories(false); error("Expected failure") } catch (e: MemoryHttpException) { e }
                assertEquals(HttpFailure.AUTHENTICATION_REQUIRED, failure.failure)
                assertFalse(failure.toString().contains(secret)); assertNull(failure.cause)
            }
            val broken = RequestAuthentication(server.url("/").toString(), AuthenticationMode.LOCAL_DEV,
                CredentialProvider { throw IllegalStateException(secret) })
            try { broken.authorize(Request.Builder().url(server.url("/")).build()); fail() }
            catch (e: AuthenticationException) { assertFalse(e.toString().contains(secret)); assertNull(e.cause) }
            assertEquals(2, server.requestCount)
            assertFalse(DeviceCredential(UUID.randomUUID(), secret).toString().contains(secret))
        } finally { cleanup(client, server) }
    }

    @Test fun `origin binding query credentials and cleartext remote are rejected`() {
        val credential = DeviceCredential(UUID.randomUUID(), token())
        val auth = RequestAuthentication("http://127.0.0.1:8000", credentials = CredentialProvider { credential })
        for (url in listOf("http://127.0.0.1:8001/api/memories", "https://127.0.0.1:8000/api/memories",
            "http://evil.invalid:8000/api/memories", "http://127.0.0.1:8000/api/memories?%61ccess_token=x")) {
            try { auth.authorize(Request.Builder().url(url).build()); fail() } catch (_: AuthenticationException) { }
        }
        for ((origin, mode) in listOf("http://example.invalid" to AuthenticationMode.SECURE,
            "https://example.invalid" to AuthenticationMode.LOCAL_DEV)) {
            try {
                RequestAuthentication(origin, mode, CredentialProvider { credential })
                    .authorize(Request.Builder().url(origin).build()); fail()
            } catch (_: AuthenticationException) { }
        }
        try {
            auth.authorize(Request.Builder().url("http://127.0.0.1:8000")
                .header("Authorization", "unexpected").build()); fail()
        } catch (_: AuthenticationException) { }
        try { DeviceCredential(UUID.randomUUID(), "private\nheader"); fail() }
        catch (e: IllegalArgumentException) { assertEquals("Invalid credentials.", e.message) }
    }

    @Test fun `HTTP redirects never forward authenticated requests even to same origin`() = runBlocking {
        val server = MockWebServer(); server.start(); val target = MockWebServer(); target.start(); val client = client()
        val auth = RequestAuthentication(server.url("/").toString(), credentials = CredentialProvider {
            DeviceCredential(UUID.randomUUID(), token()) })
        val api = MemoryHttpClient(server.url("/").toString(), client, auth)
        try {
            for (location in listOf(target.url("/steal"), server.url("/different"))) {
                server.enqueue(MockResponse().setResponseCode(307).addHeader("Location", location))
                try { api.askMemory("Synthetic"); fail() } catch (e: MemoryHttpException) {
                    assertEquals(HttpFailure.OPERATION_FAILED, e.failure)
                }
            }
            assertEquals(2, server.requestCount); assertEquals(0, target.requestCount)
        } finally { cleanup(client, server, target) }
    }
}

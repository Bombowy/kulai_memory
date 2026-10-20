package com.kulai.memory.pairing

import com.kulai.memory.transport.*
import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.MockResponse
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.util.UUID
import java.time.Instant

class ServerOriginTest {
    @Test fun `pairing lifecycle and refresh stay under memory prefix`() = runBlocking {
        val server = MockWebServer(); server.start(); val client = OkHttpClient()
        val api = PairingHttpClient(ServerOrigin(server.url("/memory/").toString(), true), client)
        val pending = PendingPairing(UUID.randomUUID(), PairingController.randomSecret(), PairingController.randomSecret(), UUID.randomUUID(), "Phone")
        val device = UUID.randomUUID()
        val reply = JSONObject().put("state", "approved").put("device_id", device.toString())
            .put("expires_at", Instant.now().plusSeconds(3600).toString()).put("verification_code", JSONObject.NULL).toString()
        fun credential() = JSONObject().put("device_id", device.toString()).put("token_id", UUID.randomUUID().toString())
            .put("access_token", PairingController.randomSecret()).put("expires_at", Instant.now().plusSeconds(600).toString())
            .put("session_id", UUID.randomUUID().toString()).put("refresh_id", UUID.randomUUID().toString())
            .put("refresh_credential", "krf1_" + PairingController.randomSecret()).put("refresh_expires_at", Instant.now().plusSeconds(1800).toString())
            .put("session_expires_at", Instant.now().plusSeconds(3600).toString()).toString()
        try {
            server.enqueue(MockResponse().setBody(reply)); api.register(pending)
            server.enqueue(MockResponse().setBody(reply)); api.status(pending)
            server.enqueue(MockResponse().setBody(credential())); val claimed = api.claim(pending)
            server.enqueue(MockResponse().setBody(reply)); api.cancel(pending)
            server.enqueue(MockResponse().setBody(credential())); assertNotNull(api.renew(claimed).session)
            for ((index, path) in listOf("pairing/request", "pairing/status", "pairing/claim", "pairing/cancel", "security/refresh").withIndex()) {
                val sent = server.takeRequest()
                assertEquals("/memory/api/$path", sent.path); assertEquals("POST", sent.method)
                assertEquals(when (index) {
                    0 -> "Pairing ${pending.invitationSecret}"
                    4 -> "Refresh ${claimed.session!!.secret}"
                    else -> "Pairing ${pending.receiverSecret}"
                }, sent.getHeader("Authorization"))
            }
            // Same-domain redirect out of the prefix must never receive a proof.
            for (refresh in listOf(false, true)) {
                server.enqueue(MockResponse().setResponseCode(307).setHeader("Location", server.url("/api/security/refresh")).setBody("{}"))
                try { if (refresh) api.renew(claimed) else api.status(pending); fail() }
                catch (_: PairingException) { } catch (_: RenewalException) { }
                assertTrue(server.takeRequest().path!!.startsWith("/memory/api/"))
            }
            assertEquals(7, server.requestCount)
        } finally {
            client.dispatcher.cancelAll(); client.connectionPool.evictAll(); client.dispatcher.executorService.shutdown(); server.shutdown()
        }
    }

    @Test fun `root and memory bases produce canonical HTTPS and WSS endpoints`() {
        for (input in listOf("https://demo.kulai.eu/memory", "https://demo.kulai.eu/memory/")) {
            val base = ServerOrigin(input)
            assertEquals("https://demo.kulai.eu/memory", base.value)
            assertEquals("https://demo.kulai.eu/memory/api/memories", base.endpoint("api/memories").toString())
            for (path in listOf("ws/memory", "ws/assistant"))
                assertEquals("wss://demo.kulai.eu/memory/$path", base.webSocket(path))
        }
        val root = ServerOrigin("https://demo.kulai.eu/")
        assertEquals("https://demo.kulai.eu", root.value)
        assertEquals("wss://demo.kulai.eu/ws/memory", root.webSocket("ws/memory"))
    }

    @Test fun `original malformed paths cannot be repaired into trusted bases`() {
        for (path in listOf("//memory/", "/memory//", "/memory/.", "/memory/../", "/x/../memory/",
            "/%6demory/", "/memory%2f", "/memory%252f", "/memory\\", "/mentor/", "/memory/api/",
            "/memory/?", "/memory/#", "/memory/\n")) {
            try { ServerOrigin("https://demo.kulai.eu$path"); fail("Accepted malformed base") }
            catch (safe: PairingException) { assertNull(safe.cause) }
        }
        val base = ServerOrigin("https://demo.kulai.eu/memory/")
        for (input in listOf("https://demo.kulai.eu:/memory/", "https://demo.kulai.eu:0/memory/", "https://@demo.kulai.eu/memory/")) {
            try { ServerOrigin(input); fail("Accepted malformed authority") } catch (_: PairingException) { }
        }
        for (path in listOf("/api/memories", "../api/memories", "%2e%2e/api", "api//memories", "https://evil.test", "ws/memory?token=x")) {
            try { base.endpoint(path); fail("Accepted unsafe relative path") } catch (_: PairingException) { }
            try { base.webSocket(path); fail("Accepted unsafe WS path") } catch (_: PairingException) { }
        }
    }

    @Test fun `tokens are scoped to the configured prefix and saved service base`() {
        val base = ServerOrigin("https://demo.kulai.eu/memory/")
        val credential = DeviceCredential(UUID.randomUUID(), PairingController.randomSecret(), base.value)
        val auth = RequestAuthentication(base.value, credentials = CredentialProvider { credential })
        assertNotNull(auth.authorize(Request.Builder().url(base.endpoint("api/memories")).build()).header("Authorization"))
        for (path in listOf("/", "/api/memories", "/mentor/", "/propertly/", "/memory", "/memory-other/api",
            "/memory/../api/memories", "/memory/%2e%2e/mentor/", "/memory//api/memories", "/memory/api/%6demories",
            "/memory/api/memories#fragment", "/memory/api/memories?access_token=x")) {
            try { auth.authorize(Request.Builder().url("https://demo.kulai.eu$path").build()); fail("Escaped service scope") }
            catch (_: AuthenticationException) { }
        }
        for (other in listOf("https://demo.kulai.eu", "https://demo.kulai.eu:444/memory")) {
            val foreign = RequestAuthentication(other, credentials = CredentialProvider { credential })
            try { foreign.authorize(Request.Builder().url("$other/api/memories").build()); fail() }
            catch (_: AuthenticationException) { }
        }
    }

    @Test fun `websockets outside prefix and unpaired requests stop before network`() {
        val server = MockWebServer(); server.start(); val client = OkHttpClient()
        val base = ServerOrigin(server.url("/memory/").toString(), true)
        try {
            for (paired in listOf(false, true)) {
                val auth = RequestAuthentication(base.value, credentials = CredentialProvider {
                    if (paired) DeviceCredential(UUID.randomUUID(), PairingController.randomSecret(), base.value) else null
                })
                for (path in if (paired) listOf("/ws/memory", "/mentor/ws/assistant") else listOf("/memory/ws/memory", "/memory/ws/assistant")) {
                    val events = mutableListOf<SocketEvent>()
                    OkHttpVoiceSocketFactory(client, auth).connect(server.url(path).toString().replaceFirst("http", "ws"), events::add).cancel()
                    assertEquals(listOf(SocketEvent.Failed), events)
                }
            }
            assertEquals(0, server.requestCount)
        } finally {
            client.dispatcher.cancelAll(); client.connectionPool.evictAll(); client.dispatcher.executorService.shutdown(); server.shutdown()
        }
    }
}

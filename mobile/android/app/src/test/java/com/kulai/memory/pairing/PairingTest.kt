package com.kulai.memory.pairing

import com.kulai.memory.transport.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.*
import okhttp3.*
import okhttp3.mockwebserver.*
import org.junit.Assert.*
import org.junit.Test
import java.time.Instant
import java.util.UUID
import javax.crypto.KeyGenerator

@OptIn(ExperimentalCoroutinesApi::class)
class PairingTest {
    private class Store : ConnectionStore {
        var saved: ConnectionRecord? = null
        var failure = false
        override fun read() = if (failure) throw IllegalStateException("PRIVATE_STORE") else saved
        override fun write(record: ConnectionRecord) { if (failure) throw IllegalStateException("PRIVATE_STORE"); saved = record }
        override fun clear() { saved = null }
    }
    private class Api : PairingApi {
        val device = UUID.randomUUID()
        var phase = "pending"
        var claims = 0
        var registrations = 0
        var cancelled = false
        var fail = false
        var claimBlocked = false
        var afterClaim: () -> Unit = {}
        private fun reply(): PairingReply {
            if (fail) throw IllegalStateException("PRIVATE_NETWORK")
            return PairingReply(phase, device, Instant.now().plusSeconds(300), null)
        }
        override suspend fun register(pending: PendingPairing): PairingReply { registrations++; return reply() }
        override suspend fun status(pending: PendingPairing) = reply()
        override suspend fun cancel(pending: PendingPairing): PairingReply { cancelled = true; phase = "cancelled"; return reply() }
        override suspend fun claim(pending: PendingPairing): StoredCredential {
            claims++
            if (claimBlocked) awaitCancellation()
            phase = "claimed"
            afterClaim()
            return StoredCredential(device, UUID.randomUUID(), PairingController.randomSecret(), Instant.now().plusSeconds(3600))
        }
    }

    @Test fun `pending approval durable save restart verification and unpair`() = runTest {
        val store = Store(); val api = Api(); val io = StandardTestDispatcher(testScheduler)
        val first = PairingController(this, store, "http://127.0.0.1:8000", false, true, { api }, io = io)
        first.startup(); runCurrent(); assertFalse(first.state.value.privateAccess)
        first.begin(UUID.randomUUID().toString(), PairingController.randomSecret(), "Phone"); runCurrent()
        assertEquals(ConnectionStatus.PAIRING, first.state.value.status); assertNull(first.current())
        assertNotNull(store.saved!!.pending); assertEquals("", store.saved!!.pending!!.invitationSecret)
        api.phase = "approved"; advanceTimeBy(5000); runCurrent()
        assertEquals(ConnectionStatus.PAIRED, first.state.value.status); assertNotNull(first.current()); assertEquals(1, api.claims)
        assertNotNull(store.saved!!.credential)
        first.shutdown()
        val second = PairingController(this, store, "http://127.0.0.1:8000", true, true, { api }, io = io)
        assertFalse(second.state.value.privateAccess)
        second.startup(); runCurrent()
        assertEquals(ConnectionStatus.PAIRED, second.state.value.status); assertNotNull(second.current()); assertEquals(1, api.claims)
        second.forget(); runCurrent()
        assertNull(second.current()); assertNull(store.saved!!.credential); assertNull(store.saved!!.pending); assertTrue(api.cancelled)
        second.shutdown()
    }

    @Test fun `uncertain claim on restart requires new pairing and never repeats issuance`() = runTest {
        val store = Store(); val api = Api().apply { phase = "approved"; claimBlocked = true }
        val io = StandardTestDispatcher(testScheduler)
        val controller = PairingController(this, store, "http://127.0.0.1:8000", false, true, { api }, io = io)
        controller.startup(); runCurrent()
        controller.begin(UUID.randomUUID().toString(), PairingController.randomSecret(), "Phone"); runCurrent()
        assertTrue(store.saved!!.pending!!.claiming); assertNull(controller.current())
        controller.shutdown(); runCurrent()
        val restarted = PairingController(this, store, "http://127.0.0.1:8000", false, true, { api }, io = io)
        restarted.startup(); runCurrent()
        assertEquals(ConnectionStatus.NOT_PAIRED, restarted.state.value.status)
        assertEquals(1, api.claims); assertNull(restarted.current()); assertFalse(restarted.state.value.privateAccess)
        restarted.shutdown()
    }

    @Test fun `revoked expired and unavailable credentials never unlock private flows`() = runTest {
        for (phase in listOf("revoked", "expired", "unavailable")) {
            val store = Store(); val api = Api().apply { this.phase = phase; fail = phase == "unavailable" }
            val pending = PendingPairing(UUID.randomUUID(), PairingController.randomSecret(), "", UUID.randomUUID(), "Phone", registered = true)
            store.saved = ConnectionRecord("http://127.0.0.1:8000", false, pending,
                StoredCredential(api.device, UUID.randomUUID(), PairingController.randomSecret(), Instant.now().plusSeconds(3600)))
            val controller = PairingController(this, store, store.saved!!.origin, false, true, { api }, io = StandardTestDispatcher(testScheduler))
            controller.startup(); runCurrent()
            assertNull(controller.current()); assertFalse(controller.state.value.privateAccess)
            assertEquals(when(phase) { "expired" -> ConnectionStatus.EXPIRED; "revoked" -> ConnectionStatus.REVOKED; else -> ConnectionStatus.UNAVAILABLE }, controller.state.value.status)
            controller.shutdown()
        }
    }

    @Test fun `corrupt storage cannot silently restore LOCAL_DEV or leak provider errors`() = runTest {
        val store = Store().apply { failure = true }
        val controller = PairingController(this, store, "http://127.0.0.1:8000", true, true, { error("No network") }, io = StandardTestDispatcher(testScheduler))
        controller.startup(); runCurrent()
        assertFalse(controller.state.value.localDev); assertFalse(controller.state.value.privateAccess); assertNull(controller.current())
        assertFalse(controller.state.value.toString().contains("PRIVATE_STORE"))
        controller.shutdown()
    }

    @Test fun `local developer path keeps loopback and rejects LAN origins`() = runTest {
        val controller = PairingController(this, Store(), "http://127.0.0.1:8000", true, true, { error("No pairing API in LOCAL_DEV") }, io = StandardTestDispatcher(testScheduler))
        controller.startup(); runCurrent(); assertTrue(controller.state.value.privateAccess)
        controller.configure("http://192.168.1.10:8000", true); runCurrent()
        assertFalse(controller.state.value.privateAccess); assertNull(controller.current()); controller.shutdown()
    }

    @Test fun `origin policy blocks plaintext remote credentials redirects query and userinfo`() {
        for (url in listOf("http://example.com", "http://192.168.1.1", "https://user:secret@example.com", "https://example.com/private", "https://example.com?token=x", "https://example.com#x")) {
            try { ServerOrigin(url, true); fail("Expected origin refusal") } catch (safe: PairingException) { assertNull(safe.cause) }
        }
        assertEquals("https://example.com", ServerOrigin("https://example.com").value)
        assertEquals("wss://example.com/ws/memory", ServerOrigin("https://example.com").webSocket("ws/memory"))
        try { ServerOrigin("http://127.0.0.1:8000"); fail() } catch (_: PairingException) { }
    }

    @Test fun `encrypted record hides secrets detects tamper key loss and atomic failed replacement`() {
        val key = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
        class File : VaultFile {
            var bytes: ByteArray? = null; var fail = false
            override fun read() = bytes
            override fun write(bytes: ByteArray) { if (fail) throw IllegalStateException("PRIVATE_WRITE"); this.bytes = bytes.copyOf() }
            override fun delete() { bytes = null }
        }
        val file = File(); val cipher = AesGcmCipher { key }; val vault = EncryptedConnectionStore(file, cipher)
        val secret = PairingController.randomSecret()
        val record = ConnectionRecord("https://example.com", false, PendingPairing(UUID.randomUUID(), secret, secret, UUID.randomUUID(), "Phone"),
            StoredCredential(UUID.randomUUID(), UUID.randomUUID(), secret, Instant.now().plusSeconds(3600)))
        vault.write(record)
        assertFalse(String(file.bytes!!, Charsets.UTF_8).contains(secret)); assertFalse(record.toString().contains(secret))
        assertEquals(secret, vault.read()!!.credential!!.token)
        val previous = file.bytes!!.copyOf(); file.fail = true
        try { vault.write(ConnectionRecord("https://other.example", false)); fail() } catch (safe: PairingException) { assertNull(safe.cause) }
        assertArrayEquals(previous, file.bytes); file.fail = false
        file.bytes = previous.copyOf().also { it[it.lastIndex] = (it.last().toInt() xor 1).toByte() }
        try { vault.read(); fail() } catch (_: PairingException) { }
        file.bytes = previous
        val wrong = EncryptedConnectionStore(file, AesGcmCipher { KeyGenerator.getInstance("AES").apply { init(256) }.generateKey() })
        try { wrong.read(); fail() } catch (_: PairingException) { }
        vault.clear(); assertNull(vault.read())
    }

    @Test fun `credentials bound to origin and expiry cannot reach foreign requests`() {
        val provider = CredentialProvider { DeviceCredential(UUID.randomUUID(), PairingController.randomSecret(), "https://trusted.example", Instant.now().plusSeconds(60)) }
        val auth = RequestAuthentication("https://different.example", credentials = provider)
        try { auth.authorize(Request.Builder().url("https://different.example/api/memories").build()); fail() } catch (_: AuthenticationException) { }
        val expired = RequestAuthentication("https://trusted.example", credentials = CredentialProvider {
            DeviceCredential(UUID.randomUUID(), PairingController.randomSecret(), "https://trusted.example", Instant.now().minusSeconds(1)) })
        try { expired.authorize(Request.Builder().url("https://trusted.example/api/memories").build()); fail() } catch (_: AuthenticationException) { }
    }

    @Test fun `pairing client sends proofs only to exact origin and refuses redirects`() = runBlocking {
        val first = MockWebServer(); val other = MockWebServer(); first.start(); other.start()
        val client = OkHttpClient(); val pending = PendingPairing(UUID.randomUUID(), PairingController.randomSecret(), PairingController.randomSecret(), UUID.randomUUID(), "Phone")
        try {
            val api = PairingHttpClient(ServerOrigin(first.url("/").toString(), true), client)
            first.enqueue(MockResponse().setResponseCode(307).setHeader("Location", other.url("/api/pairing/request")))
            try { api.register(pending); fail() } catch (safe: PairingException) { assertNull(safe.cause); assertFalse(safe.toString().contains(pending.receiverSecret)) }
            val request = first.takeRequest()
            assertEquals("/api/pairing/request", request.path); assertEquals("Pairing " + pending.invitationSecret, request.getHeader("Authorization"))
            assertTrue(request.body.readUtf8().contains(pending.receiverSecret)); assertEquals(0, other.requestCount)
            first.enqueue(MockResponse().setResponseCode(400).setBody(pending.receiverSecret))
            try { api.status(pending); fail() } catch (safe: PairingException) { assertFalse(safe.toString().contains(pending.receiverSecret)) }
        } finally { client.dispatcher.cancelAll(); client.connectionPool.evictAll(); client.dispatcher.executorService.shutdown(); first.shutdown(); other.shutdown() }
    }

    @Test fun `restart while pending resumes status without another registration`() = runTest {
        val store = Store(); val api = Api(); val io = StandardTestDispatcher(testScheduler)
        val first = PairingController(this, store, "http://127.0.0.1:8000", false, true, { api }, io = io)
        first.startup(); runCurrent()
        first.begin(UUID.randomUUID().toString(), PairingController.randomSecret(), "Phone"); runCurrent()
        first.shutdown(); runCurrent()
        val second = PairingController(this, store, "http://127.0.0.1:8000", false, true, { api }, io = io)
        second.startup(); runCurrent()
        assertEquals(1, api.registrations); assertEquals(ConnectionStatus.PAIRING, second.state.value.status)
        assertNull(second.current()); second.shutdown()
    }

    @Test fun `failed credential save never publishes received token`() = runTest {
        val store = Store(); val api = Api().apply { phase = "approved"; afterClaim = { store.failure = true } }
        val controller = PairingController(this, store, "http://127.0.0.1:8000", false, true, { api }, io = StandardTestDispatcher(testScheduler))
        controller.startup(); runCurrent()
        controller.begin(UUID.randomUUID().toString(), PairingController.randomSecret(), "Phone"); runCurrent()
        assertEquals(1, api.claims); assertTrue(store.saved!!.pending!!.claiming)
        assertNull(store.saved!!.credential); assertNull(controller.current()); assertFalse(controller.state.value.privateAccess)
        controller.shutdown()
    }

    @Test fun `expired pending session cannot issue credentials`() = runTest {
        val api = Api().apply { phase = "expired" }
        val controller = PairingController(this, Store(), "http://127.0.0.1:8000", false, true, { api }, io = StandardTestDispatcher(testScheduler))
        controller.startup(); runCurrent()
        controller.begin(UUID.randomUUID().toString(), PairingController.randomSecret(), "Phone"); runCurrent()
        assertEquals(ConnectionStatus.EXPIRED, controller.state.value.status); assertEquals(0, api.claims); assertNull(controller.current())
        controller.shutdown()
    }

    @Test fun `authentication denial verifies revocation and ignores other device callbacks`() = runTest {
        val store = Store(); val api = Api().apply { phase = "approved" }
        val controller = PairingController(this, store, "http://127.0.0.1:8000", false, true, { api }, io = StandardTestDispatcher(testScheduler))
        controller.startup(); runCurrent(); controller.begin(UUID.randomUUID().toString(), PairingController.randomSecret(), "Phone"); runCurrent()
        controller.authenticationRejected(UUID.randomUUID()); runCurrent(); assertNotNull(controller.current())
        api.phase = "revoked"; controller.authenticationRejected(api.device); runCurrent()
        assertEquals(ConnectionStatus.REVOKED, controller.state.value.status); assertNull(controller.current()); controller.shutdown()
    }

    @Test fun `changing server atomically removes credentials and forbids old origin`() = runTest {
        val store = Store(); val api = Api().apply { phase = "approved" }
        val controller = PairingController(this, store, "http://127.0.0.1:8000", false, true, { api }, io = StandardTestDispatcher(testScheduler))
        controller.startup(); runCurrent(); controller.begin(UUID.randomUUID().toString(), PairingController.randomSecret(), "Phone"); runCurrent()
        assertNotNull(controller.current())
        controller.configure("https://new.example", false); runCurrent()
        assertEquals("https://new.example", store.saved!!.origin); assertNull(controller.current()); assertNull(store.saved!!.credential)
        assertFalse(controller.state.value.privateAccess); controller.shutdown()
    }
}

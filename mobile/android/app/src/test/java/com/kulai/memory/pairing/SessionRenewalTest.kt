package com.kulai.memory.pairing

import com.kulai.memory.transport.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.*
import okhttp3.*
import okhttp3.mockwebserver.*
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.time.Instant
import java.util.UUID
import javax.crypto.KeyGenerator

@OptIn(ExperimentalCoroutinesApi::class)
class SessionRenewalTest {
    private val origin = "http://127.0.0.1:8000"
    private val now = Instant.now()
    private fun credential(expires: Instant = now.plusSeconds(10), device: UUID = UUID.randomUUID(),
        family: UUID = UUID.randomUUID(), absolute: Instant = now.plusSeconds(90*86400L)) = StoredCredential(
            device, UUID.randomUUID(), PairingController.randomSecret(), expires,
            SessionCredential(family, UUID.randomUUID(), "krf1_"+PairingController.randomSecret(), now.plusSeconds(30*86400L), absolute))
    private fun record(value: StoredCredential, renewing: Boolean = false) = ConnectionRecord(origin, false,
        PendingPairing(UUID.randomUUID(), PairingController.randomSecret(), "", UUID.randomUUID(), "Phone", registered=true), value, renewing)
    private class Store(var saved: ConnectionRecord?) : ConnectionStore {
        var fail = false
        override fun read() = saved
        override fun write(record: ConnectionRecord) { if (fail) error("PRIVATE_WRITE"); saved=record }
        override fun clear() { saved=null }
    }
    private class Api(private val initial: StoredCredential) : PairingApi {
        var calls=0
        var state="claimed"
        var failure: RenewalFailure? = null
        var gate: CompletableDeferred<Unit>? = null
        var afterRenew: () -> Unit = {}
        private fun reply() = PairingReply(state, initial.deviceId, Instant.now().plusSeconds(300), null)
        override suspend fun register(pending: PendingPairing) = reply()
        override suspend fun status(pending: PendingPairing) = reply()
        override suspend fun cancel(pending: PendingPairing) = reply()
        override suspend fun claim(pending: PendingPairing) = initial
        override suspend fun renew(credential: StoredCredential): StoredCredential {
            calls++; gate?.await()
            failure?.let { throw RenewalException(it) }
            afterRenew()
            return StoredCredential(initial.deviceId, UUID.randomUUID(), PairingController.randomSecret(), Instant.now().plusSeconds(3600),
                SessionCredential(initial.session!!.sessionId, UUID.randomUUID(), "krf1_"+PairingController.randomSecret(),
                    initial.session.refreshExpiresAt, initial.session.absoluteExpiresAt))
        }
    }

    @Test fun `concurrent callers coalesce rotation and publish only after durable save`() = runTest {
        val initial=credential(); val store=Store(record(initial)); val api=Api(initial).apply { gate=CompletableDeferred() }
        val controller=PairingController(this,store,origin,false,true,{api},io=StandardTestDispatcher(testScheduler),clock={now})
        controller.startup(); runCurrent()
        assertTrue(store.saved!!.renewing); assertNull(controller.current())
        val callers=(1..20).map { async { controller.ensureFresh() } }; runCurrent()
        assertEquals(1,api.calls)
        api.gate!!.complete(Unit); runCurrent()
        assertTrue(callers.awaitAll().all { it }); assertFalse(store.saved!!.renewing)
        assertNotEquals(initial.tokenId,store.saved!!.credential!!.tokenId)
        val request=RequestAuthentication(origin,credentials=controller).authorize(Request.Builder().url("$origin/api/memories").build())
        assertEquals("Bearer "+store.saved!!.credential!!.token,request.header("Authorization"))
        controller.shutdown()
    }

    @Test fun `restart with unfinished intent never retransmits old refresh`() = runTest {
        val initial=credential(); val store=Store(record(initial,renewing=true)); val api=Api(initial)
        val controller=PairingController(this,store,origin,true,true,{api},io=StandardTestDispatcher(testScheduler),clock={now})
        controller.startup(); runCurrent(); controller.refresh(); runCurrent()
        assertEquals(0,api.calls); assertNull(controller.current()); assertFalse(controller.state.value.localDev)
        assertEquals(ConnectionStatus.NOT_PAIRED,controller.state.value.status); controller.shutdown()
    }

    @Test fun `active voice defers proactive rotation but never extends access expiry`() = runTest {
        for (finish in listOf(true,false)) {
            val initial=credential(); val store=Store(record(initial)); val api=Api(initial)
            var time=now; var busy=true
            val controller=PairingController(this,store,origin,false,true,{api},io=StandardTestDispatcher(testScheduler),
                clock={time},renewalBusy={busy})
            controller.startup(); runCurrent()
            assertEquals(0,api.calls); assertNotNull(controller.current()); assertFalse(store.saved!!.renewing)
            if (finish) busy=false else time=initial.expiresAt
            controller.refresh(); runCurrent()
            assertEquals(1,api.calls); assertNotNull(controller.current()); controller.shutdown()
        }
    }

    @Test fun `safe pre-send offline failure retries with backoff and recovers`() = runTest {
        val initial=credential(); val store=Store(record(initial)); val api=Api(initial).apply { failure=RenewalFailure.NOT_SENT }
        val controller=PairingController(this,store,origin,false,true,{api},io=StandardTestDispatcher(testScheduler),clock={now})
        controller.startup(); runCurrent()
        assertEquals(1,api.calls); assertFalse(store.saved!!.renewing); assertNull(controller.current())
        advanceTimeBy(4999); runCurrent(); assertEquals(1,api.calls)
        api.failure=null; advanceTimeBy(1); runCurrent()
        assertEquals(2,api.calls); assertEquals(ConnectionStatus.PAIRED,controller.state.value.status)
        controller.shutdown()
        val restarted=PairingController(this,store,origin,false,true,{api},io=StandardTestDispatcher(testScheduler),clock={now})
        restarted.startup(); runCurrent(); assertNotNull(restarted.current()); assertEquals(2,api.calls); restarted.shutdown()
    }

    @Test fun `ambiguous delivery failed save and timeout keep intent and require pairing`() = runTest {
        for (fault in listOf("network","save","timeout")) {
            val initial=credential(); val store=Store(record(initial)); val api=Api(initial)
            when(fault) {
                "network" -> api.failure=RenewalFailure.UNCERTAIN
                "save" -> api.afterRenew={store.fail=true}
                "timeout" -> api.gate=CompletableDeferred()
            }
            val controller=PairingController(this,store,origin,false,true,{api},io=StandardTestDispatcher(testScheduler),clock={now})
            controller.startup(); runCurrent()
            if (fault=="timeout") { advanceTimeBy(12001); runCurrent() }
            assertTrue(store.saved!!.renewing); assertNull(controller.current()); assertFalse(controller.state.value.privateAccess)
            controller.refresh(); runCurrent(); assertEquals(1,api.calls)
            assertFalse(controller.state.value.toString().contains(initial.session!!.secret)); controller.shutdown()
        }
    }

    @Test fun `expired absolute or revoked family cannot recover data access`() = runTest {
        for (fault in listOf("absolute","refresh","revoked")) {
            val initial=credential(); val family=initial.session!!
            val saved=if(fault=="revoked") initial else StoredCredential(initial.deviceId,initial.tokenId,initial.token,initial.expiresAt,
                SessionCredential(family.sessionId,family.refreshId,family.secret,
                    if(fault=="refresh") now.minusSeconds(1) else family.refreshExpiresAt,
                    if(fault=="absolute") now.minusSeconds(1) else family.absoluteExpiresAt))
            val store=Store(record(saved)); val api=Api(initial).apply { if(fault=="revoked") state="revoked" }
            val controller=PairingController(this,store,origin,false,true,{api},io=StandardTestDispatcher(testScheduler),clock={now})
            controller.startup(); runCurrent(); assertNull(controller.current()); assertEquals(0,api.calls)
            assertFalse(controller.state.value.privateAccess); controller.shutdown()
        }
    }

    @Test fun `encrypted session round trip hides both secrets and retains intent atomically`() {
        val key=KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
        class File : VaultFile {
            var bytes: ByteArray?=null; var fail=false
            override fun read()=bytes
            override fun write(bytes: ByteArray) { if(fail) error("PRIVATE_WRITE"); this.bytes=bytes.copyOf() }
            override fun delete() { bytes=null }
        }
        val file=File(); val cipher=AesGcmCipher { key }; val vault=EncryptedConnectionStore(file,cipher)
        val original=record(credential(),renewing=true); vault.write(original)
        val bytes=file.bytes!!.copyOf()
        assertFalse(String(bytes).contains(original.credential!!.token)); assertFalse(String(bytes).contains(original.credential.session!!.secret))
        val restored=EncryptedConnectionStore(file,cipher).read()!!
        assertTrue(restored.renewing); assertEquals(original.credential.session.secret,restored.credential!!.session!!.secret)
        file.fail=true
        try { vault.write(ConnectionRecord("https://other.invalid",false)); fail() } catch (_:PairingException) { }
        assertArrayEquals(bytes,file.bytes)
        assertFalse(restored.toString().contains(original.credential.session.secret))
    }

    @Test fun `late rejection of previous access does not reject the rotated snapshot`() {
        val old=credential(); val next=credential(device=old.deviceId)
        var active=DeviceCredential(old.deviceId,old.token,origin,old.expiresAt)
        var denied=0
        val auth=RequestAuthentication(origin,credentials=CredentialProvider {active},onRejected={denied++})
        val sent=auth.authorize(Request.Builder().url("$origin/api/memories").build())
        active=DeviceCredential(next.deviceId,next.token,origin,next.expiresAt)
        auth.rejected(sent); assertEquals(0,denied)
    }

    @Test fun `refresh client rejects uncertain response and never follows redirects or retries`() = runBlocking {
        val server=MockWebServer(); val foreign=MockWebServer(); server.start(); foreign.start()
        val client=OkHttpClient(); val initial=credential()
        try {
            val api=PairingHttpClient(ServerOrigin(server.url("/memory/").toString(),true),client)
            server.enqueue(MockResponse().setResponseCode(307).setHeader("Location",foreign.url("/api/security/refresh")).setBody("{}"))
            try { api.renew(initial); fail() } catch (error:RenewalException) { assertEquals(RenewalFailure.UNCERTAIN,error.outcome) }
            val request=server.takeRequest(); assertEquals("/memory/api/security/refresh",request.path)
            assertEquals("Refresh "+initial.session!!.secret,request.getHeader("Authorization"))
            assertFalse(request.body.readUtf8().contains(initial.session.secret)); assertEquals(0,foreign.requestCount)
            server.enqueue(MockResponse().setResponseCode(503).setBody("{\"code\":\"unavailable\",\"request_applied\":false}"))
            try { api.renew(initial); fail() } catch (error:RenewalException) { assertEquals(RenewalFailure.RETRY_LATER,error.outcome) }
            server.enqueue(MockResponse().setResponseCode(503).setBody(initial.session.secret))
            try { api.renew(initial); fail() } catch (error:RenewalException) {
                assertEquals(RenewalFailure.UNCERTAIN,error.outcome); assertNull(error.cause); assertFalse(error.toString().contains(initial.session.secret))
            }
            assertEquals(3,server.requestCount)
        } finally { client.dispatcher.cancelAll(); client.connectionPool.evictAll(); client.dispatcher.executorService.shutdown(); server.shutdown(); foreign.shutdown() }
    }
}

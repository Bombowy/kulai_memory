package com.kulai.memory.pairing

import com.kulai.memory.transport.CredentialProvider
import com.kulai.memory.transport.DeviceCredential
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.security.MessageDigest
import java.security.SecureRandom
import java.time.Instant
import java.util.Base64
import java.util.UUID
import java.util.concurrent.atomic.AtomicReference

enum class ConnectionStatus { NOT_PAIRED, PAIRING, PAIRED, EXPIRED, REVOKED, UNAVAILABLE }
data class ConnectionState(val origin: String, val localDev: Boolean, val status: ConnectionStatus = ConnectionStatus.NOT_PAIRED,
    val sessionId: UUID? = null, val deviceId: UUID? = null, val verificationCode: String? = null, val error: String? = null) {
    val privateAccess: Boolean get() = status != ConnectionStatus.UNAVAILABLE && (localDev || status == ConnectionStatus.PAIRED)
}

/** Owned by ViewModel scope. IO/crypto is off main; network calls cancel with their operation. */
class PairingController(private val scope: CoroutineScope, private val store: ConnectionStore,
    initialOrigin: String, initialLocalDev: Boolean, private val allowLocalCleartext: Boolean,
    private val apiFactory: (ServerOrigin) -> PairingApi,
    private val onConnectionChanged: (String, Boolean) -> Unit = { _, _ -> },
    private val io: CoroutineDispatcher = Dispatchers.IO,
    private val clock: () -> Instant = Instant::now,
    private val renewalBusy: () -> Boolean = { false }) : CredentialProvider {
    private val snapshot = AtomicReference<DeviceCredential?>(null)
    private val mutableState = MutableStateFlow(ConnectionState(initialOrigin, initialLocalDev, ConnectionStatus.UNAVAILABLE))
    val state = mutableState.asStateFlow()
    private var record: ConnectionRecord? = null
    private var operation: Job? = null
    private var expiry: Job? = null
    private var renewalOwner: Job? = null
    private var retryCount = 0
    private var closed = false
    override fun current(): DeviceCredential? = snapshot.get()?.takeIf { it.expiresAt > clock() }

    fun startup() = launch {
        val saved = try { withContext(io) { store.read() } }
        catch (_: Exception) {
            mutableState.value = state.value.copy(localDev = false, error = "Local credentials are unavailable. Pair again.")
            try { withContext(io) { store.clear() } } catch (_: Exception) { }
            onConnectionChanged(state.value.origin, false)
            return@launch
        }
        record = saved ?: ConnectionRecord(state.value.origin, state.value.localDev)
        val configured = record!!
        validate(configured.origin, configured.localDev)
        mutableState.value = ConnectionState(configured.origin, configured.localDev)
        onConnectionChanged(configured.origin, configured.localDev)
        resume(configured)
    }

    fun configure(value: String, localDev: Boolean) = launch {
        val origin = validate(value, localDev)
        snapshot.set(null); expiry?.cancel()
        val changed = ConnectionRecord(origin.value, localDev)
        withContext(io) { store.write(changed) }
        record = changed
        mutableState.value = ConnectionState(origin.value, localDev)
        onConnectionChanged(origin.value, localDev)
    }

    fun begin(session: String, invitation: String, name: String) = launch {
        if (state.value.localDev) throw PairingException()
        val origin = validate(state.value.origin, false)
        require(Regex("[A-Za-z0-9_-]{43}").matches(invitation) && Regex("[A-Za-z0-9 ._-]{1,48}").matches(name) && name.isNotBlank())
        val pending = PendingPairing(UUID.fromString(session), randomSecret(), invitation, UUID.randomUUID(), name.trim())
        val changed = ConnectionRecord(origin.value, false, pending)
        snapshot.set(null); expiry?.cancel(); retryCount = 0
        withContext(io) { store.write(changed) }
        record = changed
        mutableState.value = ConnectionState(origin.value, false, ConnectionStatus.PAIRING, pending.sessionId,
            verificationCode = receiverCode(pending.receiverSecret))
        waitForApproval(changed, apiFactory(origin))
    }

    fun refresh() {
        if (renewalOwner?.isActive == true) return // PL/EN: coalesce; never cancel an in-flight rotation to retry it.
        launch { record?.let { resume(it) } }
    }

    /** Optional suspend boundary; current() stays nonblocking for HTTP and WS transports. */
    suspend fun ensureFresh(): Boolean = withContext(scope.coroutineContext.minusKey(Job)) {
        val saved = record
        if (saved?.credential?.session != null && nearExpiry(saved.credential)) refresh()
        withTimeoutOrNull(15000) { (renewalOwner ?: operation)?.join() }
        current() != null
    }

    fun authenticationRejected(deviceId: UUID) {
        scope.launch {
            if (!closed && snapshot.get()?.deviceId == deviceId) {
                snapshot.set(null)
                mutableState.value = state.value.copy(status = ConnectionStatus.UNAVAILABLE)
                refresh() // Check own receiver capability; never infer identity from an IP.
            }
        }
    }

    fun forget() = launch {
        snapshot.set(null); expiry?.cancel()
        val previous = record
        val changed = ConnectionRecord(state.value.origin, state.value.localDev)
        // Local removal always succeeds independently of server availability.
        withContext(io) { store.write(changed) }
        record = changed
        mutableState.value = ConnectionState(changed.origin, changed.localDev)
        onConnectionChanged(changed.origin, changed.localDev)
        previous?.pending?.let { pending ->
            try { apiFactory(validate(previous.origin, false)).cancel(pending) }
            catch (cancel: CancellationException) { throw cancel }
            catch (_: Exception) { mutableState.value = state.value.copy(error = "Removed locally. Administrator may need to revoke server access.") }
        }
    }

    private suspend fun resume(saved: ConnectionRecord) {
        if (saved.localDev) return
        val pending = saved.pending ?: return
        if (saved.renewing) { requirePairing(); return }
        if (pending.claiming && saved.credential == null) {
            mutableState.value = state.value.copy(status = ConnectionStatus.NOT_PAIRED,
                error = "Credential delivery was interrupted. Pair again and ask the administrator to revoke the old device.")
            return
        }
        val api = apiFactory(validate(saved.origin, false))
        if (saved.credential == null) {
            mutableState.value = state.value.copy(status = ConnectionStatus.PAIRING, sessionId = pending.sessionId,
                verificationCode = receiverCode(pending.receiverSecret))
            waitForApproval(saved, api)
        } else {
            val credential = saved.credential
            if (credential.session == null && credential.expiresAt <= clock()) { expire(); return }
            if (credential.session != null && (credential.session.refreshExpiresAt <= clock() ||
                    credential.session.absoluteExpiresAt <= clock())) { expire(); return }
            val reply = api.status(pending)
            if (reply.state != "claimed" || reply.deviceId != credential.deviceId) {
                mutableState.value = state.value.copy(status = if (reply.state == "expired") ConnectionStatus.EXPIRED else ConnectionStatus.REVOKED)
                snapshot.set(null)
            } else if (credential.session != null && nearExpiry(credential)) {
                if (credential.expiresAt > clock() && renewalBusy()) publish(saved, deferred = true)
                else renew(saved, api)
            } else publish(saved)
        }
    }

    private suspend fun waitForApproval(saved: ConnectionRecord, api: PairingApi) {
        val original = saved.pending!!
        var reply = if (original.registered) api.status(original) else api.register(original)
        val pending = PendingPairing(original.sessionId, original.receiverSecret, "", original.clientId, original.label, registered = true)
        val accepted = ConnectionRecord(saved.origin, false, pending)
        withContext(io) { store.write(accepted) }; record = accepted
        while (!closed) {
            mutableState.value = state.value.copy(deviceId = reply.deviceId)
            when (reply.state) {
                "approved" -> {
                    val claiming = PendingPairing(pending.sessionId, pending.receiverSecret, pending.invitationSecret,
                        pending.clientId, pending.label, claiming = true, registered = true)
                    val intent = ConnectionRecord(saved.origin, false, claiming)
                    withContext(io) { store.write(intent) }; record = intent
                    val token = api.claim(pending)
                    require(token.deviceId == reply.deviceId && token.expiresAt > clock())
                    val paired = ConnectionRecord(saved.origin, false, pending, token)
                    withContext(io) { store.write(paired) }; record = paired
                    publish(paired)
                    return
                }
                "pending" -> {
                    if (reply.expiresAt <= clock()) { expire(); return }
                    delay(5000); reply = api.status(pending)
                }
                "expired" -> { expire(); return }
                else -> {
                    mutableState.value = state.value.copy(status = ConnectionStatus.REVOKED, error = "Pairing was refused or cancelled. Pair again.")
                    return
                }
            }
        }
    }

    private fun publish(saved: ConnectionRecord, deferred: Boolean = false) {
        val credential = saved.credential!!
        snapshot.set(DeviceCredential(credential.deviceId, credential.token, saved.origin, credential.expiresAt))
        mutableState.value = state.value.copy(status = ConnectionStatus.PAIRED, deviceId = credential.deviceId, error = null)
        retryCount = 0
        expiry?.cancel()
        expiry = scope.launch {
            val remaining = java.time.Duration.between(clock(), credential.expiresAt).toMillis().coerceAtLeast(0)
            // PL/EN: avoid revoking a recording's handshake bearer; defer only until its real expiry.
            delay(if (deferred) minOf(5000L, remaining) else if (credential.session == null) remaining
                else (remaining - minOf(300000, remaining / 4)).coerceAtLeast(1))
            if (credential.session == null) expire() else refresh()
        }
    }
    private fun nearExpiry(credential: StoredCredential): Boolean =
        credential.expiresAt <= clock().plusSeconds(300)

    private suspend fun renew(saved: ConnectionRecord, api: PairingApi) {
        renewalOwner = currentCoroutineContext().job
        snapshot.set(null)
        mutableState.value = state.value.copy(status = ConnectionStatus.UNAVAILABLE, error = "Renewing secure session.")
        expiry?.cancel()
        val intent = ConnectionRecord(saved.origin, false, saved.pending, saved.credential, renewing = true)
        try {
            withContext(io) { store.write(intent) }; record = intent
            val next = withTimeout(12000) { api.renew(saved.credential!!) }
            val old = saved.credential!!
            val family = next.session ?: throw RenewalException(RenewalFailure.UNCERTAIN)
            require(next.deviceId == old.deviceId && next.tokenId != old.tokenId && family.refreshId != old.session!!.refreshId &&
                family.sessionId == old.session.sessionId && family.absoluteExpiresAt == old.session.absoluteExpiresAt &&
                next.expiresAt > clock())
            val changed = ConnectionRecord(saved.origin, false, saved.pending, next)
            withContext(io) { store.write(changed) }; record = changed
            publish(changed) // PL/EN: both credentials become visible only after durable replacement.
        } catch (cancel: CancellationException) {
            // Intent survives cancellation/timeout/process death. Never retransmit its old secret.
            requirePairing()
            throw cancel
        } catch (error: Exception) {
            if (error is RenewalException && error.outcome in setOf(RenewalFailure.NOT_SENT, RenewalFailure.RETRY_LATER)) {
                withContext(io) { store.write(saved) }; record = saved
                mutableState.value = state.value.copy(status = ConnectionStatus.UNAVAILABLE, error = "Server unavailable. Session recovery will retry safely.")
                scheduleRetry()
            } else {
                requirePairing()
                if (error is RenewalException && error.outcome == RenewalFailure.DENIED) {
                    try {
                        val view = api.status(saved.pending!!)
                        mutableState.value = state.value.copy(status = if (view.state == "revoked") ConnectionStatus.REVOKED else ConnectionStatus.EXPIRED)
                    } catch (_: Exception) { }
                }
            }
        } finally { renewalOwner = null }
    }

    private fun requirePairing() {
        snapshot.set(null)
        mutableState.value = state.value.copy(status = ConnectionStatus.NOT_PAIRED,
            error = "Session renewal could not be confirmed. Pair again; ask the administrator to revoke the old device.")
    }

    private fun scheduleRetry() {
        val saved = record ?: return
        val family = saved.credential?.session ?: return
        if (closed || saved.renewing || family.refreshExpiresAt <= clock() || family.absoluteExpiresAt <= clock() || retryCount >= 5) return
        val wait = minOf(60000L, 5000L shl retryCount++)
        expiry?.cancel()
        expiry = scope.launch { delay(wait); refresh() }
    }
    private fun expire() { snapshot.set(null); mutableState.value = state.value.copy(status = ConnectionStatus.EXPIRED) }
    private fun validate(value: String, localDev: Boolean): ServerOrigin = ServerOrigin(value, allowLocalCleartext).also {
        if (localDev && (!allowLocalCleartext || !ServerOrigin.loopback(it.url.host))) throw PairingException()
    }
    private fun launch(block: suspend () -> Unit) {
        if (closed) return
        val previous = operation
        previous?.cancel()
        operation = scope.launch {
            previous?.join() // Drain IO and cancellation before changing the durable record.
            try { block() }
            catch (cancel: CancellationException) { throw cancel }
            catch (_: Exception) {
                snapshot.set(null)
                mutableState.value = state.value.copy(status = ConnectionStatus.UNAVAILABLE, error = "Connection or pairing is unavailable. Retry or pair again.")
                scheduleRetry()
            }
        }
    }
    fun shutdown() { closed = true; operation?.cancel(); expiry?.cancel(); snapshot.set(null) }
    companion object {
        fun randomSecret(): String = Base64.getUrlEncoder().withoutPadding().encodeToString(ByteArray(32).also { SecureRandom().nextBytes(it) })
        fun receiverCode(secret: String): String = MessageDigest.getInstance("SHA-256").digest(secret.toByteArray(Charsets.US_ASCII))
            .joinToString("") { "%02x".format(it) }
    }
}

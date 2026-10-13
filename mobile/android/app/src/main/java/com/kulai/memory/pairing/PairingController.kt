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
    private val io: CoroutineDispatcher = Dispatchers.IO) : CredentialProvider {
    private val snapshot = AtomicReference<DeviceCredential?>(null)
    private val mutableState = MutableStateFlow(ConnectionState(initialOrigin, initialLocalDev, ConnectionStatus.UNAVAILABLE))
    val state = mutableState.asStateFlow()
    private var record: ConnectionRecord? = null
    private var operation: Job? = null
    private var expiry: Job? = null
    private var closed = false
    override fun current(): DeviceCredential? = snapshot.get()?.takeIf { it.expiresAt > Instant.now() }

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
        snapshot.set(null)
        withContext(io) { store.write(changed) }
        record = changed
        mutableState.value = ConnectionState(origin.value, false, ConnectionStatus.PAIRING, pending.sessionId,
            verificationCode = receiverCode(pending.receiverSecret))
        waitForApproval(changed, apiFactory(origin))
    }

    fun refresh() = launch { record?.let { resume(it) } }

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
            if (saved.credential.expiresAt <= Instant.now()) { expire(); return }
            val reply = api.status(pending)
            if (reply.state != "claimed" || reply.deviceId != saved.credential.deviceId) {
                mutableState.value = state.value.copy(status = if (reply.state == "expired") ConnectionStatus.EXPIRED else ConnectionStatus.REVOKED)
                snapshot.set(null)
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
                    require(token.deviceId == reply.deviceId && token.expiresAt > Instant.now())
                    val paired = ConnectionRecord(saved.origin, false, pending, token)
                    withContext(io) { store.write(paired) }; record = paired
                    publish(paired)
                    return
                }
                "pending" -> {
                    if (reply.expiresAt <= Instant.now()) { expire(); return }
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

    private fun publish(saved: ConnectionRecord) {
        val credential = saved.credential!!
        snapshot.set(DeviceCredential(credential.deviceId, credential.token, saved.origin, credential.expiresAt))
        mutableState.value = state.value.copy(status = ConnectionStatus.PAIRED, deviceId = credential.deviceId, error = null)
        expiry?.cancel()
        expiry = scope.launch { delay(java.time.Duration.between(Instant.now(), credential.expiresAt).toMillis().coerceAtLeast(0)); expire() }
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

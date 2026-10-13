package com.kulai.memory.pairing

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.suspendCancellableCoroutine
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.time.Instant
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

class PairingReply(val state: String, val deviceId: UUID?, val expiresAt: Instant, val verificationCode: String?)
interface PairingApi {
    suspend fun register(pending: PendingPairing): PairingReply
    suspend fun status(pending: PendingPairing): PairingReply
    suspend fun claim(pending: PendingPairing): StoredCredential
    suspend fun cancel(pending: PendingPairing): PairingReply
    suspend fun renew(credential: StoredCredential): StoredCredential { throw RenewalException(RenewalFailure.UNCERTAIN) }
}

enum class RenewalFailure { NOT_SENT, RETRY_LATER, DENIED, UNCERTAIN }
class RenewalException(val outcome: RenewalFailure) : Exception("Session renewal unavailable.")
private class RenewalAttempt { val submitted = AtomicBoolean(false) }

/** Pre-auth proof is restricted to these four exact native POST paths and one validated origin. */
class PairingHttpClient(private val origin: ServerOrigin, client: OkHttpClient) : PairingApi {
    private val client = client.newBuilder().followRedirects(false).followSslRedirects(false)
        .retryOnConnectionFailure(false).callTimeout(10, java.util.concurrent.TimeUnit.SECONDS)
        .eventListenerFactory { call -> object : EventListener() {
            override fun requestHeadersStart(call: Call) { call.request().tag(RenewalAttempt::class.java)?.submitted?.set(true) }
        } }.build()
    override suspend fun register(pending: PendingPairing): PairingReply = reply(send("request", pending.invitationSecret,
        JSONObject().put("session_id", pending.sessionId.toString()).put("receiver_secret", pending.receiverSecret)
            .put("client_id", pending.clientId.toString()).put("device_label", pending.label)))
    override suspend fun status(pending: PendingPairing): PairingReply = reply(send("status", pending.receiverSecret, body(pending)))
    override suspend fun cancel(pending: PendingPairing): PairingReply = reply(send("cancel", pending.receiverSecret, body(pending)))
    override suspend fun claim(pending: PendingPairing): StoredCredential {
        val json = send("claim", pending.receiverSecret, body(pending))
        return parseCredential(json)
    }
    private fun parseCredential(json: JSONObject): StoredCredential {
        return try {
            val basic = setOf("device_id", "token_id", "access_token", "expires_at")
            val extended = setOf("session_id", "refresh_id", "refresh_credential", "refresh_expires_at", "session_expires_at")
            val keys = json.keys().asSequence().toSet()
            require(keys == basic || keys == basic + extended)
            val token = json.getString("access_token")
            require(Regex("[A-Za-z0-9_-]{43}").matches(token))
            val session = if (keys == basic) null else SessionCredential(UUID.fromString(json.getString("session_id")),
                UUID.fromString(json.getString("refresh_id")), json.getString("refresh_credential"),
                Instant.parse(json.getString("refresh_expires_at")), Instant.parse(json.getString("session_expires_at")))
            StoredCredential(UUID.fromString(json.getString("device_id")), UUID.fromString(json.getString("token_id")),
                token, Instant.parse(json.getString("expires_at")), session).also {
                    require(it.expiresAt > Instant.now())
                    if (session != null) require(it.expiresAt <= session.refreshExpiresAt && session.refreshExpiresAt <= session.absoluteExpiresAt)
                }
        } catch (_: Exception) { throw PairingException() }
    }

    override suspend fun renew(credential: StoredCredential): StoredCredential {
        val session = credential.session ?: throw RenewalException(RenewalFailure.DENIED)
        val request = Request.Builder().url(origin.url.newBuilder().addPathSegments("api/security/refresh").build())
            .header("Authorization", "Refresh ${session.secret}").tag(RenewalAttempt::class.java, RenewalAttempt())
            .post(JSONObject().put("session_id", session.sessionId.toString()).put("device_id", credential.deviceId.toString())
                .put("refresh_id", session.refreshId.toString()).toString().toRequestBody("application/json".toMediaType())).build()
        val json = sendRequest(request, renewal = true)
        return try { parseCredential(json).also { require(it.session != null) } }
            catch (_: Exception) { throw RenewalException(RenewalFailure.UNCERTAIN) }
    }
    private fun body(pending: PendingPairing) = JSONObject().put("session_id", pending.sessionId.toString())
    private fun reply(json: JSONObject): PairingReply = try {
        val state = json.getString("state")
        require(state in setOf("pending", "approved", "claimed", "denied", "cancelled", "expired", "revoked"))
        val device = if (json.isNull("device_id")) null else UUID.fromString(json.getString("device_id"))
        val code = if (json.isNull("verification_code")) null else json.getString("verification_code")
        PairingReply(state, device, Instant.parse(json.getString("expires_at")), code)
    } catch (_: Exception) { throw PairingException() }

    private suspend fun send(operation: String, proof: String, body: JSONObject): JSONObject {
        if (!Regex("[A-Za-z0-9_-]{43}").matches(proof)) throw PairingException()
        val request = Request.Builder().url(origin.url.newBuilder().addPathSegments("api/pairing/$operation").build())
            .header("Authorization", "Pairing $proof").header("Content-Type", "application/json")
            .post(body.toString().toRequestBody("application/json".toMediaType())).build()
        return sendRequest(request)
    }
    private suspend fun sendRequest(request: Request, renewal: Boolean = false): JSONObject {
        return suspendCancellableCoroutine { continuation ->
            val call = client.newCall(request)
            continuation.invokeOnCancellation { call.cancel() }
            call.enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    if (continuation.isActive) continuation.resumeWithException(if (!renewal) PairingException() else
                        RenewalException(if (request.tag(RenewalAttempt::class.java)?.submitted?.get() == false)
                            RenewalFailure.NOT_SENT else RenewalFailure.UNCERTAIN))
                }
                override fun onResponse(call: Call, response: Response) {
                    try {
                        val json = response.use {
                            val source = it.body?.source() ?: throw PairingException()
                            source.request(8193)
                            val bytes = source.buffer.readByteArray()
                            if (bytes.size > 8192) throw PairingException()
                            val parsed = JSONObject(bytes.toString(Charsets.UTF_8))
                            if (!it.isSuccessful) {
                                if (!renewal) throw PairingException()
                                val code = parsed.optString("code")
                                throw RenewalException(when {
                                    it.code == 401 && code == "renewal_required" -> RenewalFailure.DENIED
                                    it.code in setOf(429, 503) && code in setOf("retry_later", "unavailable") &&
                                        parsed.has("request_applied") && parsed.getBoolean("request_applied") == false -> RenewalFailure.RETRY_LATER
                                    else -> RenewalFailure.UNCERTAIN
                                })
                            }
                            parsed
                        }
                        if (continuation.isActive) continuation.resume(json)
                    } catch (error: Exception) {
                        if (continuation.isActive) continuation.resumeWithException(if (!renewal) PairingException() else
                            if (error is RenewalException) error else RenewalException(RenewalFailure.UNCERTAIN))
                    }
                }
            })
        }
    }
}

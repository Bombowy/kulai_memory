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
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

class PairingReply(val state: String, val deviceId: UUID?, val expiresAt: Instant, val verificationCode: String?)
interface PairingApi {
    suspend fun register(pending: PendingPairing): PairingReply
    suspend fun status(pending: PendingPairing): PairingReply
    suspend fun claim(pending: PendingPairing): StoredCredential
    suspend fun cancel(pending: PendingPairing): PairingReply
}

/** Pre-auth proof is restricted to these four exact native POST paths and one validated origin. */
class PairingHttpClient(private val origin: ServerOrigin, client: OkHttpClient) : PairingApi {
    private val client = client.newBuilder().followRedirects(false).followSslRedirects(false)
        .retryOnConnectionFailure(false).callTimeout(10, java.util.concurrent.TimeUnit.SECONDS).build()
    override suspend fun register(pending: PendingPairing): PairingReply = reply(send("request", pending.invitationSecret,
        JSONObject().put("session_id", pending.sessionId.toString()).put("receiver_secret", pending.receiverSecret)
            .put("client_id", pending.clientId.toString()).put("device_label", pending.label)))
    override suspend fun status(pending: PendingPairing): PairingReply = reply(send("status", pending.receiverSecret, body(pending)))
    override suspend fun cancel(pending: PendingPairing): PairingReply = reply(send("cancel", pending.receiverSecret, body(pending)))
    override suspend fun claim(pending: PendingPairing): StoredCredential {
        val json = send("claim", pending.receiverSecret, body(pending))
        return try {
            val token = json.getString("access_token")
            require(Regex("[A-Za-z0-9_-]{43}").matches(token))
            StoredCredential(UUID.fromString(json.getString("device_id")), UUID.fromString(json.getString("token_id")),
                token, Instant.parse(json.getString("expires_at"))).also { require(it.expiresAt > Instant.now()) }
        } catch (_: Exception) { throw PairingException() }
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
        return suspendCancellableCoroutine { continuation ->
            val call = client.newCall(request)
            continuation.invokeOnCancellation { call.cancel() }
            call.enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    if (continuation.isActive) continuation.resumeWithException(PairingException())
                }
                override fun onResponse(call: Call, response: Response) {
                    try {
                        val json = response.use {
                            if (!it.isSuccessful) throw PairingException()
                            val source = it.body?.source() ?: throw PairingException()
                            source.request(8193)
                            val bytes = source.buffer.readByteArray()
                            if (bytes.size > 8192) throw PairingException()
                            JSONObject(bytes.toString(Charsets.UTF_8))
                        }
                        if (continuation.isActive) continuation.resume(json)
                    } catch (_: Exception) {
                        if (continuation.isActive) continuation.resumeWithException(PairingException())
                    }
                }
            })
        }
    }
}

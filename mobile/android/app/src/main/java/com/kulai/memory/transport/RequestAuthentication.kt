package com.kulai.memory.transport

import okhttp3.HttpUrl
import com.kulai.memory.pairing.ServerOrigin
import okhttp3.Request
import java.util.UUID
import java.time.Instant

enum class AuthenticationMode { LOCAL_DEV, SECURE }

/** PL: identyfikator nie jest sekretem. EN: opaque secret is never printable. */
class DeviceCredential(val deviceId: UUID, private val accessToken: String,
    val serverOrigin: String? = null, val expiresAt: Instant = Instant.MAX) {
    init { require(Regex("[A-Za-z0-9_-]{43}").matches(accessToken)) { "Invalid credentials." } }
    internal fun bearerValue(): String = "Bearer $accessToken"
    override fun toString(): String = "DeviceCredential([redacted])"
}

/** PL: bez I/O w transportach. EN: pairing/vault publishes a thread-safe credential snapshot. */
fun interface CredentialProvider {
    fun current(): DeviceCredential?
}

class AuthenticationException : Exception("Authentication required.")

/** Common HTTP/WS header policy; credentials are bound to one configured server origin. */
class RequestAuthentication(
    serverOrigin: String,
    private val mode: AuthenticationMode = AuthenticationMode.SECURE,
    private val credentials: CredentialProvider = CredentialProvider { null },
    private val onRejected: (UUID) -> Unit = {},
) {
    private val origin = try { ServerOrigin(serverOrigin, allowLocalCleartext = true) }
        catch (_: Exception) { throw AuthenticationException() }

    fun rejected(request: Request) {
        try {
            val device = request.header("X-Kulai-Device-ID") ?: return
            // PL/EN: a delayed denial of a predecessor must not invalidate the replacement.
            val active = credentials.current()
            if (active != null && request.header("Authorization") != active.bearerValue()) return
            onRejected(UUID.fromString(device))
        } catch (_: Exception) { }
    }

    fun authorize(request: Request): Request {
        try {
            val url = request.url
            if (!origin.contains(url) ||
                url.queryParameterNames.any { it.lowercase() in QUERY_CREDENTIALS } ||
                request.header("Authorization") != null || request.header("X-Kulai-Device-ID") != null) {
                throw AuthenticationException()
            }
            val local = isLoopback(url)
            if (mode == AuthenticationMode.LOCAL_DEV && !local || !local && !url.isHttps) {
                throw AuthenticationException()
            }
            val credential = credentials.current()
            if (credential == null) {
                if (mode != AuthenticationMode.LOCAL_DEV) throw AuthenticationException()
                return request
            }
            if (credential.expiresAt <= Instant.now() || credential.serverOrigin != null &&
                ServerOrigin(credential.serverOrigin, allowLocalCleartext = true).value != origin.value) throw AuthenticationException()
            return request.newBuilder().header("Authorization", credential.bearerValue())
                .header("X-Kulai-Device-ID", credential.deviceId.toString()).build()
        } catch (_: Exception) {
            // PL/EN: never propagate provider errors, URL, headers or secret values.
            throw AuthenticationException()
        }
    }

    private fun isLoopback(url: HttpUrl): Boolean {
        if (url.host == "localhost" || url.host == "::1") return true
        val octets = url.host.split('.')
        return octets.size == 4 && octets[0] == "127" && octets.all { it.toIntOrNull() in 0..255 }
    }

    companion object {
        private val QUERY_CREDENTIALS = setOf("token", "access_token", "authorization", "bearer", "api_key", "secret")
    }
}

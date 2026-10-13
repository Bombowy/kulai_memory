package com.kulai.memory.transport

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
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

/** Thread-safe, nonblocking snapshot. Pairing/secure storage will implement this port in TASK 4D.2. */
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
    private val origin = serverOrigin.toHttpUrl()

    fun rejected(request: Request) {
        val device = request.header("X-Kulai-Device-ID") ?: return
        try { onRejected(UUID.fromString(device)) } catch (_: Exception) { }
    }

    fun authorize(request: Request): Request {
        try {
            val url = request.url
            if (url.scheme != origin.scheme || url.host != origin.host || url.port != origin.port ||
                url.username.isNotEmpty() || url.password.isNotEmpty() ||
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
                credential.serverOrigin != origin.toString().removeSuffix("/")) throw AuthenticationException()
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

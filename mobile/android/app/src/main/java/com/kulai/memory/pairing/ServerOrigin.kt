package com.kulai.memory.pairing

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import java.net.URI

/** Explicit service base: root or /memory/. External transport must use HTTPS. */
class ServerOrigin(value: String, allowLocalCleartext: Boolean = false) {
    val url: HttpUrl
    val value: String
    init {
        try {
            // Check the original syntax before OkHttp can repair slashes or dot segments.
            val raw = URI(value)
            require(raw.isAbsolute && raw.rawAuthority != null && raw.rawUserInfo == null &&
                !raw.rawAuthority.endsWith(":") && raw.port != 0 &&
                raw.rawQuery == null && raw.rawFragment == null &&
                raw.rawPath in setOf("", "/", "/memory", "/memory/"))
            val parsed = value.toHttpUrl()
            require(parsed.username.isEmpty() && parsed.password.isEmpty() &&
                parsed.query == null && parsed.fragment == null &&
                (parsed.isHttps || allowLocalCleartext && loopback(parsed.host)))
            url = parsed.newBuilder().encodedPath(if (raw.rawPath.startsWith("/memory")) "/memory/" else "/").build()
            this.value = url.toString().removeSuffix("/")
        } catch (_: Exception) { throw PairingException() }
    }
    fun endpoint(path: String): HttpUrl {
        if (!Regex("[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*").matches(path)) throw PairingException()
        return url.newBuilder().addPathSegments(path).build()
    }
    fun contains(target: HttpUrl): Boolean = target.scheme == url.scheme && target.host == url.host &&
        target.port == url.port && target.username.isEmpty() && target.password.isEmpty() && target.fragment == null &&
        target.encodedPath.startsWith(url.encodedPath) &&
        Regex("/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]*").matches(target.encodedPath)

    fun webSocket(path: String): String = endpoint(path).toString()
        .replaceFirst(if (url.isHttps) "https://" else "http://", if (url.isHttps) "wss://" else "ws://")
    override fun toString(): String = value
    companion object {
        fun loopback(host: String): Boolean {
            if (host == "localhost" || host == "::1") return true
            val parts = host.split('.')
            return parts.size == 4 && parts[0] == "127" && parts.all { it.toIntOrNull() in 0..255 }
        }
    }
}

class PairingException : Exception("Pairing could not be completed.", null)

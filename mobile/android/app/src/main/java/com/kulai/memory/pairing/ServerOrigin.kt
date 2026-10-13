package com.kulai.memory.pairing

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl

/** PL/EN: explicit origin, no paths/userinfo/query; external transport must use HTTPS. */
class ServerOrigin(value: String, allowLocalCleartext: Boolean = false) {
    val url: HttpUrl
    val value: String
    init {
        try {
            val parsed = value.toHttpUrl()
            require(parsed.username.isEmpty() && parsed.password.isEmpty() && parsed.encodedPath == "/" &&
                parsed.query == null && parsed.fragment == null &&
                (parsed.isHttps || allowLocalCleartext && loopback(parsed.host)))
            url = parsed
            this.value = parsed.toString().removeSuffix("/")
        } catch (_: Exception) { throw PairingException() }
    }
    fun webSocket(path: String): String = url.newBuilder().addPathSegments(path).build().toString()
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

package com.kulai.memory.transport

import okio.ByteString.Companion.toByteString
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit

sealed interface SocketEvent {
    data object Opened : SocketEvent
    data class Text(val value: String) : SocketEvent
    data class Closed(val code: Int) : SocketEvent
    data object Failed : SocketEvent
}

interface VoiceSocket {
    fun sendText(value: String): Boolean
    fun sendBinary(value: ByteArray): Boolean
    fun close(code: Int = 1000): Boolean
    fun cancel()
}

fun interface VoiceSocketFactory {
    fun connect(endpoint: String, onEvent: (SocketEvent) -> Unit): VoiceSocket
}

class OkHttpVoiceSocketFactory(
    client: OkHttpClient =
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .build(),
    private val authentication: RequestAuthentication = RequestAuthentication("https://127.0.0.1:8000"),
) : VoiceSocketFactory {
    private val client = client.newBuilder().followRedirects(false).followSslRedirects(false)
        .retryOnConnectionFailure(false).build()
    override fun connect(endpoint: String, onEvent: (SocketEvent) -> Unit): VoiceSocket {
        val request = try { authentication.authorize(Request.Builder().url(endpoint).build()) }
        catch (_: Exception) {
            onEvent(SocketEvent.Failed)
            return object : VoiceSocket {
                override fun sendText(value: String) = false
                override fun sendBinary(value: ByteArray) = false
                override fun close(code: Int) = false
                override fun cancel() = Unit
            }
        }
        val adapter = OkHttpVoiceSocket(onEvent) { authentication.rejected(request) }
        adapter.bind(client.newWebSocket(request, adapter))
        return adapter
    }
}

private class OkHttpVoiceSocket(
    private val onEvent: (SocketEvent) -> Unit,
    private val onRejected: () -> Unit,
) : VoiceSocket, WebSocketListener() {
    @Volatile
    private var socket: WebSocket? = null
    private var conversationRejection = false

    fun bind(value: WebSocket) {
        socket = value
    }

    override fun onOpen(webSocket: WebSocket, response: Response) {
        onEvent(SocketEvent.Opened)
    }

    override fun onMessage(webSocket: WebSocket, text: String) {
        // V2 business errors use 1008 too. They must not invalidate a paired session.
        if (text.length <= 4096) runCatching {
            val event = JSONObject(text)
            if (event.optInt("schema_version") == 1 && event.optString("type") == "error" &&
                event.optJSONObject("payload")?.optString("code") in
                setOf("conversation.not_found", "conversation.request_conflict")) conversationRejection = true
        }
        onEvent(SocketEvent.Text(text))
    }

    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
        webSocket.close(code, null)
    }

    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
        if (code == 1008 && !conversationRejection) onRejected()
        onEvent(SocketEvent.Closed(code))
    }

    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
        if (response?.code == 401 || response?.code == 403) onRejected()
        onEvent(SocketEvent.Failed)
    }

    override fun sendText(value: String): Boolean = socket?.send(value) == true

    override fun sendBinary(value: ByteArray): Boolean =
        socket?.send(value.toByteString()) == true

    override fun close(code: Int): Boolean = socket?.close(code, null) == true

    override fun cancel() {
        socket?.cancel()
    }
}

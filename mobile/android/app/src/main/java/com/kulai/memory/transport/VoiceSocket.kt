package com.kulai.memory.transport

import okio.ByteString.Companion.toByteString
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
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
    private val client: OkHttpClient =
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .build(),
) : VoiceSocketFactory {
    override fun connect(endpoint: String, onEvent: (SocketEvent) -> Unit): VoiceSocket {
        val request = Request.Builder().url(endpoint).build()
        val adapter = OkHttpVoiceSocket(onEvent)
        adapter.bind(client.newWebSocket(request, adapter))
        return adapter
    }
}

private class OkHttpVoiceSocket(
    private val onEvent: (SocketEvent) -> Unit,
) : VoiceSocket, WebSocketListener() {
    @Volatile
    private var socket: WebSocket? = null

    fun bind(value: WebSocket) {
        socket = value
    }

    override fun onOpen(webSocket: WebSocket, response: Response) {
        onEvent(SocketEvent.Opened)
    }

    override fun onMessage(webSocket: WebSocket, text: String) {
        onEvent(SocketEvent.Text(text))
    }

    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
        webSocket.close(code, null)
    }

    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
        onEvent(SocketEvent.Closed(code))
    }

    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
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

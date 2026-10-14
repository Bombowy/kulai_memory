package com.kulai.memory.transport

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import org.junit.Assert.*
import org.junit.Test
import java.util.concurrent.TimeUnit

class RemoteDisconnectTest {
    @Test fun `failed websocket upgrade is a single attempt without reconnect`() = runBlocking {
        val server = MockWebServer()
        val client = OkHttpClient.Builder().readTimeout(5, TimeUnit.SECONDS).build()
        server.start()
        val failed = CompletableDeferred<Unit>()
        try {
            server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AFTER_REQUEST))
            server.enqueue(MockResponse().setResponseCode(503))
            val origin = server.url("/").toString()
            val socket = OkHttpVoiceSocketFactory(client, RequestAuthentication(origin, AuthenticationMode.LOCAL_DEV))
                .connect(server.url("/ws/memory").toString().replaceFirst("http", "ws")) {
                    if (it == SocketEvent.Failed) failed.complete(Unit)
                }
            withTimeout(5000) { failed.await() }
            assertEquals("/ws/memory", server.takeRequest(5, TimeUnit.SECONDS)!!.path)
            assertNull(server.takeRequest(300, TimeUnit.MILLISECONDS))
            assertEquals(1, server.requestCount)
            socket.cancel()
        } finally {
            client.dispatcher.cancelAll()
            client.connectionPool.evictAll()
            client.dispatcher.executorService.shutdown()
            server.shutdown()
        }
    }
}

package com.kulai.memory

import com.kulai.memory.audio.RecordingEnd
import com.kulai.memory.audio.VoiceAudioRecorder
import com.kulai.memory.cache.PendingAudioCacheStore
import com.kulai.memory.transport.OkHttpVoiceSocketFactory
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.ByteString
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.util.UUID
import java.util.concurrent.CopyOnWriteArrayList

class VoiceWebSocketMockServerTest {
    @get:Rule
    val temporaryFolder = TemporaryFolder()

    @Test
    fun `real OkHttp websocket completes the success flow`() = runBlocking {
        val server = MockWebServer()
        val receivedText = CopyOnWriteArrayList<String>()
        val receivedBinary = CopyOnWriteArrayList<ByteArray>()
        server.enqueue(
            websocketResponse(
                onText = { socket, value ->
                    receivedText += value
                    when (JSONObject(value).getString("type")) {
                        "recording.start" -> socket.send(event("session.ready", 1))
                        "recording.stop" -> {
                            socket.send(event("transcript.final", 2, "{\"text\":\"alpha\"}"))
                            socket.send(event("memory.saving", 3))
                            socket.send(savedEvent(4))
                            socket.close(1000, "complete")
                        }
                    }
                },
                onBinary = { _, value -> receivedBinary += value.toByteArray() },
            ),
        )
        server.start()
        val fixture = fixture(server)
        try {
            fixture.controller.startNewNote()
            waitUntil { fixture.controller.state.value.phase == VoiceMemoryPhase.RECORDING }
            waitUntil { receivedBinary.isNotEmpty() }
            fixture.controller.stopRecording()
            waitUntil { fixture.controller.state.value.phase == VoiceMemoryPhase.SAVED }

            assertArrayEquals(TEST_PCM, receivedBinary.single())
            assertEquals(
                listOf("recording.start", "recording.stop"),
                receivedText.map { JSONObject(it).getString("type") },
            )
        } finally {
            fixture.close()
            server.shutdown()
        }
    }

    @Test
    fun `recoverable server failure retries save without audio or STT`() = runBlocking {
        verifyRecoverableServerFailure("memory.save_failed")
    }

    @Test
    fun `saved memory indexing failure uses memory retry over OkHttp`() = runBlocking {
        verifyRecoverableServerFailure("memory.index_failed")
    }

    private suspend fun verifyRecoverableServerFailure(code: String) {
        val server = MockWebServer()
        val receivedTypes = CopyOnWriteArrayList<String>()
        server.enqueue(
            websocketResponse(
                onText = { socket, value ->
                    val type = JSONObject(value).getString("type")
                    receivedTypes += type
                    when (type) {
                        "recording.start" -> socket.send(event("session.ready", 1))
                        "recording.stop" -> {
                            socket.send(event("transcript.final", 2, "{\"text\":\"alpha\"}"))
                            socket.send(event("memory.saving", 3))
                            socket.send(
                                event(
                                    "error",
                                    4,
                                    "{\"code\":\"$code\",\"message\":\"safe\",\"recoverable\":true}",
                                ),
                            )
                        }
                        "memory.retry" -> {
                            socket.send(event("memory.saving", 5))
                            socket.send(savedEvent(6))
                            socket.close(1000, "complete")
                        }
                    }
                },
            ),
        )
        server.start()
        val fixture = fixture(server)
        try {
            fixture.controller.startNewNote()
            waitUntil { fixture.controller.state.value.phase == VoiceMemoryPhase.RECORDING }
            fixture.controller.stopRecording()
            waitUntil { fixture.controller.state.value.phase == VoiceMemoryPhase.RETRY_SAVE }
            fixture.controller.retrySave()
            waitUntil { fixture.controller.state.value.phase == VoiceMemoryPhase.SAVED }

            assertEquals(
                listOf("recording.start", "recording.stop", "memory.retry"),
                receivedTypes,
            )
            assertEquals(1, fixture.recorder.callCount)
        } finally {
            fixture.close()
            server.shutdown()
        }
    }

    @Test
    fun `reconnect replays cached PCM with the same ingestion ID`() = runBlocking {
        val server = MockWebServer()
        val ingestionIds = CopyOnWriteArrayList<String>()
        val replayed = CopyOnWriteArrayList<ByteArray>()
        server.enqueue(
            websocketResponse(
                onText = { socket, value ->
                    val json = JSONObject(value)
                    if (json.getString("type") == "recording.start") {
                        ingestionIds += json.getString("ingestion_id")
                        socket.send(event("session.ready", 1))
                    }
                },
                onBinary = { socket, _ -> socket.close(1011, "test disconnect") },
            ),
        )
        server.enqueue(
            websocketResponse(
                onText = { socket, value ->
                    val json = JSONObject(value)
                    when (json.getString("type")) {
                        "recording.start" -> {
                            ingestionIds += json.getString("ingestion_id")
                            socket.send(event("session.ready", 1))
                        }
                        "recording.stop" -> {
                            socket.send(event("transcript.final", 2, "{\"text\":\"alpha\"}"))
                            socket.send(event("memory.saving", 3))
                            socket.send(savedEvent(4))
                            socket.close(1000, "complete")
                        }
                    }
                },
                onBinary = { _, value -> replayed += value.toByteArray() },
            ),
        )
        server.start()
        val fixture = fixture(server)
        try {
            fixture.controller.startNewNote()
            waitUntil { fixture.controller.state.value.phase == VoiceMemoryPhase.RETRY_NOTE }
            fixture.controller.retryNote()
            waitUntil { fixture.controller.state.value.phase == VoiceMemoryPhase.SAVED }

            assertEquals(2, ingestionIds.size)
            assertEquals(ingestionIds[0], ingestionIds[1])
            assertArrayEquals(TEST_PCM, replayed.single())
            assertEquals(1, fixture.recorder.callCount)
        } finally {
            fixture.close()
            server.shutdown()
        }
    }

    private fun fixture(server: MockWebServer): Fixture {
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        val recorder = OneChunkRecorder()
        val endpoint = server.url("/ws/memory").toString().replaceFirst("http", "ws")
        val controller = VoiceMemoryController(
            scope = scope,
            endpoint = endpoint,
            socketFactory = OkHttpVoiceSocketFactory(),
            recorder = recorder,
            cacheStore = PendingAudioCacheStore(temporaryFolder.root),
        )
        controller.setMicrophonePermission(true)
        return Fixture(controller, recorder, scope)
    }

    private data class Fixture(
        val controller: VoiceMemoryController,
        val recorder: OneChunkRecorder,
        val scope: CoroutineScope,
    ) {
        fun close() {
            controller.shutdown()
            scope.cancel()
        }
    }

    private class OneChunkRecorder : VoiceAudioRecorder {
        @Volatile
        var callCount = 0
        private var stop = CompletableDeferred<Unit>()

        override suspend fun record(
            onStarted: () -> Unit,
            onPcm: suspend (ByteArray) -> Boolean,
        ): RecordingEnd {
            callCount += 1
            stop = CompletableDeferred()
            onStarted()
            if (!onPcm(TEST_PCM.copyOf())) {
                error("transport unavailable")
            }
            stop.await()
            return RecordingEnd.STOPPED
        }

        override fun requestStop() {
            stop.complete(Unit)
        }
    }

    private fun websocketResponse(
        onText: (WebSocket, String) -> Unit,
        onBinary: (WebSocket, ByteString) -> Unit = { _, _ -> },
    ): MockResponse = MockResponse().withWebSocketUpgrade(
        object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) = Unit

            override fun onMessage(webSocket: WebSocket, text: String) {
                onText(webSocket, text)
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                onBinary(webSocket, bytes)
            }
        },
    )

    private fun event(type: String, sequence: Int, payload: String = "{}"): String =
        "{\"schema_version\":1,\"type\":\"$type\",\"session_id\":\"$SESSION_ID\",\"sequence\":$sequence,\"payload\":$payload}"

    private fun savedEvent(sequence: Int): String = event(
        "memory.saved",
        sequence,
        "{\"memory_id\":\"${UUID.randomUUID()}\"}",
    )

    private suspend fun waitUntil(predicate: () -> Boolean) {
        withTimeout(10_000) {
            while (!predicate()) {
                delay(10)
            }
        }
    }

    companion object {
        private val SESSION_ID: UUID = UUID.randomUUID()
        private val TEST_PCM = byteArrayOf(1, 2, 3, 4)
    }
}

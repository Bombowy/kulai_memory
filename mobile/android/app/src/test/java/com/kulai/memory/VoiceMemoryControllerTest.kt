package com.kulai.memory

import com.kulai.memory.audio.RecordingEnd
import com.kulai.memory.audio.VoiceAudioRecorder
import com.kulai.memory.cache.PendingAudioCacheStore
import com.kulai.memory.transport.SocketEvent
import com.kulai.memory.transport.VoiceSocket
import com.kulai.memory.transport.VoiceSocketFactory
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.util.UUID

@OptIn(ExperimentalCoroutinesApi::class)
class VoiceMemoryControllerTest {
    @get:Rule
    val temporaryFolder = TemporaryFolder()

    @Test
    fun `recording flows through transcript and saved`() = runTest {
        val fixture = fixture()
        val connection = fixture.connectNewNote()
        val ingestionId = fixture.controller.state.value.ingestionId!!
        connection.emit(testEvent("session.ready", 1))
        runCurrent()
        assertEquals(VoiceMemoryPhase.RECORDING, fixture.controller.state.value.phase)
        assertArrayEquals(byteArrayOf(1, 2, 3, 4), connection.binary.single())

        fixture.controller.stopRecording()
        runCurrent()
        assertEquals(VoiceMemoryPhase.WAITING_FOR_TRANSCRIPT, fixture.controller.state.value.phase)
        assertEquals("recording.stop", JSONObject(connection.text.last()).getString("type"))

        val memoryId = UUID.randomUUID()
        connection.emit(testEvent("transcript.final", 2, "{\"text\":\"alpha\"}"))
        connection.emit(testEvent("memory.saving", 3))
        connection.emit(testEvent("memory.saved", 4, "{\"memory_id\":\"$memoryId\"}"))
        runCurrent()

        val state = fixture.controller.state.value
        assertEquals(VoiceMemoryPhase.SAVED, state.phase)
        assertEquals("alpha", state.transcript)
        assertEquals(memoryId, state.memoryId)
        assertEquals(ingestionId, state.ingestionId)
        assertEquals(1, fixture.recorder.callCount)
        assertFalse(hasOwnedCache())
    }

    @Test
    fun `empty transcript becomes no speech and removes cache`() = runTest {
        val fixture = fixture()
        val connection = fixture.connectAndStop()

        connection.emit(testEvent("transcript.final", 2, "{\"text\":\"   \"}"))
        runCurrent()

        assertEquals(VoiceMemoryPhase.NO_SPEECH, fixture.controller.state.value.phase)
        assertFalse(hasOwnedCache())
    }

    @Test
    fun `recoverable save retry keeps UUID and does not record twice`() = runTest {
        val fixture = fixture()
        val connection = fixture.connectAndStop()
        val ingestionId = fixture.controller.state.value.ingestionId
        connection.emit(testEvent("transcript.final", 2, "{\"text\":\"alpha\"}"))
        connection.emit(testEvent("memory.saving", 3))
        connection.emit(
            testEvent(
                "error",
                4,
                "{\"code\":\"memory.save_failed\",\"message\":\"safe\",\"recoverable\":true}",
            ),
        )
        runCurrent()

        assertEquals(VoiceMemoryPhase.RETRY_SAVE, fixture.controller.state.value.phase)
        fixture.controller.retrySave()
        runCurrent()
        assertEquals("memory.retry", JSONObject(connection.text.last()).getString("type"))
        connection.emit(testEvent("memory.saving", 5))
        connection.emit(
            testEvent("memory.saved", 6, "{\"memory_id\":\"${UUID.randomUUID()}\"}"),
        )
        runCurrent()

        assertEquals(VoiceMemoryPhase.SAVED, fixture.controller.state.value.phase)
        assertEquals(ingestionId, fixture.controller.state.value.ingestionId)
        assertEquals(1, fixture.recorder.callCount)
    }

    @Test
    fun `disconnect replays cache with the same ingestion UUID`() = runTest {
        val fixture = fixture()
        val first = fixture.connectNewNote()
        val ingestionId = fixture.controller.state.value.ingestionId!!
        first.emit(testEvent("session.ready", 1))
        runCurrent()
        first.emit(SocketEvent.Failed)
        runCurrent()
        assertEquals(VoiceMemoryPhase.RETRY_NOTE, fixture.controller.state.value.phase)
        assertTrue(hasOwnedCache())

        fixture.controller.retryNote()
        runCurrent()
        val second = fixture.sockets.connections[1]
        second.emit(SocketEvent.Opened)
        runCurrent()
        assertEquals(
            ingestionId.toString(),
            JSONObject(second.text.single()).getString("ingestion_id"),
        )
        second.emit(testEvent("session.ready", 1))
        runCurrent()

        assertArrayEquals(byteArrayOf(1, 2, 3, 4), second.binary.single())
        assertEquals("recording.stop", JSONObject(second.text.last()).getString("type"))
        second.emit(testEvent("transcript.final", 2, "{\"text\":\"alpha\"}"))
        second.emit(testEvent("memory.saving", 3))
        second.emit(
            testEvent("memory.saved", 4, "{\"memory_id\":\"${UUID.randomUUID()}\"}"),
        )
        runCurrent()

        assertEquals(VoiceMemoryPhase.SAVED, fixture.controller.state.value.phase)
        assertEquals(1, fixture.recorder.callCount)
        assertFalse(hasOwnedCache())
    }

    @Test
    fun `a new logical note receives a new ingestion UUID`() = runTest {
        val fixture = fixture()
        val connection = fixture.connectAndStop()
        val firstId = fixture.controller.state.value.ingestionId
        connection.emit(testEvent("transcript.final", 2, "{\"text\":\"alpha\"}"))
        connection.emit(testEvent("memory.saving", 3))
        connection.emit(
            testEvent("memory.saved", 4, "{\"memory_id\":\"${UUID.randomUUID()}\"}"),
        )
        runCurrent()

        fixture.controller.startNewNote()
        val secondId = fixture.controller.state.value.ingestionId

        assertNotEquals(firstId, secondId)
    }

    @Test
    fun `permission denial keeps recording disabled`() = runTest {
        val fixture = fixture(permission = false)

        fixture.controller.startNewNote()

        assertEquals(VoiceMemoryPhase.IDLE, fixture.controller.state.value.phase)
        assertFalse(fixture.controller.state.value.canRecord)
        assertTrue(fixture.sockets.connections.isEmpty())
    }

    private fun kotlinx.coroutines.test.TestScope.fixture(
        permission: Boolean = true,
    ): Fixture {
        val recorder = FakeRecorder()
        val sockets = FakeSocketFactory()
        val controller = VoiceMemoryController(
            scope = backgroundScope,
            endpoint = "ws://127.0.0.1:8000/ws/memory",
            socketFactory = sockets,
            recorder = recorder,
            cacheStore = PendingAudioCacheStore(temporaryFolder.root),
            ioDispatcher = StandardTestDispatcher(testScheduler),
        )
        controller.setMicrophonePermission(permission)
        return Fixture(controller, recorder, sockets, this)
    }

    private fun hasOwnedCache(): Boolean =
        temporaryFolder.root.walkTopDown().any { it.isFile && it.name.startsWith("note-") }

    private data class Fixture(
        val controller: VoiceMemoryController,
        val recorder: FakeRecorder,
        val sockets: FakeSocketFactory,
        val testScope: kotlinx.coroutines.test.TestScope,
    ) {
        fun connectNewNote(): FakeSocket {
            controller.startNewNote()
            assertEquals(controller.state.value.toString(), 1, sockets.connections.size)
            val socket = sockets.connections.single()
            socket.emit(SocketEvent.Opened)
            testScope.runCurrent()
            assertEquals("recording.start", JSONObject(socket.text.single()).getString("type"))
            return socket
        }

        fun connectAndStop(): FakeSocket {
            val socket = connectNewNote()
            socket.emit(testEvent("session.ready", 1))
            testScope.runCurrent()
            controller.stopRecording()
            testScope.runCurrent()
            return socket
        }
    }

    private class FakeRecorder : VoiceAudioRecorder {
        var callCount = 0
        private var stop = CompletableDeferred<Unit>()

        override suspend fun record(
            onStarted: () -> Unit,
            onPcm: suspend (ByteArray) -> Boolean,
        ): RecordingEnd {
            callCount += 1
            stop = CompletableDeferred()
            onStarted()
            if (!onPcm(byteArrayOf(1, 2, 3, 4))) {
                error("transport unavailable")
            }
            stop.await()
            return RecordingEnd.STOPPED
        }

        override fun requestStop() {
            stop.complete(Unit)
        }
    }

    private class FakeSocketFactory : VoiceSocketFactory {
        val connections = mutableListOf<FakeSocket>()

        override fun connect(endpoint: String, onEvent: (SocketEvent) -> Unit): VoiceSocket =
            FakeSocket(onEvent).also(connections::add)
    }

    private class FakeSocket(
        private val onEvent: (SocketEvent) -> Unit,
    ) : VoiceSocket {
        val text = mutableListOf<String>()
        val binary = mutableListOf<ByteArray>()
        var open = true

        fun emit(event: SocketEvent) = onEvent(event)

        override fun sendText(value: String): Boolean = open.also { if (it) text += value }

        override fun sendBinary(value: ByteArray): Boolean =
            open.also { if (it) binary += value.copyOf() }

        override fun close(code: Int): Boolean {
            open = false
            return true
        }

        override fun cancel() {
            open = false
        }
    }

}

private val TEST_SESSION_ID: UUID = UUID.randomUUID()

private fun testEvent(
    type: String,
    sequence: Int,
    payload: String = "{}",
    sessionId: UUID = TEST_SESSION_ID,
): SocketEvent.Text = SocketEvent.Text(
    "{\"schema_version\":1,\"type\":\"$type\",\"session_id\":\"$sessionId\",\"sequence\":$sequence,\"payload\":$payload}",
)

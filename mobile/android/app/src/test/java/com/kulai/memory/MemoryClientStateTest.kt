package com.kulai.memory

import com.kulai.memory.audio.*
import com.kulai.memory.cache.PendingAudioCacheStore
import com.kulai.memory.models.*
import com.kulai.memory.transport.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.*
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import kotlin.coroutines.Continuation
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine

@OptIn(ExperimentalCoroutinesApi::class)
class MemoryClientStateTest {
    @get:Rule val folder = TemporaryFolder()
    private class Fixture(scope: CoroutineScope, dispatcher: CoroutineDispatcher, cache: java.io.File) {
        val api = FakeMemoryApi(); val gate = MicrophoneGate()
        val noteSockets = QuestionSockets(); val questionSockets = QuestionSockets()
        val noteRecorder = QuestionRecorder(); val questionRecorder = QuestionRecorder()
        val note = VoiceMemoryController(scope, "ws://127.0.0.1/ws/memory", noteSockets, noteRecorder,
            PendingAudioCacheStore(cache), dispatcher, microphoneGate = gate)
        val ask = MemoryAssistantController(scope, api, "ws://127.0.0.1/ws/assistant", questionSockets,
            questionRecorder, gate, dispatcher)
        val history = MemoryHistoryController(scope, api)
        val root = MemoryClientController(scope, note, ask, history, gate)
    }
    private fun TestScope.fixture() = Fixture(backgroundScope, StandardTestDispatcher(testScheduler), folder.root)

    @Test fun `NOTE ASK MEMORY navigation keeps note query answer history and detail`() = runTest {
        val f = fixture(); assertEquals(MemoryTab.NOTE, f.root.tab.value)
        f.root.setMicrophonePermission(true); f.note.startNewNote(); runCurrent()
        val note = f.note.state.value
        f.root.selectTab(MemoryTab.ASK); f.ask.setQuery("Synthetic question"); f.ask.ask(); runCurrent()
        assertEquals(ANSWER, f.ask.state.value.result)
        f.root.selectTab(MemoryTab.MEMORY); runCurrent(); assertEquals(1, f.history.state.value.items.size)
        f.history.selectMemory(FIXTURE_ID); runCurrent(); val memoryState = f.history.state.value
        f.root.selectTab(MemoryTab.NOTE); assertEquals(note, f.note.state.value)
        f.root.selectTab(MemoryTab.ASK); assertEquals(ANSWER, f.ask.state.value.result)
        f.root.selectTab(MemoryTab.MEMORY); runCurrent(); assertEquals(memoryState, f.history.state.value)
        assertEquals(1, f.api.lists.size); f.root.shutdown()
    }
    @Test fun `denied permission disables both voice starts but permits text history and detail`() = runTest {
        val f = fixture(); f.root.setMicrophonePermission(false)
        f.ask.setQuery("Synthetic query")
        val controls = memoryClientControls(f.note.state.value, f.ask.state.value, f.gate.owner.value)
        assertFalse(controls.noteRecordEnabled); assertFalse(controls.voiceAskEnabled); assertTrue(controls.textAskEnabled)
        f.note.startNewNote(); f.ask.startVoiceQuestion(); runCurrent()
        assertTrue(f.noteSockets.connections.isEmpty()); assertTrue(f.questionSockets.connections.isEmpty())
        f.ask.ask(); f.root.selectTab(MemoryTab.MEMORY); runCurrent(); f.history.selectMemory(FIXTURE_ID); runCurrent()
        assertEquals(ANSWER, f.ask.state.value.result); assertNotNull(f.history.state.value.selectedDetail)
    }
    @Test fun `note connecting and recording block voice ASK with one capture`() = runTest {
        val f = fixture(); f.root.setMicrophonePermission(true); f.note.startNewNote()
        f.ask.startVoiceQuestion(); runCurrent(); assertTrue(f.questionSockets.connections.isEmpty())
        val s = f.noteSockets.connections.single(); s.emit(SocketEvent.Opened); s.emit(assistantEvent("session.ready", 1)); runCurrent()
        f.ask.startVoiceQuestion(); runCurrent(); assertTrue(f.questionSockets.connections.isEmpty())
        assertEquals(1, f.noteRecorder.requests); assertEquals(0, f.questionRecorder.requests)
        assertFalse(memoryClientControls(f.note.state.value, f.ask.state.value, f.gate.owner.value).voiceAskEnabled)
        f.note.stopRecording(); runCurrent(); assertNull(f.gate.owner.value)
    }
    @Test fun `voice ASK connecting and recording block note and rapid double tap`() = runTest {
        val f = fixture(); f.root.setMicrophonePermission(true); f.ask.startVoiceQuestion(); f.ask.startVoiceQuestion()
        f.note.startNewNote(); runCurrent(); assertTrue(f.noteSockets.connections.isEmpty()); assertEquals(1, f.questionSockets.connections.size)
        val s = f.questionSockets.connections.single(); s.emit(SocketEvent.Opened); s.emit(assistantEvent("session.ready", 1)); runCurrent()
        f.note.startNewNote(); runCurrent(); assertEquals(0, f.noteRecorder.requests); assertEquals(1, f.questionRecorder.requests)
        assertFalse(memoryClientControls(f.note.state.value, f.ask.state.value, f.gate.owner.value).noteRecordEnabled)
        assertTrue(folder.root.walkTopDown().none { it.isFile }) // Questions never create Note cache.
        f.root.shutdown(); runCurrent(); assertNull(f.gate.owner.value)
    }
    @Test fun `question cancellation keeps gate until noncooperative capture drains`() = runTest {
        val gate = MicrophoneGate(); val api = FakeMemoryApi(); val sockets = QuestionSockets()
        var drained: Continuation<RecordingEnd>? = null
        val recorder = object : VoiceAudioRecorder {
            override suspend fun record(onStarted: () -> Unit, onPcm: suspend (ByteArray) -> Boolean): RecordingEnd {
                onStarted(); return suspendCoroutine { drained = it }
            }
            override fun requestStop() { }
        }
        val ask = MemoryAssistantController(backgroundScope, api, "ws://127.0.0.1/ws/assistant", sockets,
            recorder, gate, StandardTestDispatcher(testScheduler))
        ask.setMicrophonePermission(true); ask.startVoiceQuestion(); runCurrent()
        val s = sockets.connections.single(); s.emit(SocketEvent.Opened); s.emit(assistantEvent("session.ready", 1)); runCurrent()
        ask.cancelQuestion(); runCurrent()
        assertEquals(CaptureOwner.QUESTION, gate.owner.value); assertNull(gate.acquire(CaptureOwner.NOTE))
        ask.startVoiceQuestion(); assertEquals(1, sockets.connections.size)
        drained!!.resume(RecordingEnd.STOPPED); runCurrent(); assertNull(gate.owner.value)
        assertEquals(AssistantPhase.IDLE, ask.state.value.phase); assertEquals(1, s.text.size)
        ask.startVoiceQuestion(); assertEquals(2, sockets.connections.size); ask.shutdown()
    }
    @Test fun `note shutdown keeps capture gate until recorder finally drains`() = runTest {
        val gate = MicrophoneGate(); val sockets = QuestionSockets(); var drained: Continuation<RecordingEnd>? = null
        val recorder = object : VoiceAudioRecorder {
            override suspend fun record(onStarted: () -> Unit, onPcm: suspend (ByteArray) -> Boolean): RecordingEnd {
                onStarted(); return suspendCoroutine { drained = it }
            }
            override fun requestStop() { }
        }
        val note = VoiceMemoryController(backgroundScope, "ws://127.0.0.1/ws/memory", sockets, recorder,
            PendingAudioCacheStore(folder.root), StandardTestDispatcher(testScheduler), microphoneGate = gate)
        note.setMicrophonePermission(true); note.startNewNote(); val s = sockets.connections.single()
        s.emit(SocketEvent.Opened); s.emit(assistantEvent("session.ready", 1)); runCurrent()
        note.shutdown(); runCurrent(); val stopped = note.state.value
        assertEquals(CaptureOwner.NOTE, gate.owner.value); assertNull(gate.acquire(CaptureOwner.QUESTION))
        drained!!.resume(RecordingEnd.STOPPED); runCurrent()
        assertNull(gate.owner.value); assertEquals(stopped, note.state.value)
        assertTrue(folder.root.walkTopDown().none { it.isFile })
    }
    @Test fun `Note save marks history stale without concurrent paging refresh`() = runTest {
        val f = fixture(); f.history.refresh(); runCurrent(); assertFalse(f.history.state.value.stale)
        f.root.setMicrophonePermission(true); f.note.startNewNote(); val s = f.noteSockets.connections.single()
        s.emit(SocketEvent.Opened); s.emit(assistantEvent("session.ready", 1)); runCurrent()
        f.note.stopRecording(); runCurrent()
        s.emit(assistantEvent("transcript.final", 2, """{"text":"Synthetic note"}"""))
        s.emit(assistantEvent("memory.saving", 3))
        s.emit(assistantEvent("memory.saved", 4, """{"memory_id":"$FIXTURE_ID"}""")); runCurrent()
        assertTrue(f.history.state.value.stale); assertEquals(1, f.api.lists.size)
        f.root.selectTab(MemoryTab.MEMORY); runCurrent(); assertEquals(2, f.api.lists.size)
        assertEquals(VoiceMemoryPhase.SAVED, f.note.state.value.phase)
    }
    @Test fun `UI controls and status cover busy and insufficient sources`() {
        val idle = VoiceMemoryUiState(permissionGranted = true)
        val ask = MemoryAssistantState(permissionGranted = true, query = "Synthetic")
        assertTrue(memoryClientControls(idle, ask, null).textAskEnabled)
        assertFalse(memoryClientControls(idle, ask.copy(query = " "), null).textAskEnabled)
        assertFalse(memoryClientControls(idle, ask.copy(phase = AssistantPhase.RECORDING), CaptureOwner.QUESTION).textAskEnabled)
        assertFalse(memoryClientControls(idle, ask.copy(phase = AssistantPhase.ASKING), null).voiceAskEnabled)
        assertEquals("Searching memory...", assistantStatusLabel(AssistantPhase.SEARCHING))
        assertEquals("No speech detected", assistantStatusLabel(AssistantPhase.NO_SPEECH))
        assertEquals("Not enough information", assistantStatusLabel(AssistantPhase.INSUFFICIENT_CONTEXT))
        assertTrue(INSUFFICIENT.citations.isEmpty()); assertEquals(FIXTURE_ID, ANSWER.citations.single().memoryId)
    }
    @Test fun `root shutdown is idempotent and blocks navigation`() = runTest {
        val f = fixture(); f.root.shutdown(); f.root.shutdown(); f.root.selectTab(MemoryTab.MEMORY); runCurrent()
        assertEquals(MemoryTab.NOTE, f.root.tab.value); assertTrue(f.api.lists.isEmpty())
    }
}

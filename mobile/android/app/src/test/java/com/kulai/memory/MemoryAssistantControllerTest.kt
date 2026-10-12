package com.kulai.memory

import com.kulai.memory.audio.*
import com.kulai.memory.models.*
import com.kulai.memory.transport.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.*
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import kotlin.coroutines.Continuation
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine

@OptIn(ExperimentalCoroutinesApi::class)
class MemoryAssistantControllerTest {
    private class Fixture(scope: CoroutineScope, dispatcher: CoroutineDispatcher) {
        val api = FakeMemoryApi(); val sockets = QuestionSockets(); val recorder = QuestionRecorder(); val gate = MicrophoneGate()
        val controller = MemoryAssistantController(scope, api, "ws://127.0.0.1:8000/ws/assistant", sockets, recorder, gate, dispatcher)
    }
    private fun TestScope.fixture() = Fixture(backgroundScope, StandardTestDispatcher(testScheduler))
    private fun TestScope.start(f: Fixture): QuestionSocket {
        f.controller.setMicrophonePermission(true); f.controller.startVoiceQuestion(); runCurrent()
        val socket = f.sockets.connections.last(); socket.emit(SocketEvent.Opened); runCurrent()
        socket.emit(assistantEvent("session.ready", 1)); runCurrent(); return socket
    }
    private fun TestScope.stop(f: Fixture, socket: QuestionSocket, text: String = "Gdzie mieszka smok?") {
        f.controller.stopVoiceQuestion(); runCurrent()
        socket.emit(assistantEvent("transcript.final", 2, JSONObject().put("text", text).toString())); runCurrent()
    }

    @Test fun `text validation is local and clears stale results`() = runTest {
        val f = fixture()
        for (q in listOf("", "  ", "x".repeat(10001))) {
            f.controller.setQuery(q); f.controller.ask(); runCurrent()
            assertEquals(AssistantPhase.ERROR, f.controller.state.value.phase); assertNull(f.controller.state.value.result)
        }
        assertTrue(f.api.asks.isEmpty()); assertEquals(0, f.recorder.requests); assertTrue(f.sockets.connections.isEmpty())
    }
    @Test fun `text answered with exact citations without microphone permission`() = runTest {
        val f = fixture(); f.controller.setQuery("Dokładnie to pytanie."); f.controller.ask()
        assertEquals(AssistantPhase.ASKING, f.controller.state.value.phase); runCurrent()
        assertEquals(listOf("Dokładnie to pytanie."), f.api.asks)
        assertEquals(ANSWER, f.controller.state.value.result); assertEquals(AssistantPhase.ANSWERED, f.controller.state.value.phase)
        assertEquals(0, f.recorder.requests); assertTrue(f.sockets.connections.isEmpty())
    }
    @Test fun `insufficient text result and safe failure`() = runTest {
        val f = fixture(); f.api.answer = { INSUFFICIENT }; f.controller.setQuery("Synthetic unsupported")
        f.controller.ask(); runCurrent(); assertEquals(AssistantPhase.INSUFFICIENT_CONTEXT, f.controller.state.value.phase)
        assertTrue(f.controller.state.value.result!!.citations.isEmpty())
        f.api.answer = { error("PRIVATE query content payload") }; f.controller.ask(); assertNull(f.controller.state.value.result); runCurrent()
        assertEquals("The memory assistant could not complete the operation.", f.controller.state.value.error)
    }
    @Test fun `second text ask cancels first and late result cannot overwrite`() = runTest {
        val f = fixture(); val replies = mutableListOf<Continuation<AssistantAnswer>>()
        f.api.answer = { suspendCoroutine { replies += it } }
        f.controller.setQuery("first"); f.controller.ask(); runCurrent()
        f.controller.setQuery("second"); f.controller.ask(); runCurrent()
        replies[1].resume(INSUFFICIENT); runCurrent(); replies[0].resume(ANSWER); runCurrent()
        assertEquals("second", f.controller.state.value.query); assertEquals(INSUFFICIENT, f.controller.state.value.result)
    }
    @Test fun `cooperative text call is cancelled on replacement and shutdown`() = runTest {
        val f = fixture(); var cancellations = 0
        f.api.answer = { try { awaitCancellation() } finally { cancellations++ } }
        f.controller.setQuery("first"); f.controller.ask(); runCurrent(); f.controller.ask(); runCurrent()
        assertEquals(1, cancellations); f.controller.shutdown(); runCurrent(); assertEquals(2, cancellations)
    }
    @Test fun `voice streams ephemeral PCM and renders transcript answer citations`() = runTest {
        val f = fixture(); val socket = start(f)
        assertEquals(AssistantPhase.RECORDING, f.controller.state.value.phase)
        assertEquals(CaptureOwner.QUESTION, f.gate.owner.value); assertArrayEquals(byteArrayOf(1, 2, 3, 4), socket.pcm.single())
        stop(f, socket); assertEquals("Gdzie mieszka smok?", f.controller.state.value.voiceTranscript)
        assertEquals("Gdzie mieszka smok?", f.controller.state.value.query)
        socket.emit(assistantEvent("rag.started", 3)); runCurrent(); assertEquals(AssistantPhase.SEARCHING, f.controller.state.value.phase)
        socket.emit(assistantEvent("assistant.completed", 4, answerJson())); runCurrent()
        assertEquals(ANSWER, f.controller.state.value.result); assertNull(f.gate.owner.value)
        assertTrue(f.api.asks.isEmpty()); assertTrue(f.api.lists.isEmpty()); assertTrue(f.api.details.isEmpty())
        assertEquals(listOf("question.start", "question.stop"), socket.text.map { JSONObject(it).getString("type") })
        assertTrue(socket.text.none { it.contains("ingestion") || it.contains("retry") || it.contains("recording.start") })
    }
    @Test fun `voice insufficient has no sources`() = runTest {
        val f = fixture(); val s = start(f); stop(f, s)
        s.emit(assistantEvent("rag.started", 3)); s.emit(assistantEvent("assistant.completed", 4, answerJson(INSUFFICIENT))); runCurrent()
        assertEquals(AssistantPhase.INSUFFICIENT_CONTEXT, f.controller.state.value.phase); assertTrue(f.controller.state.value.result!!.citations.isEmpty())
    }
    @Test fun `completed connection cannot overwrite answer with late events`() = runTest {
        val f = fixture(); val s = start(f); stop(f, s)
        s.emit(assistantEvent("rag.started", 3)); s.emit(assistantEvent("assistant.completed", 4, answerJson())); runCurrent()
        s.emit(assistantEvent("assistant.completed", 5, answerJson(INSUFFICIENT))); s.emit(SocketEvent.Failed); runCurrent()
        assertEquals(ANSWER, f.controller.state.value.result); assertEquals(AssistantPhase.ANSWERED, f.controller.state.value.phase)
    }
    @Test fun `empty voice transcript is terminal no speech`() = runTest {
        val f = fixture(); val s = start(f); stop(f, s, "   ")
        assertEquals(AssistantPhase.NO_SPEECH, f.controller.state.value.phase); assertNull(f.controller.state.value.result)
        assertTrue(f.api.asks.isEmpty()); assertTrue(s.cancelled); assertNull(f.gate.owner.value)
    }
    @Test fun `connection losses in each active phase stop capture and clear answer`() = runTest {
        for (phase in listOf(AssistantPhase.CONNECTING, AssistantPhase.RECORDING, AssistantPhase.TRANSCRIBING, AssistantPhase.SEARCHING)) {
            val f = fixture(); f.controller.setMicrophonePermission(true); f.controller.startVoiceQuestion(); runCurrent()
            val s = f.sockets.connections.single()
            if (phase != AssistantPhase.CONNECTING) { s.emit(SocketEvent.Opened); s.emit(assistantEvent("session.ready", 1)); runCurrent() }
            if (phase in listOf(AssistantPhase.TRANSCRIBING, AssistantPhase.SEARCHING)) stop(f, s)
            if (phase == AssistantPhase.SEARCHING) { s.emit(assistantEvent("rag.started", 3)); runCurrent() }
            s.emit(SocketEvent.Failed); runCurrent()
            assertEquals(AssistantPhase.ERROR, f.controller.state.value.phase); assertNull(f.controller.state.value.result)
            assertEquals("Local server is unavailable.", f.controller.state.value.error); assertNull(f.gate.owner.value)
            f.controller.shutdown()
        }
    }
    @Test fun `protocol violation and malicious server error are safe`() = runTest {
        val f = fixture(); val s = start(f)
        s.emit(assistantEvent("assistant.completed", 2, answerJson())); runCurrent()
        assertEquals("The server response was invalid.", f.controller.state.value.error)
        val t = start(f)
        t.emit(assistantEvent("error", 2, """{"code":"PRIVATE","message":"PRIVATE password","recoverable":false}""")); runCurrent()
        assertEquals("The memory assistant could not complete the operation.", f.controller.state.value.error)
    }
    @Test fun `stale socket event cannot update new question`() = runTest {
        val f = fixture(); val old = start(f); old.emit(SocketEvent.Failed); runCurrent()
        val current = start(f)
        old.emit(assistantEvent("assistant.completed", 4, answerJson())); old.emit(SocketEvent.Closed(1000)); runCurrent()
        assertEquals(AssistantPhase.RECORDING, f.controller.state.value.phase); assertNull(f.controller.state.value.result)
        stop(f, current, ""); assertEquals(AssistantPhase.NO_SPEECH, f.controller.state.value.phase)
    }
    @Test fun `double start cannot create second recorder and permission denial is local`() = runTest {
        val f = fixture(); f.controller.startVoiceQuestion(); assertTrue(f.sockets.connections.isEmpty())
        val s = start(f); f.controller.startVoiceQuestion(); runCurrent()
        assertEquals(1, f.recorder.requests); assertEquals(1, f.sockets.connections.size)
        f.controller.setMicrophonePermission(false); runCurrent()
        assertEquals(AssistantPhase.ERROR, f.controller.state.value.phase); assertTrue(s.cancelled); assertNull(f.gate.owner.value)
    }
    @Test fun `note capture gate rejects voice question before connecting`() = runTest {
        val f = fixture(); val noteLease = f.gate.acquire(CaptureOwner.NOTE)!!
        f.controller.setMicrophonePermission(true); f.controller.startVoiceQuestion(); runCurrent()
        assertTrue(f.sockets.connections.isEmpty()); assertEquals(0, f.recorder.requests)
        noteLease.release(); start(f); assertEquals(1, f.recorder.requests)
    }
    @Test fun `shutdown recording and searching ignores all later socket events`() = runTest {
        for (searching in listOf(false, true)) {
            val f = fixture(); val s = start(f)
            if (searching) { stop(f, s); s.emit(assistantEvent("rag.started", 3)); runCurrent() }
            f.controller.shutdown(); val state = f.controller.state.value; runCurrent()
            s.emit(assistantEvent("assistant.completed", 4, answerJson())); runCurrent()
            assertEquals(state, f.controller.state.value); assertTrue(s.cancelled); assertNull(f.gate.owner.value)
        }
    }
    @Test fun `cancel permits fresh text request with no stale voice answer`() = runTest {
        val f = fixture(); val s = start(f); f.controller.cancelQuestion(); runCurrent()
        f.controller.setQuery("new text"); f.controller.ask(); runCurrent()
        s.emit(assistantEvent("assistant.completed", 4, answerJson(INSUFFICIENT))); runCurrent()
        assertEquals(ANSWER, f.controller.state.value.result); assertEquals(listOf("new text"), f.api.asks)
    }
    @Test fun `duplicate transcript or rag started without transcript fail strictly`() = runTest {
        val f = fixture(); val s = start(f); stop(f, s)
        s.emit(assistantEvent("transcript.final", 3, """{"text":"different"}""")); runCurrent()
        assertEquals(AssistantPhase.ERROR, f.controller.state.value.phase)
        val t = start(f); f.controller.stopVoiceQuestion(); runCurrent()
        t.emit(assistantEvent("rag.started", 2)); runCurrent(); assertEquals(AssistantPhase.ERROR, f.controller.state.value.phase)
    }
}

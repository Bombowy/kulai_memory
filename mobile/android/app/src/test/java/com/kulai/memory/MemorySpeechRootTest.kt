package com.kulai.memory

import com.kulai.memory.audio.*
import com.kulai.memory.cache.PendingAudioCacheStore
import com.kulai.memory.models.*
import com.kulai.memory.speech.*
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
class MemorySpeechRootTest {
    @get:Rule val folder = TemporaryFolder()
    private class Fixture(scope: CoroutineScope, dispatcher: CoroutineDispatcher, cache: java.io.File) {
        val api = FakeMemoryApi(); val gate = MicrophoneGate(); val engine = FakeLocalSpeechEngine()
        val questionSockets = QuestionSockets(); val noteSockets = QuestionSockets()
        val note = VoiceMemoryController(scope, "ws://127.0.0.1/ws/memory", noteSockets, QuestionRecorder(),
            PendingAudioCacheStore(cache), dispatcher, microphoneGate = gate)
        val assistant = MemoryAssistantController(scope, api, "ws://127.0.0.1/ws/assistant", questionSockets,
            QuestionRecorder(), gate, dispatcher)
        val history = MemoryHistoryController(scope, api); val speech = MemorySpeechController(scope, api, engine)
        val root = MemoryClientController(scope, note, assistant, history, gate, speech)
    }
    private fun TestScope.fixture(unavailable: Boolean = false): Fixture = Fixture(backgroundScope, StandardTestDispatcher(testScheduler), folder.root)
        .also { it.engine.unavailable = unavailable; it.speech.startup(); runCurrent() }
    private fun TestScope.text(f: Fixture, answer: AssistantAnswer = ANSWER) {
        f.api.answer = { answer }; f.assistant.setQuery("Synthetic query"); f.root.ask(); runCurrent()
        assertEquals(answer, f.assistant.state.value.result) // Result renders before the common UI callback.
        f.root.answerRendered(f.assistant.state.value.answerVersion); runCurrent()
    }
    private fun TestScope.voice(f: Fixture) {
        f.root.setMicrophonePermission(true); f.root.startVoiceQuestion(); runCurrent()
        val s = f.questionSockets.connections.last(); s.emit(SocketEvent.Opened); s.emit(assistantEvent("session.ready", 1)); runCurrent()
        f.assistant.stopVoiceQuestion(); runCurrent(); s.emit(assistantEvent("transcript.final", 2, """{"text":"Synthetic question"}"""))
        s.emit(assistantEvent("rag.started", 3)); s.emit(assistantEvent("assistant.completed", 4, answerJson())); runCurrent()
        assertEquals(ANSWER, f.assistant.state.value.result)
        f.root.answerRendered(f.assistant.state.value.answerVersion); runCurrent()
    }
    @Test fun `Text ASK renders then auto speaks once without microphone permission`() = runTest {
        val f = fixture(); f.root.setMicrophonePermission(false); text(f)
        assertEquals(1, f.api.speechRequests.size); assertEquals(1, f.engine.spoken.size)
        f.root.answerRendered(f.assistant.state.value.answerVersion); runCurrent(); assertEquals(1, f.engine.spoken.size)
        assertFalse(f.note.state.value.canRecord); assertEquals(ANSWER, f.assistant.state.value.result)
    }
    @Test fun `Voice ASK auto speaks exactly once on same common result callback`() = runTest {
        val f = fixture(); voice(f); f.root.answerRendered(f.assistant.state.value.answerVersion); runCurrent()
        assertEquals(1, f.api.speechRequests.size); assertEquals(1, f.engine.spoken.size)
        assertEquals("Synthetic question", f.assistant.state.value.voiceTranscript)
    }
    @Test fun `auto OFF disables text and voice automatic speech but manual SPEAK works`() = runTest {
        val f = fixture(); f.speech.setAutoSpeak(false); text(f); voice(f)
        assertTrue(f.api.speechRequests.isEmpty()); f.root.speakAnswer(); runCurrent(); assertEquals(1, f.engine.spoken.size)
    }
    @Test fun `insufficient auto PL failed assistant zero and unavailable retains valid answer`() = runTest {
        val f = fixture(); text(f, INSUFFICIENT); assertEquals(SpeechLanguage.PL, f.engine.spoken.single().language)
        assertTrue(f.assistant.state.value.result!!.citations.isEmpty())
        f.api.answer = { error("PRIVATE query") }; f.root.ask(); runCurrent()
        f.root.answerRendered(f.assistant.state.value.answerVersion); runCurrent(); assertEquals(1, f.api.speechRequests.size)
        val missing = fixture(unavailable = true); text(missing)
        assertTrue(missing.api.speechRequests.isEmpty()); assertEquals(ANSWER, missing.assistant.state.value.result)
    }
    @Test fun `speech failure leaves rendered canonical answer and citations intact`() = runTest {
        val f = fixture(); f.engine.failSpeech = true; text(f)
        assertEquals(ANSWER, f.assistant.state.value.result); assertEquals(SpeechPhase.ERROR, f.speech.state.value.phase)
        assertEquals("Could not speak answer.", f.speech.state.value.error)
    }
    @Test fun `new Text ASK Voice ASK and Note wait for planner stop drain before operation`() = runTest {
        for (mode in listOf("text", "voice", "note")) {
            val f = fixture(); f.root.setMicrophonePermission(true)
            var late: Continuation<SpeechPlan>? = null
            f.api.planner = { suspendCoroutine { late = it } }
            text(f); assertEquals(SpeechPhase.PLANNING, f.speech.state.value.phase)
            when (mode) { "text" -> f.root.ask(); "voice" -> f.root.startVoiceQuestion(); else -> f.root.record() }
            runCurrent(); assertEquals(SpeechPhase.STOPPING, f.speech.state.value.phase)
            assertEquals(1, f.api.asks.size); assertTrue(f.questionSockets.connections.isEmpty()); assertTrue(f.noteSockets.connections.isEmpty())
            late!!.resume(SpeechPlan(listOf(SpeechSegment(ANSWER.answer, SpeechLanguage.EN)))); runCurrent()
            when (mode) { "text" -> assertEquals(2, f.api.asks.size); "voice" -> assertEquals(1, f.questionSockets.connections.size)
                else -> assertEquals(1, f.noteSockets.connections.size) }
            assertTrue(f.engine.spoken.isEmpty()); f.root.shutdown()
        }
    }
    @Test fun `new operation stops actual native utterance stale callbacks cannot finish new one`() = runTest {
        val api = FakeMemoryApi(); val native = FakeTtsBoundary().apply { autoDone = false }
        val engine = OfflineLocalSpeechEngine { it(true); native }
        val speech = MemorySpeechController(backgroundScope, api, engine); speech.startup(); runCurrent()
        speech.speak(ANSWER); runCurrent(); val old = native.spoken.single().id
        speech.stopAudio(); runCurrent(); speech.speak(ANSWER); runCurrent(); val current = native.spoken.last().id
        native.callback!!(old, true); runCurrent(); assertEquals(SpeechPhase.SPEAKING, speech.state.value.phase)
        native.callback!!(current, true); runCurrent(); assertEquals(SpeechPhase.READY, speech.state.value.phase)
        speech.shutdown(); speech.shutdown(); assertEquals(1, native.shutdowns)
    }
    @Test fun `speech continues across tabs and returning ASK never triggers a second playback`() = runTest {
        val f = fixture(); f.engine.holdSpeech = true; text(f)
        f.root.selectTab(MemoryTab.MEMORY); runCurrent(); assertEquals(SpeechPhase.SPEAKING, f.speech.state.value.phase)
        f.history.selectMemory(FIXTURE_ID); runCurrent(); assertNotNull(f.history.state.value.selectedDetail)
        f.root.selectTab(MemoryTab.ASK); f.root.answerRendered(f.assistant.state.value.answerVersion); runCurrent()
        assertEquals(1, f.api.speechRequests.size); assertEquals(1, f.engine.spoken.size)
        f.root.stopAudio(); runCurrent(); assertEquals(ANSWER, f.assistant.state.value.result)
    }
    @Test fun `speech does not start into an active microphone capture`() = runTest {
        val f = fixture(); f.root.setMicrophonePermission(true); f.root.record(); runCurrent()
        f.assistant.setQuery("Synthetic"); f.assistant.ask(); runCurrent(); f.root.answerRendered(f.assistant.state.value.answerVersion)
        f.root.speakAnswer(); runCurrent(); assertTrue(f.api.speechRequests.isEmpty()); f.root.shutdown()
    }
}

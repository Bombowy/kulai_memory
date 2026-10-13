package com.kulai.memory

import com.kulai.memory.models.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.*
import org.junit.Assert.*
import org.junit.Test
import kotlin.coroutines.Continuation
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine

@OptIn(ExperimentalCoroutinesApi::class)
class MemorySpeechControllerTest {
    private class Fixture(scope: CoroutineScope) {
        val api = FakeMemoryApi(); val engine = FakeLocalSpeechEngine()
        val controller = MemorySpeechController(scope, api, engine)
    }
    private fun TestScope.fixture(): Fixture = Fixture(backgroundScope).also { it.controller.startup(); runCurrent() }
    @Test fun `manual speak planner receives only final answer ordered segments and repeat`() = runTest {
        val f = fixture(); f.api.planner = { MIXED_SPEECH }
        f.controller.speak(MIXED_ANSWER); assertEquals(SpeechPhase.PLANNING, f.controller.state.value.phase); runCurrent()
        assertEquals(MIXED_SPEECH.segments, f.engine.spoken); assertEquals(listOf(MIXED_ANSWER.answer), f.api.speechRequests)
        assertEquals(SpeechPhase.READY, f.controller.state.value.phase); assertEquals(1, f.engine.initializations)
        f.controller.speak(MIXED_ANSWER); runCurrent(); assertEquals(6, f.engine.spoken.size)
        assertEquals(6, f.engine.ids.distinct().size); assertEquals(1, f.engine.initializations)
    }
    @Test fun `common automatic answer consumption is exactly once including repeated identical answer`() = runTest {
        val f = fixture(); f.controller.answerRendered(1, ANSWER); f.controller.answerRendered(1, ANSWER); runCurrent()
        assertEquals(1, f.api.speechRequests.size)
        f.controller.answerRendered(2, ANSWER); runCurrent(); assertEquals(2, f.api.speechRequests.size)
        f.controller.answerRendered(1, ANSWER); runCurrent(); assertEquals(2, f.api.speechRequests.size)
    }
    @Test fun `auto off suppresses automatic but manual repeat works`() = runTest {
        val f = fixture(); f.controller.setAutoSpeak(false); f.controller.answerRendered(1, ANSWER); runCurrent()
        assertTrue(f.api.speechRequests.isEmpty()); f.controller.speak(ANSWER); runCurrent(); assertEquals(1, f.engine.spoken.size)
    }
    @Test fun `insufficient uses one canonical PL segment preserves zero citations`() = runTest {
        val f = fixture(); f.controller.answerRendered(1, INSUFFICIENT); runCurrent()
        assertEquals(listOf(SpeechSegment(INSUFFICIENT.answer, SpeechLanguage.PL)), f.engine.spoken)
        assertTrue(INSUFFICIENT.citations.isEmpty()); assertEquals(listOf(INSUFFICIENT.answer), f.api.speechRequests)
    }
    @Test fun `planner failure or invalid text preserves original answer citations`() = runTest {
        val f = fixture(); val answer = ANSWER.copy(citations = ANSWER.citations.toList())
        for (wrongPlan in listOf(false, true)) {
            f.api.planner = { if (wrongPlan) SpeechPlan(listOf(SpeechSegment("rewritten", SpeechLanguage.PL))) else error("PRIVATE answer prompt") }
            f.controller.speak(answer); runCurrent()
            assertEquals(SpeechPhase.ERROR, f.controller.state.value.phase)
            assertEquals("Could not speak answer.", f.controller.state.value.error)
            assertEquals(ANSWER, answer); assertTrue(f.engine.spoken.isEmpty())
        }
    }
    @Test fun `TTS failure keeps answer available and next speech can retry`() = runTest {
        val f = fixture(); f.engine.failSpeech = true; f.controller.speak(ANSWER); runCurrent()
        assertEquals(SpeechPhase.ERROR, f.controller.state.value.phase); assertEquals(1, f.engine.spoken.size)
        f.engine.failSpeech = false; f.controller.speak(ANSWER); runCurrent(); assertEquals(SpeechPhase.READY, f.controller.state.value.phase)
    }
    @Test fun `unavailable does not call planner and engine shutdown only once`() = runTest {
        val f = Fixture(backgroundScope); f.engine.unavailable = true; f.controller.startup(); runCurrent()
        assertEquals(SpeechPhase.UNAVAILABLE, f.controller.state.value.phase)
        f.controller.answerRendered(1, ANSWER); f.controller.speak(ANSWER); runCurrent(); assertTrue(f.api.speechRequests.isEmpty())
        f.controller.shutdown(); f.controller.shutdown(); assertEquals(1, f.engine.shutdowns)
    }
    @Test fun `STOP during planning cancels HTTP and late plan cannot play`() = runTest {
        val f = fixture(); var late: Continuation<SpeechPlan>? = null
        f.api.planner = { suspendCoroutine { late = it } }
        f.controller.speak(ANSWER); runCurrent()
        val stopping = launch { f.controller.stopAudio() }; runCurrent()
        assertEquals(SpeechPhase.STOPPING, f.controller.state.value.phase); assertFalse(stopping.isCompleted)
        late!!.resume(SpeechPlan(listOf(SpeechSegment(ANSWER.answer, SpeechLanguage.EN)))); runCurrent(); stopping.join()
        assertTrue(f.engine.spoken.isEmpty()); assertEquals(SpeechPhase.READY, f.controller.state.value.phase)
    }
    @Test fun `cooperative STOP during planning cancels immediately and repeat speech works`() = runTest {
        val f = fixture(); var cancelled = false
        f.api.planner = { try { awaitCancellation() } finally { cancelled = true } }
        f.controller.speak(ANSWER); runCurrent(); f.controller.stopAudio(); runCurrent()
        assertTrue(cancelled); assertEquals(SpeechPhase.READY, f.controller.state.value.phase)
        f.api.planner = { SpeechPlan(listOf(SpeechSegment(it, SpeechLanguage.EN))) }
        f.controller.speak(ANSWER); runCurrent(); assertEquals(1, f.engine.spoken.size)
    }
    @Test fun `cancelled STOP caller retains drain ownership for the next operation`() = runTest {
        val f = fixture(); var late: Continuation<SpeechPlan>? = null
        f.api.planner = { suspendCoroutine { late = it } }; f.controller.speak(ANSWER); runCurrent()
        val firstStop = launch { f.controller.stopAudio() }; runCurrent(); firstStop.cancelAndJoin()
        val nextStop = launch { f.controller.stopAudio() }; runCurrent()
        assertFalse(nextStop.isCompleted)
        late!!.resume(SpeechPlan(listOf(SpeechSegment(ANSWER.answer, SpeechLanguage.EN)))); runCurrent()
        nextStop.join(); assertEquals(SpeechPhase.READY, f.controller.state.value.phase); assertTrue(f.engine.spoken.isEmpty())
    }
    @Test fun `invalid final text fails before planner or native speech`() = runTest {
        val f = fixture()
        for (text in listOf("", " ", "x".repeat(6001))) { f.controller.speak(ANSWER.copy(answer = text)); runCurrent() }
        assertTrue(f.api.speechRequests.isEmpty()); assertTrue(f.engine.spoken.isEmpty())
        assertEquals(SpeechPhase.ERROR, f.controller.state.value.phase)
    }
    @Test fun `STOP speaking cancels current utterance and not remaining segments`() = runTest {
        val f = fixture(); f.engine.holdSpeech = true; f.api.planner = { MIXED_SPEECH }
        f.controller.speak(MIXED_ANSWER); runCurrent(); assertEquals(SpeechPhase.SPEAKING, f.controller.state.value.phase)
        f.controller.stopAudio(); runCurrent(); assertEquals(1, f.engine.spoken.size)
        assertEquals(SpeechPhase.READY, f.controller.state.value.phase); assertEquals(0, f.engine.shutdowns)
    }
    @Test fun `shutdown planning and speaking prevents stale finish and closes once`() = runTest {
        for (planning in listOf(true, false)) {
            val f = fixture(); var late: Continuation<SpeechPlan>? = null
            if (planning) f.api.planner = { suspendCoroutine { late = it } } else f.engine.holdSpeech = true
            f.controller.speak(ANSWER); runCurrent(); f.controller.shutdown(); val closed = f.controller.state.value
            if (planning) late!!.resume(SpeechPlan(listOf(SpeechSegment(ANSWER.answer, SpeechLanguage.EN))))
            runCurrent(); f.controller.shutdown()
            assertEquals(closed, f.controller.state.value); assertEquals(1, f.engine.shutdowns)
        }
    }
    @Test fun `init once no synthesis startup and busy state is explicit`() = runTest {
        val f = fixture(); f.controller.startup(); runCurrent(); assertEquals(1, f.engine.initializations)
        assertTrue(f.api.speechRequests.isEmpty()); assertTrue(f.engine.spoken.isEmpty())
        assertEquals("offline-pl", f.controller.state.value.voices!!.polish.id)
        assertFalse(f.controller.state.value.busy)
    }
}

package com.kulai.memory.speech

import com.kulai.memory.*
import com.kulai.memory.models.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.*
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.Parameterized

@OptIn(ExperimentalCoroutinesApi::class)
class LocalSpeechEngineTest {
    @Test fun `init once deterministic exact offline voices selection and shutdown once`() = runTest {
        val native = FakeTtsBoundary()
        native.voices = listOf(PL_VOICE.copy(id = "z-pl"), EN_VOICE.copy(id = "z-en"), PL_VOICE,
            EN_VOICE, PL_VOICE.copy(id = "aaa-network", networkRequired = true), EN_VOICE.copy(id = "aaa-missing", installed = false),
            EN_VOICE.copy(id = "aaa-british", locale = "en-GB"))
        var created = 0
        val engine = OfflineLocalSpeechEngine { callback -> created++; callback(true); native }
        assertEquals(SelectedSpeechVoices(PL_VOICE, EN_VOICE), engine.initialize())
        engine.initialize(); assertEquals(1, created)
        engine.shutdown(); engine.shutdown(); assertEquals(1, native.shutdowns)
    }
    @Test fun `PL EN PL and reverse request order uses verified voice per segment`() = runTest {
        val native = FakeTtsBoundary(); val engine = OfflineLocalSpeechEngine { it(true); native }; engine.initialize()
        val all = MIXED_SPEECH.segments + listOf(SpeechSegment("English. ", SpeechLanguage.EN),
            SpeechSegment("Polski. ", SpeechLanguage.PL), SpeechSegment("English again.", SpeechLanguage.EN))
        for ((index, segment) in all.withIndex()) engine.speak(segment, "generation1:$index")
        assertEquals(listOf("offline-pl", "offline-en", "offline-pl", "offline-en", "offline-pl", "offline-en"), native.selections)
        assertEquals(all.map { it.text }, native.spoken.map { it.text }); assertEquals(6, native.spoken.map { it.id }.distinct().size)
        engine.shutdown()
    }
    @Test fun `sequential playback awaits current done and ignores stale callback`() = runTest {
        val native = FakeTtsBoundary(); native.autoDone = false
        val engine = OfflineLocalSpeechEngine { it(true); native }; engine.initialize()
        val operation = launch { for ((i, s) in MIXED_SPEECH.segments.withIndex()) engine.speak(s, "g1:$i") }
        runCurrent(); assertEquals(1, native.spoken.size)
        val first = native.spoken[0].id; native.callback!!(first, true); runCurrent(); assertEquals(2, native.spoken.size)
        native.callback!!(first, true); native.callback!!("old-generation:0:0", false); runCurrent(); assertEquals(2, native.spoken.size)
        native.callback!!(native.spoken[1].id, true); runCurrent(); assertEquals(3, native.spoken.size)
        native.callback!!(native.spoken[2].id, true); operation.join(); engine.shutdown()
    }
    @Test fun `stop cancels utterance and repeat speak works without reinitializing`() = runTest {
        val native = FakeTtsBoundary(); native.autoDone = false
        val engine = OfflineLocalSpeechEngine { it(true); native }; engine.initialize()
        val first = launch { engine.speak(SpeechSegment("Polski.", SpeechLanguage.PL), "old:0") }
        runCurrent(); val old = native.spoken.single().id; engine.stop(); runCurrent(); assertTrue(first.isCancelled)
        val second = launch { engine.speak(SpeechSegment("English.", SpeechLanguage.EN), "new:0") }
        runCurrent(); native.callback!!(old, false); runCurrent(); assertFalse(second.isCompleted)
        native.callback!!(native.spoken.last().id, true); second.join()
        assertEquals(0, native.shutdowns); engine.shutdown(); assertEquals(1, native.shutdowns)
    }
    @Test fun `chunks obey native bound preserve text and do not split surrogate pairs`() = runTest {
        val native = FakeTtsBoundary(); native.maxInputLength = 3
        val engine = OfflineLocalSpeechEngine { it(true); native }; engine.initialize()
        val text = "ab😀 cd\nŻółć."
        engine.speak(SpeechSegment(text, SpeechLanguage.PL), "g:0")
        assertEquals(text, native.spoken.joinToString("") { it.text })
        assertTrue(native.spoken.all { it.text.length <= 3 && !it.text.last().isHighSurrogate() && !it.text.first().isLowSurrogate() })
        assertTrue(native.spoken.all { it.voice == PL_VOICE.id }); engine.shutdown()
    }
    @Test fun `voice set verification failure and synthesis enqueue failure stop safely`() = runTest {
        for (failure in listOf("select", "verify", "speak")) {
            val native = FakeTtsBoundary().apply { when (failure) {
                "select" -> selectSucceeds = false; "verify" -> verifySucceeds = false
                else -> { speakSucceeds = false; autoDone = false }
            } }
            val engine = OfflineLocalSpeechEngine { it(true); native }; engine.initialize()
            try { engine.speak(SpeechSegment("PRIVATE answer", SpeechLanguage.EN), "g:0"); fail() }
            catch (e: LocalSpeechException) { assertEquals("Could not speak answer.", e.message); assertNull(e.cause) }
            assertTrue(native.stops > 0)
            if (failure != "speak") assertTrue(native.spoken.isEmpty())
            engine.shutdown()
        }
    }
    @Test fun `onError is safe and stops remaining speech`() = runTest {
        val native = FakeTtsBoundary(); native.autoDone = false
        val engine = OfflineLocalSpeechEngine { it(true); native }; engine.initialize()
        val error = async { runCatching { engine.speak(MIXED_SPEECH.segments[0], "g:0") }.exceptionOrNull() }
        runCurrent(); native.callback!!(native.spoken.single().id, false); runCurrent()
        assertTrue(error.await() is LocalSpeechException); assertEquals(1, native.spoken.size); engine.shutdown()
    }
    @Test fun `shutdown while init callback pending never resurrects engine`() = runTest {
        val native = FakeTtsBoundary(); var init: ((Boolean) -> Unit)? = null
        val engine = OfflineLocalSpeechEngine { init = it; native }
        val job = launch { engine.initialize() }; runCurrent(); job.cancel(); engine.shutdown(); runCurrent()
        init!!(true); runCurrent(); assertTrue(job.isCancelled); assertEquals(1, native.shutdowns)
    }
    @Test fun `native stop failure shuts down instead of continuing speech`() = runTest {
        val native = FakeTtsBoundary(); val engine = OfflineLocalSpeechEngine { it(true); native }; engine.initialize()
        native.stopSucceeds = false
        assertThrows(LocalSpeechException::class.java) { engine.stop() }
        engine.shutdown(); assertEquals(1, native.shutdowns)
    }
}

@RunWith(Parameterized::class)
class UnavailableLocalSpeechTest(private val voices: List<InstalledSpeechVoice>, private val initSucceeds: Boolean) {
    @Test fun `missing network only and uninstalled exact locale voices are unavailable`() = runBlocking {
        val native = FakeTtsBoundary(); native.voices = voices
        val engine = OfflineLocalSpeechEngine { it(initSucceeds); native }
        try { engine.initialize(); fail() } catch (e: LocalSpeechException) { assertEquals("Could not speak answer.", e.message) }
        engine.shutdown(); assertEquals(1, native.shutdowns); assertTrue(native.spoken.isEmpty())
    }
    companion object {
        @JvmStatic @Parameterized.Parameters(name = "unavailable {index}") fun cases() = listOf(
            arrayOf(listOf(PL_VOICE), true), arrayOf(listOf(EN_VOICE), true), arrayOf(emptyList<InstalledSpeechVoice>(), true),
            arrayOf(listOf(PL_VOICE.copy(networkRequired = true), EN_VOICE), true),
            arrayOf(listOf(PL_VOICE, EN_VOICE.copy(networkRequired = true)), true),
            arrayOf(listOf(PL_VOICE.copy(installed = false), EN_VOICE), true),
            arrayOf(listOf(PL_VOICE, EN_VOICE.copy(locale = "en-GB")), true),
            arrayOf(listOf(PL_VOICE.copy(locale = "pl"), EN_VOICE), true), arrayOf(listOf(PL_VOICE, EN_VOICE), false))
    }
}

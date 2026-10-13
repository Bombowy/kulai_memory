package com.kulai.memory

import com.kulai.memory.models.*
import com.kulai.memory.speech.*
import com.kulai.memory.transport.MemoryApi
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

enum class SpeechPhase { UNAVAILABLE, READY, PLANNING, SPEAKING, STOPPING, ERROR }
data class MemorySpeechState(
    val phase: SpeechPhase = SpeechPhase.UNAVAILABLE,
    val available: Boolean = false,
    val voices: SelectedSpeechVoices? = null,
    val autoSpeak: Boolean = true,
    val error: String? = null,
) {
    val busy: Boolean get() = phase in setOf(SpeechPhase.PLANNING, SpeechPhase.SPEAKING, SpeechPhase.STOPPING)
}

/** Secondary presentation only. Never owns or changes the canonical answer/citations. */
class MemorySpeechController(private val scope: CoroutineScope, private val api: MemoryApi, private val engine: LocalSpeechEngine) {
    private val mutableState = MutableStateFlow(MemorySpeechState())
    val state = mutableState.asStateFlow()
    private var startupJob: Job? = null
    private var speechJob: Job? = null
    private var generation = 0L
    private var handledAnswerVersion = 0L
    private var closed = false
    private var engineShutdown = false

    fun startup() {
        if (closed || startupJob != null) return
        startupJob = scope.launch {
            try {
                val voices = engine.initialize()
                if (!closed) mutableState.value = state.value.copy(phase = SpeechPhase.READY, available = true, voices = voices)
            } catch (timeout: TimeoutCancellationException) { unavailable() }
            catch (cancel: CancellationException) { throw cancel }
            catch (_: Exception) { unavailable() }
        }
    }
    fun setAutoSpeak(enabled: Boolean) { if (!closed) mutableState.value = state.value.copy(autoSpeak = enabled) }
    fun answerRendered(version: Long, answer: AssistantAnswer, allowSpeech: Boolean = true) {
        if (closed || version <= handledAnswerVersion) return
        handledAnswerVersion = version
        if (allowSpeech && state.value.autoSpeak && state.value.available) speak(answer)
    }
    fun speak(answer: AssistantAnswer) {
        if (closed || !state.value.available) return
        val token = ++generation
        val previous = speechJob
        previous?.cancel()
        stopEngine()
        if (!state.value.available) return
        mutableState.value = state.value.copy(phase = SpeechPhase.PLANNING, error = null)
        speechJob = scope.launch {
            try {
                previous?.join()
                withTimeout(600000) {
                    require(answer.answer.isNotBlank() && answer.answer.length <= 6000) { "Invalid speech request." }
                    val plan = api.planSpeech(answer.answer).validate(answer.answer)
                    if (!owns(token)) return@withTimeout
                    mutableState.value = state.value.copy(phase = SpeechPhase.SPEAKING)
                    for ((index, segment) in plan.segments.withIndex()) {
                        ensureActive()
                        if (!owns(token)) return@withTimeout
                        engine.speak(segment, "kulai:$token:$index")
                    }
                }
                if (owns(token)) mutableState.value = state.value.copy(phase = SpeechPhase.READY)
            } catch (timeout: TimeoutCancellationException) { if (owns(token)) failed() }
            catch (cancel: CancellationException) { throw cancel }
            catch (error: Exception) {
                if (owns(token)) {
                    if (error is LocalSpeechException && error.unavailable) unavailable() else failed()
                }
            }
        }
    }
    suspend fun stopAudio() {
        if (closed) return
        generation++
        val active = speechJob
        if (state.value.busy) mutableState.value = state.value.copy(phase = SpeechPhase.STOPPING)
        active?.cancel()
        stopEngine()
        active?.join()
        if (speechJob === active) speechJob = null
        if (!closed && state.value.available) mutableState.value = state.value.copy(phase = SpeechPhase.READY, error = null)
    }
    fun shutdown() {
        if (closed) return
        closed = true; generation++
        speechJob?.cancel(); startupJob?.cancel()
        if (!engineShutdown) runCatching { engine.stop() }
        shutdownEngine()
    }
    private fun owns(token: Long) = !closed && generation == token
    private fun stopEngine() { if (!engineShutdown) try { engine.stop() } catch (_: Exception) { unavailable() } }
    private fun shutdownEngine() {
        if (!engineShutdown) { engineShutdown = true; runCatching { engine.shutdown() } }
    }
    private fun unavailable() {
        shutdownEngine()
        if (!closed) mutableState.value = state.value.copy(phase = SpeechPhase.UNAVAILABLE, available = false,
            error = "TTS unavailable")
    }
    private fun failed() {
        stopEngine()
        if (state.value.available) mutableState.value = state.value.copy(phase = SpeechPhase.ERROR, error = "Could not speak answer.")
    }
}

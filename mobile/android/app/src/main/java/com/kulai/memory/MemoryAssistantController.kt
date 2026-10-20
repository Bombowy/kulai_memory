package com.kulai.memory

import com.kulai.memory.audio.*
import com.kulai.memory.models.*
import com.kulai.memory.protocol.*
import com.kulai.memory.transport.*
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

enum class AssistantPhase {
    IDLE, ASKING, CONNECTING, RECORDING, TRANSCRIBING, SEARCHING,
    ANSWERED, INSUFFICIENT_CONTEXT, NO_SPEECH, ERROR,
}
data class MemoryAssistantState(
    val query: String = "",
    val voiceTranscript: String = "",
    val phase: AssistantPhase = AssistantPhase.IDLE,
    val result: AssistantAnswer? = null,
    val error: String? = null,
    val permissionGranted: Boolean = false,
    val answerVersion: Long = 0,
) {
    val voiceBusy: Boolean get() = phase in setOf(AssistantPhase.CONNECTING, AssistantPhase.RECORDING,
        AssistantPhase.TRANSCRIBING, AssistantPhase.SEARCHING)
    val canAsk: Boolean get() = !voiceBusy && query.isNotBlank() && query.length <= 10000
}

/** Ephemeral questions only. No ingestion identity, disk cache or Memory mutation path. */
class MemoryAssistantController(
    private val scope: CoroutineScope,
    private val api: MemoryApi,
    private val endpoint: String,
    private val socketFactory: VoiceSocketFactory,
    private val recorder: VoiceAudioRecorder,
    private val microphoneGate: MicrophoneGate,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
) {
    private val mutableState = MutableStateFlow(MemoryAssistantState())
    val state = mutableState.asStateFlow()
    private var httpJob: Job? = null
    private var captureJob: Job? = null
    private var timeoutJob: Job? = null
    private var socket: VoiceSocket? = null
    private var lease: MicrophoneGate.Lease? = null
    @Volatile private var generation = 0L
    @Volatile private var closed = false
    private var validator = AssistantSequenceValidator()
    private var transcriptReceived = false
    private val parser = AssistantEventParser()
    private val events = Channel<Pair<Long, SocketEvent>>(Channel.UNLIMITED)
    private val eventJob = scope.launch {
        for ((token, event) in events) if (owns(token)) handle(event, token)
    }

    fun setMicrophonePermission(granted: Boolean) {
        mutableState.value = state.value.copy(permissionGranted = granted)
        if (!granted && state.value.voiceBusy) fail("Microphone permission is required.")
    }
    fun setQuery(query: String) { if (!closed) mutableState.value = state.value.copy(query = query) }
    fun ask() {
        if (closed || state.value.voiceBusy) return
        val query = state.value.query
        val token = invalidate()
        if (query.isBlank() || query.length > 10000) {
            mutableState.value = state.value.copy(phase = AssistantPhase.ERROR, result = null,
                error = "Enter a question of 1 to 10000 characters.")
            return
        }
        mutableState.value = state.value.copy(phase = AssistantPhase.ASKING, result = null, voiceTranscript = "", error = null)
        httpJob = scope.launch {
            try {
                val answer = api.askMemory(query, 5)
                if (owns(token)) complete(answer)
            } catch (cancel: CancellationException) { throw cancel }
            catch (error: Exception) { if (owns(token)) fail(safeAssistantError(error)) }
        }
    }
    fun startVoiceQuestion() {
        if (closed || !state.value.permissionGranted || state.value.voiceBusy || captureJob?.isCompleted == false) return
        val acquired = microphoneGate.acquire(CaptureOwner.QUESTION) ?: return
        val token = invalidate()
        lease = acquired
        validator = AssistantSequenceValidator()
        transcriptReceived = false
        mutableState.value = state.value.copy(phase = AssistantPhase.CONNECTING, result = null, voiceTranscript = "", error = null)
        socket = try {
            socketFactory.connect(endpoint) { event -> events.trySend(token to event) }
        } catch (_: Exception) { null }
        if (socket == null) { fail("Local server is unavailable."); return }
        // Bounded connect/capture/STT/RAG lifetime; the recorder also enforces the shared audio limit.
        timeoutJob = scope.launch {
            delay((VoiceAudioContract.MAX_DURATION_SECONDS + 300L) * 1000)
            if (owns(token) && state.value.voiceBusy) fail("The memory assistant could not complete the operation.")
        }
    }
    fun stopVoiceQuestion() {
        if (state.value.phase != AssistantPhase.RECORDING) return
        mutableState.value = state.value.copy(phase = AssistantPhase.TRANSCRIBING)
        recorder.requestStop()
    }
    fun cancelQuestion() {
        if (closed) return
        invalidate()
        mutableState.value = state.value.copy(phase = AssistantPhase.IDLE, result = null, error = null)
    }
    fun shutdown() {
        if (closed) return
        closed = true; invalidate(); events.close(); eventJob.cancel()
    }
    private fun invalidate(): Long {
        generation++
        httpJob?.cancel(); httpJob = null
        timeoutJob?.cancel(); timeoutJob = null
        socket?.cancel(); socket = null
        recorder.requestStop(); captureJob?.cancel()
        if (captureJob?.isCompleted != false) lease?.release()
        return generation
    }
    private fun owns(token: Long) = !closed && generation == token
    private suspend fun handle(event: SocketEvent, token: Long) {
        when (event) {
            SocketEvent.Opened -> {
                if (state.value.phase != AssistantPhase.CONNECTING || socket?.sendText(AssistantClientMessages.questionStart()) != true)
                    fail("Local server is unavailable.")
            }
            is SocketEvent.Text -> {
                val parsed = try {
                    val parsed = withContext(ioDispatcher) { parser.parse(event.value) }
                    if (!owns(token)) return
                    parsed.also(validator::accept)
                } catch (_: ProtocolException) {
                    if (owns(token)) fail("The server response was invalid.")
                    return
                }
                when (parsed) {
                    is AssistantEvent.Ready -> {
                        if (state.value.phase != AssistantPhase.CONNECTING) { protocolFailure(); return }
                        launchCapture(token)
                    }
                    is AssistantEvent.Transcript -> {
                        if (state.value.phase != AssistantPhase.TRANSCRIBING || transcriptReceived) { protocolFailure(); return }
                        transcriptReceived = true
                        mutableState.value = state.value.copy(voiceTranscript = parsed.text, query = parsed.text)
                        if (parsed.text.isBlank()) {
                            mutableState.value = state.value.copy(phase = AssistantPhase.NO_SPEECH)
                            finishSocket()
                        }
                    }
                    is AssistantEvent.Started -> {
                        if (state.value.phase != AssistantPhase.TRANSCRIBING || !transcriptReceived || state.value.voiceTranscript.isBlank()) {
                            protocolFailure(); return
                        }
                        mutableState.value = state.value.copy(phase = AssistantPhase.SEARCHING)
                    }
                    is AssistantEvent.Completed -> {
                        if (state.value.phase != AssistantPhase.SEARCHING) { protocolFailure(); return }
                        complete(parsed.answer); finishSocket()
                    }
                    is AssistantEvent.ConversationCompleted -> protocolFailure()
                    is AssistantEvent.Error -> fail("The memory assistant could not complete the operation.")
                }
            }
            is SocketEvent.Closed, SocketEvent.Failed -> {
                if (state.value.voiceBusy) fail("Local server is unavailable.")
            }
        }
    }
    private fun launchCapture(token: Long) {
        if (captureJob?.isCompleted == false) { protocolFailure(); return }
        val ownedSocket = socket ?: return
        val ownedLease = lease
        captureJob = scope.launch {
            try {
                withContext(ioDispatcher) {
                    recorder.record(onStarted = {
                        scope.launch { if (owns(token) && state.value.phase == AssistantPhase.CONNECTING)
                            mutableState.value = state.value.copy(phase = AssistantPhase.RECORDING) }
                    }, onPcm = { pcm -> owns(token) && ownedSocket.sendBinary(pcm) })
                }
                if (owns(token) && state.value.voiceBusy) {
                    mutableState.value = state.value.copy(phase = AssistantPhase.TRANSCRIBING)
                    if (!ownedSocket.sendText(AssistantClientMessages.questionStop())) fail("Local server is unavailable.")
                }
            } catch (cancel: CancellationException) { throw cancel }
            catch (_: Exception) { if (owns(token)) fail("The memory assistant could not complete the operation.") }
        }
        captureJob?.invokeOnCompletion { ownedLease?.release() }
    }
    private fun complete(answer: AssistantAnswer) {
        mutableState.value = state.value.copy(result = answer, answerVersion = state.value.answerVersion + 1, error = null, phase =
            if (answer.status == AssistantStatus.ANSWERED) AssistantPhase.ANSWERED else AssistantPhase.INSUFFICIENT_CONTEXT)
    }
    private fun finishSocket() {
        generation++ // Retire terminal connection callbacks, including already queued events.
        timeoutJob?.cancel(); socket?.close(); socket = null
        if (captureJob?.isCompleted != false) lease?.release()
    }
    private fun protocolFailure() = fail("The server response was invalid.")
    private fun fail(message: String) {
        invalidate()
        mutableState.value = state.value.copy(phase = AssistantPhase.ERROR, result = null, error = message)
    }
    private fun safeAssistantError(error: Exception): String = when ((error as? MemoryHttpException)?.failure) {
        HttpFailure.UNAVAILABLE -> "Local server is unavailable."
        HttpFailure.INVALID_RESPONSE -> "The server response was invalid."
        else -> "The memory assistant could not complete the operation."
    }
}

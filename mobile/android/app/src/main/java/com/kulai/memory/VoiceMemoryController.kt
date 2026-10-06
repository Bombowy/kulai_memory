package com.kulai.memory

import com.kulai.memory.audio.RecordingEnd
import com.kulai.memory.audio.VoiceAudioContract
import com.kulai.memory.audio.VoiceAudioRecorder
import com.kulai.memory.cache.PendingAudioCache
import com.kulai.memory.cache.PendingAudioCacheStore
import com.kulai.memory.protocol.ClientMessages
import com.kulai.memory.protocol.EventSequenceValidator
import com.kulai.memory.protocol.ProtocolException
import com.kulai.memory.protocol.ServerEvent
import com.kulai.memory.protocol.ServerEventParser
import com.kulai.memory.transport.SocketEvent
import com.kulai.memory.transport.VoiceSocket
import com.kulai.memory.transport.VoiceSocketFactory
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.UUID

enum class VoiceMemoryPhase {
    IDLE,
    CONNECTING,
    READY,
    RECORDING,
    WAITING_FOR_TRANSCRIPT,
    SAVING,
    SAVED,
    NO_SPEECH,
    RETRY_SAVE,
    RETRY_NOTE,
    ERROR,
}

data class VoiceMemoryUiState(
    val phase: VoiceMemoryPhase = VoiceMemoryPhase.IDLE,
    val permissionGranted: Boolean = false,
    val elapsedMillis: Long = 0,
    val transcript: String = "",
    val statusDetail: String? = null,
    val ingestionId: UUID? = null,
    val memoryId: UUID? = null,
) {
    val canRecord: Boolean
        get() = permissionGranted && phase in setOf(
            VoiceMemoryPhase.IDLE,
            VoiceMemoryPhase.SAVED,
            VoiceMemoryPhase.NO_SPEECH,
            VoiceMemoryPhase.ERROR,
        )
    val canStop: Boolean
        get() = phase == VoiceMemoryPhase.RECORDING
    val canRetrySave: Boolean
        get() = phase == VoiceMemoryPhase.RETRY_SAVE
    val canRetryNote: Boolean
        get() = phase == VoiceMemoryPhase.RETRY_NOTE
}

private enum class ConnectionMode {
    CAPTURE,
    REPLAY,
}

private data class PendingNote(
    val ingestionId: UUID,
    val cache: PendingAudioCache,
    var transcript: String? = null,
)

class VoiceMemoryController(
    private val scope: CoroutineScope,
    private val endpoint: String,
    private val socketFactory: VoiceSocketFactory,
    private val recorder: VoiceAudioRecorder,
    private val cacheStore: PendingAudioCacheStore,
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
    private val parser: ServerEventParser = ServerEventParser(),
    private val newIngestionId: () -> UUID = UUID::randomUUID,
    private val nanoTime: () -> Long = System::nanoTime,
) {
    private val mutableState = MutableStateFlow(VoiceMemoryUiState())
    val state: StateFlow<VoiceMemoryUiState> = mutableState.asStateFlow()

    private var pending: PendingNote? = null
    private var socket: VoiceSocket? = null
    private var sequenceValidator = EventSequenceValidator()
    private var connectionMode = ConnectionMode.CAPTURE
    private var connectionGeneration = 0L
    private var captureJob: Job? = null
    private var elapsedJob: Job? = null
    @Volatile
    private var transportFailed = false
    private var closed = false
    private val socketEvents = Channel<Pair<Long, SocketEvent>>(Channel.UNLIMITED)
    private val socketEventJob = scope.launch {
        for ((generation, event) in socketEvents) {
            if (!closed && generation == connectionGeneration) {
                handleSocketEvent(generation, event)
            }
        }
    }
    fun startup() {
        scope.launch(ioDispatcher) {
            cacheStore.cleanupOrphans()
        }
    }

    fun setMicrophonePermission(granted: Boolean) {
        mutableState.value = mutableState.value.copy(
            permissionGranted = granted,
            statusDetail = if (granted) null else "Microphone permission is required.",
        )
    }

    fun startNewNote() {
        if (!mutableState.value.canRecord || closed || captureJob?.isActive == true) {
            return
        }
        cleanupPending()
        val ingestionId = newIngestionId()
        val cache = try {
            cacheStore.create()
        } catch (_: Exception) {
            showError("Private audio cache is unavailable.")
            return
        }
        pending = PendingNote(ingestionId, cache)
        mutableState.value = VoiceMemoryUiState(
            phase = VoiceMemoryPhase.CONNECTING,
            permissionGranted = mutableState.value.permissionGranted,
            ingestionId = ingestionId,
            statusDetail = "Connecting to the local server…",
        )
        openConnection(ConnectionMode.CAPTURE)
    }

    fun stopRecording() {
        if (!mutableState.value.canStop) {
            return
        }
        stopElapsedTimer()
        mutableState.value = mutableState.value.copy(
            phase = VoiceMemoryPhase.WAITING_FOR_TRANSCRIPT,
            statusDetail = "Finishing audio…",
        )
        recorder.requestStop()
    }

    fun retrySave() {
        if (!mutableState.value.canRetrySave) {
            return
        }
        val activeSocket = socket
        if (activeSocket == null || !activeSocket.sendText(ClientMessages.memoryRetry())) {
            handleConnectionLoss()
            return
        }
        mutableState.value = mutableState.value.copy(
            phase = VoiceMemoryPhase.SAVING,
            statusDetail = "Retrying save…",
        )
    }

    fun retryNote() {
        if (!mutableState.value.canRetryNote) {
            return
        }
        scope.launch {
            captureJob?.join()
            val note = pending
            if (note == null) {
                showError("The pending note is unavailable.")
                return@launch
            }
            note.cache.finishWriting()
            if (note.cache.size <= 0) {
                cleanupPending()
                showError("No cached audio is available to retry.")
                return@launch
            }
            mutableState.value = mutableState.value.copy(
                phase = VoiceMemoryPhase.CONNECTING,
                statusDetail = "Reconnecting to retry the note…",
            )
            openConnection(ConnectionMode.REPLAY)
        }
    }

    fun shutdown() {
        if (closed) {
            return
        }
        closed = true
        stopElapsedTimer()
        recorder.requestStop()
        captureJob?.cancel()
        captureJob = null
        socket?.cancel()
        socket = null
        socketEvents.close()
        socketEventJob.cancel()
        cleanupPending()
    }

    private fun openConnection(mode: ConnectionMode) {
        socket?.cancel()
        connectionMode = mode
        sequenceValidator = EventSequenceValidator()
        transportFailed = false
        val generation = ++connectionGeneration
        socket = try {
            socketFactory.connect(endpoint) { event ->
                socketEvents.trySend(generation to event)
            }
        } catch (_: Exception) {
            null
        }
        if (socket == null) {
            handleConnectionLoss()
        }
    }

    private suspend fun handleSocketEvent(generation: Long, event: SocketEvent) {
        when (event) {
            SocketEvent.Opened -> {
                val note = pending ?: return
                val activeSocket = socket
                if (activeSocket == null ||
                    !activeSocket.sendText(ClientMessages.recordingStart(note.ingestionId))
                ) {
                    handleConnectionLoss()
                }
            }
            is SocketEvent.Text -> handleServerText(generation, event.value)
            is SocketEvent.Closed,
            SocketEvent.Failed,
            -> handleConnectionLoss()
        }
    }

    private fun handleServerText(generation: Long, raw: String) {
        val event = try {
            parser.parse(raw).also(sequenceValidator::accept)
        } catch (_: ProtocolException) {
            failNonRecoverable("The server response did not match protocol v1.", 1002)
            return
        }

        when (event) {
            is ServerEvent.SessionReady -> {
                if (mutableState.value.phase != VoiceMemoryPhase.CONNECTING) {
                    failNonRecoverable("The server sent an event in an invalid state.", 1002)
                    return
                }
                mutableState.value = mutableState.value.copy(
                    phase = VoiceMemoryPhase.READY,
                    statusDetail = "Server ready.",
                )
                if (connectionMode == ConnectionMode.CAPTURE) {
                    launchCapture(generation)
                } else {
                    launchReplay(generation)
                }
            }
            is ServerEvent.TranscriptFinal -> {
                if (mutableState.value.phase != VoiceMemoryPhase.WAITING_FOR_TRANSCRIPT) {
                    failNonRecoverable("The server sent an event in an invalid state.", 1002)
                    return
                }
                pending?.transcript = event.text
                mutableState.value = mutableState.value.copy(transcript = event.text)
                if (event.text.isBlank()) {
                    cleanupPending()
                    mutableState.value = mutableState.value.copy(
                        phase = VoiceMemoryPhase.NO_SPEECH,
                        statusDetail = "No speech detected.",
                    )
                    socket?.close()
                }
            }
            is ServerEvent.MemorySaving -> {
                if (mutableState.value.phase !in setOf(
                        VoiceMemoryPhase.WAITING_FOR_TRANSCRIPT,
                        VoiceMemoryPhase.RETRY_SAVE,
                        VoiceMemoryPhase.SAVING,
                    )
                ) {
                    failNonRecoverable("The server sent an event in an invalid state.", 1002)
                    return
                }
                mutableState.value = mutableState.value.copy(
                    phase = VoiceMemoryPhase.SAVING,
                    statusDetail = "Saving…",
                )
            }
            is ServerEvent.MemorySaved -> {
                if (mutableState.value.phase != VoiceMemoryPhase.SAVING) {
                    failNonRecoverable("The server sent an event in an invalid state.", 1002)
                    return
                }
                cleanupPending()
                mutableState.value = mutableState.value.copy(
                    phase = VoiceMemoryPhase.SAVED,
                    memoryId = event.memoryId,
                    statusDetail = "Saved.",
                )
                socket?.close()
            }
            is ServerEvent.Error -> {
                if (event.code == "memory.save_failed" && event.recoverable) {
                    mutableState.value = mutableState.value.copy(
                        phase = VoiceMemoryPhase.RETRY_SAVE,
                        statusDetail = "Save failed — retry available.",
                    )
                } else {
                    failNonRecoverable("The voice-memory operation failed.", 1000)
                }
            }
        }
    }

    private fun launchCapture(generation: Long) {
        val note = pending ?: return
        captureJob = scope.launch {
            var result: RecordingEnd? = null
            var failure = false
            try {
                result = withContext(ioDispatcher) {
                    recorder.record(
                        onStarted = {
                            scope.launch {
                                if (generation == connectionGeneration && !transportFailed) {
                                    mutableState.value = mutableState.value.copy(
                                        phase = VoiceMemoryPhase.RECORDING,
                                        elapsedMillis = 0,
                                        statusDetail = "Recording…",
                                    )
                                    startElapsedTimer()
                                }
                            }
                        },
                        onPcm = { pcm ->
                            if (generation != connectionGeneration) {
                                return@record false
                            }
                            note.cache.append(pcm)
                            val sent = socket?.sendBinary(pcm) == true
                            if (!sent) {
                                transportFailed = true
                            }
                            sent
                        },
                    )
                }
            } catch (_: Exception) {
                failure = true
            }
            finishCapture(generation, note, result, failure)
        }
    }

    private fun finishCapture(
        generation: Long,
        note: PendingNote,
        result: RecordingEnd?,
        failed: Boolean,
    ) {
        stopElapsedTimer()
        note.cache.finishWriting()
        if (generation != connectionGeneration || pending !== note) {
            note.cache.delete()
            return
        }
        if (transportFailed) {
            enterRetryNoteOrError()
            return
        }
        if (failed || result == null) {
            failNonRecoverable("Microphone recording failed.", 1000)
            return
        }
        val activeSocket = socket
        if (activeSocket == null || !activeSocket.sendText(ClientMessages.recordingStop())) {
            enterRetryNoteOrError()
            return
        }
        mutableState.value = mutableState.value.copy(
            phase = VoiceMemoryPhase.WAITING_FOR_TRANSCRIPT,
            statusDetail = if (result == RecordingEnd.LIMIT_REACHED) {
                "Ten-minute limit reached. Transcribing…"
            } else {
                "Transcribing…"
            },
        )
    }

    private fun launchReplay(generation: Long) {
        val note = pending ?: return
        mutableState.value = mutableState.value.copy(
            phase = VoiceMemoryPhase.WAITING_FOR_TRANSCRIPT,
            statusDetail = "Replaying cached audio…",
        )
        captureJob = scope.launch {
            val sent = try {
                withContext(ioDispatcher) {
                    note.cache.forEachChunk(VoiceAudioContract.FRAME_BYTES) { pcm ->
                        socket?.sendBinary(pcm) == true
                    }
                }
            } catch (_: Exception) {
                false
            }
            if (generation != connectionGeneration || !sent ||
                socket?.sendText(ClientMessages.recordingStop()) != true
            ) {
                enterRetryNoteOrError()
            } else {
                mutableState.value = mutableState.value.copy(
                    phase = VoiceMemoryPhase.WAITING_FOR_TRANSCRIPT,
                    statusDetail = "Transcribing…",
                )
            }
        }
    }

    private fun handleConnectionLoss() {
        val phase = mutableState.value.phase
        if (phase in setOf(
                VoiceMemoryPhase.SAVED,
                VoiceMemoryPhase.NO_SPEECH,
                VoiceMemoryPhase.ERROR,
                VoiceMemoryPhase.IDLE,
            )
        ) {
            return
        }
        socket = null
        stopElapsedTimer()
        transportFailed = true
        recorder.requestStop()
        if (pending != null && phase !in setOf(VoiceMemoryPhase.CONNECTING, VoiceMemoryPhase.READY)) {
            mutableState.value = mutableState.value.copy(
                phase = VoiceMemoryPhase.RETRY_NOTE,
                statusDetail = "Connection lost — note retry available.",
            )
        } else if (connectionMode == ConnectionMode.REPLAY && pending != null) {
            mutableState.value = mutableState.value.copy(
                phase = VoiceMemoryPhase.RETRY_NOTE,
                statusDetail = "Connection lost — note retry available.",
            )
        } else {
            cleanupPending()
            showError("The local server is unavailable.")
        }
    }

    private fun enterRetryNoteOrError() {
        val note = pending
        note?.cache?.finishWriting()
        if (note != null && note.cache.size > 0) {
            mutableState.value = mutableState.value.copy(
                phase = VoiceMemoryPhase.RETRY_NOTE,
                statusDetail = "Connection lost — note retry available.",
            )
        } else {
            cleanupPending()
            showError("The note could not be sent.")
        }
    }

    private fun failNonRecoverable(message: String, closeCode: Int) {
        stopElapsedTimer()
        recorder.requestStop()
        cleanupPending()
        socket?.close(closeCode)
        socket = null
        showError(message)
    }

    private fun showError(message: String) {
        mutableState.value = mutableState.value.copy(
            phase = VoiceMemoryPhase.ERROR,
            statusDetail = message,
        )
    }

    private fun cleanupPending() {
        pending?.cache?.delete()
        pending = null
    }

    private fun startElapsedTimer() {
        stopElapsedTimer()
        val startedAt = nanoTime()
        elapsedJob = scope.launch {
            while (isActive && mutableState.value.phase == VoiceMemoryPhase.RECORDING) {
                mutableState.value = mutableState.value.copy(
                    elapsedMillis = (nanoTime() - startedAt) / 1_000_000,
                )
                delay(200)
            }
        }
    }

    private fun stopElapsedTimer() {
        elapsedJob?.cancel()
        elapsedJob = null
    }
}

package com.kulai.memory

import com.kulai.memory.audio.CaptureOwner
import com.kulai.memory.audio.MicrophoneGate
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

enum class MemoryTab { NOTE, ASK, MEMORY }

/** Root navigation/permission coordination; each feature retains its own state and ownership. */
class MemoryClientController(
    private val scope: CoroutineScope,
    val note: VoiceMemoryController,
    val assistant: MemoryAssistantController,
    val history: MemoryHistoryController,
    val microphone: MicrophoneGate,
    val speech: MemorySpeechController? = null,
) {
    private val mutableTab = MutableStateFlow(MemoryTab.NOTE)
    val tab = mutableTab.asStateFlow()
    private var closed = false
    private var operationGeneration = 0L
    private var operationJob: Job? = null
    private val savedObserver = scope.launch {
        var lastSaved: java.util.UUID? = null
        note.state.collect { state ->
            if (state.phase == VoiceMemoryPhase.SAVED && state.memoryId != lastSaved) {
                lastSaved = state.memoryId
                history.markStale()
            }
        }
    }
    fun selectTab(tab: MemoryTab) {
        if (closed) return
        mutableTab.value = tab
        if (tab == MemoryTab.MEMORY) history.refreshIfStale()
    }
    fun setMicrophonePermission(granted: Boolean) {
        note.setMicrophonePermission(granted); assistant.setMicrophonePermission(granted)
    }
    fun ask() = afterSpeechStopped { assistant.ask() }
    fun startVoiceQuestion() = afterSpeechStopped { assistant.startVoiceQuestion() }
    fun record() = afterSpeechStopped { note.startNewNote() }
    fun retryNote() = afterSpeechStopped { note.retryNote() }
    fun retrySave() = afterSpeechStopped { note.retrySave() }
    fun stopAudio() = afterSpeechStopped { }
    fun speakAnswer() {
        if (!closed && operationJob?.isActive != true && microphone.owner.value == null)
            assistant.state.value.result?.let { speech?.speak(it) }
    }
    fun answerRendered(version: Long) {
        val current = assistant.state.value
        if (!closed && version == current.answerVersion) current.result?.let {
            speech?.answerRendered(version, it, operationJob?.isActive != true && microphone.owner.value == null)
        }
    }
    private fun afterSpeechStopped(action: () -> Unit) {
        if (closed) return
        val token = ++operationGeneration
        operationJob?.cancel()
        operationJob = scope.launch {
            speech?.stopAudio()
            if (!closed && token == operationGeneration) action()
        }
    }
    fun shutdown() {
        if (closed) return
        closed = true; operationGeneration++; operationJob?.cancel(); savedObserver.cancel()
        speech?.shutdown()
        assistant.shutdown(); history.shutdown(); note.shutdown()
    }
}

data class MemoryClientControls(val noteRecordEnabled: Boolean, val voiceAskEnabled: Boolean, val textAskEnabled: Boolean)
fun memoryClientControls(note: VoiceMemoryUiState, ask: MemoryAssistantState, owner: CaptureOwner?): MemoryClientControls =
    MemoryClientControls(note.canRecord && owner == null,
        ask.permissionGranted && !ask.voiceBusy && ask.phase != AssistantPhase.ASKING && owner == null,
        ask.canAsk)

package com.kulai.memory

import com.kulai.memory.audio.CaptureOwner
import com.kulai.memory.audio.MicrophoneGate
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

enum class MemoryTab { NOTE, ASK, MEMORY }

/** Root navigation/permission coordination; each feature retains its own state and ownership. */
class MemoryClientController(
    scope: CoroutineScope,
    val note: VoiceMemoryController,
    val assistant: MemoryAssistantController,
    val history: MemoryHistoryController,
    val microphone: MicrophoneGate,
) {
    private val mutableTab = MutableStateFlow(MemoryTab.NOTE)
    val tab = mutableTab.asStateFlow()
    private var closed = false
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
    fun shutdown() {
        if (closed) return
        closed = true; savedObserver.cancel()
        assistant.shutdown(); history.shutdown(); note.shutdown()
    }
}

data class MemoryClientControls(val noteRecordEnabled: Boolean, val voiceAskEnabled: Boolean, val textAskEnabled: Boolean)
fun memoryClientControls(note: VoiceMemoryUiState, ask: MemoryAssistantState, owner: CaptureOwner?): MemoryClientControls =
    MemoryClientControls(note.canRecord && owner == null,
        ask.permissionGranted && !ask.voiceBusy && ask.phase != AssistantPhase.ASKING && owner == null,
        ask.canAsk)

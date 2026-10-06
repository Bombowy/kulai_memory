package com.kulai.memory

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.kulai.memory.audio.AndroidVoiceAudioRecorder
import com.kulai.memory.cache.PendingAudioCacheStore
import com.kulai.memory.transport.OkHttpVoiceSocketFactory

class VoiceMemoryViewModel(application: Application) : AndroidViewModel(application) {
    private val controller = VoiceMemoryController(
        scope = viewModelScope,
        endpoint = BuildConfig.VOICE_WS_URL,
        socketFactory = OkHttpVoiceSocketFactory(),
        recorder = AndroidVoiceAudioRecorder(application),
        cacheStore = PendingAudioCacheStore(application.cacheDir),
    )

    val state = controller.state

    init {
        controller.startup()
    }

    fun setMicrophonePermission(granted: Boolean) =
        controller.setMicrophonePermission(granted)

    fun record() = controller.startNewNote()

    fun stop() = controller.stopRecording()

    fun retrySave() = controller.retrySave()

    fun retryNote() = controller.retryNote()

    override fun onCleared() {
        controller.shutdown()
        super.onCleared()
    }
}

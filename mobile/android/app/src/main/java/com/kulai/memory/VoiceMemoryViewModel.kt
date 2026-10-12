package com.kulai.memory

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.kulai.memory.audio.AndroidVoiceAudioRecorder
import com.kulai.memory.audio.MicrophoneGate
import com.kulai.memory.cache.PendingAudioCacheStore
import com.kulai.memory.transport.OkHttpVoiceSocketFactory
import com.kulai.memory.transport.MemoryHttpClient
import okhttp3.OkHttpClient
import java.util.concurrent.TimeUnit

class VoiceMemoryViewModel(application: Application) : AndroidViewModel(application) {
    private val client = OkHttpClient.Builder().connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(210, TimeUnit.SECONDS).callTimeout(240, TimeUnit.SECONDS).build()
    private val socketFactory = OkHttpVoiceSocketFactory(client.newBuilder().readTimeout(0, TimeUnit.MILLISECONDS).build())
    private val api = MemoryHttpClient(BuildConfig.MEMORY_HTTP_BASE_URL, client)
    val microphone = MicrophoneGate()
    private val controller = VoiceMemoryController(
        scope = viewModelScope,
        endpoint = BuildConfig.VOICE_WS_URL,
        socketFactory = socketFactory,
        recorder = AndroidVoiceAudioRecorder(application),
        cacheStore = PendingAudioCacheStore(application.cacheDir),
        microphoneGate = microphone,
    )
    val assistant = MemoryAssistantController(viewModelScope, api, BuildConfig.ASSISTANT_WS_URL,
        socketFactory, AndroidVoiceAudioRecorder(application), microphone)
    val history = MemoryHistoryController(viewModelScope, api)
    val root = MemoryClientController(viewModelScope, controller, assistant, history, microphone)

    val state = controller.state

    init {
        controller.startup()
    }

    fun setMicrophonePermission(granted: Boolean) =
        root.setMicrophonePermission(granted)

    fun record() = controller.startNewNote()

    fun stop() = controller.stopRecording()

    fun retrySave() = controller.retrySave()

    fun retryNote() = controller.retryNote()

    override fun onCleared() {
        root.shutdown()
        client.dispatcher.cancelAll()
        client.connectionPool.evictAll()
        client.dispatcher.executorService.shutdown()
        super.onCleared()
    }
}

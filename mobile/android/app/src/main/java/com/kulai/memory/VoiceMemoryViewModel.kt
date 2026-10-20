package com.kulai.memory

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue
import androidx.compose.runtime.mutableStateOf
import com.kulai.memory.audio.AndroidVoiceAudioRecorder
import com.kulai.memory.audio.MicrophoneGate
import com.kulai.memory.cache.PendingAudioCacheStore
import com.kulai.memory.transport.OkHttpVoiceSocketFactory
import com.kulai.memory.transport.MemoryHttpClient
import com.kulai.memory.transport.RequestAuthentication
import com.kulai.memory.transport.AuthenticationMode
import com.kulai.memory.pairing.*
import com.kulai.memory.speech.AndroidTtsBoundary
import com.kulai.memory.speech.OfflineLocalSpeechEngine
import okhttp3.OkHttpClient
import java.util.concurrent.TimeUnit

class VoiceMemoryViewModel(application: Application) : AndroidViewModel(application) {
    private val appContext = application
    private var microphonePermissionGranted = false
    private val client = OkHttpClient.Builder().connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(210, TimeUnit.SECONDS).callTimeout(240, TimeUnit.SECONDS).build()
    val microphone = MicrophoneGate()
    val connection: PairingController = PairingController(viewModelScope, androidConnectionStore(application),
        BuildConfig.MEMORY_HTTP_BASE_URL, BuildConfig.SECURITY_MODE == "LOCAL_DEV", BuildConfig.DEBUG,
        apiFactory = { PairingHttpClient(it, client) }, onConnectionChanged = { origin, localDev ->
            features.root.shutdown()
            client.dispatcher.cancelAll()
            features = Features(origin, localDev)
            features.root.setMicrophonePermission(microphonePermissionGranted)
        }, renewalBusy = {
            microphone.owner.value != null || features.assistant.state.value.voiceBusy ||
                features.chat.state.value.busy ||
                features.note.state.value.phase in setOf(VoiceMemoryPhase.CONNECTING, VoiceMemoryPhase.READY,
                    VoiceMemoryPhase.RECORDING, VoiceMemoryPhase.WAITING_FOR_TRANSCRIPT, VoiceMemoryPhase.SAVING)
        })
    private var features: Features by mutableStateOf(Features(BuildConfig.MEMORY_HTTP_BASE_URL, BuildConfig.SECURITY_MODE == "LOCAL_DEV"))
    val root get() = features.root
    val assistant get() = features.assistant
    val history get() = features.history
    val speech get() = features.speech
    val chat get() = features.chat
    val state get() = features.note.state

    private inner class Features(originValue: String, localDev: Boolean) {
        private val origin = ServerOrigin(originValue, BuildConfig.DEBUG)
        private val authentication = RequestAuthentication(origin.value,
            if (localDev) AuthenticationMode.LOCAL_DEV else AuthenticationMode.SECURE, connection, connection::authenticationRejected)
        private val socketFactory = OkHttpVoiceSocketFactory(client.newBuilder().readTimeout(0, TimeUnit.MILLISECONDS).build(), authentication)
        private val api = MemoryHttpClient(origin.value, client, authentication)
        val note = VoiceMemoryController(viewModelScope, origin.webSocket("ws/memory"), socketFactory,
            AndroidVoiceAudioRecorder(appContext), PendingAudioCacheStore(appContext.cacheDir), microphoneGate = microphone)
        val assistant = MemoryAssistantController(viewModelScope, api, origin.webSocket("ws/assistant"), socketFactory,
            AndroidVoiceAudioRecorder(appContext), microphone)
        val history = MemoryHistoryController(viewModelScope, api)
        val chat = ChatController(viewModelScope, api, origin.webSocket("ws/assistant"), socketFactory,
            AndroidVoiceAudioRecorder(appContext), microphone)
        val speech = MemorySpeechController(viewModelScope, api, OfflineLocalSpeechEngine { callback ->
            AndroidTtsBoundary(appContext.applicationContext, callback)
        })
        val root = MemoryClientController(viewModelScope, note, assistant, history, microphone, speech, chat)
        init { note.startup(); speech.startup() }
    }
    init { connection.startup() }

    fun setMicrophonePermission(granted: Boolean) {
        microphonePermissionGranted = granted
        root.setMicrophonePermission(granted)
    }

    fun record() = root.record()

    fun stop() = features.note.stopRecording()

    fun retrySave() = root.retrySave()

    fun retryNote() = root.retryNote()

    override fun onCleared() {
        connection.shutdown()
        root.shutdown()
        client.dispatcher.cancelAll()
        client.connectionPool.evictAll()
        client.dispatcher.executorService.shutdown()
        super.onCleared()
    }
}

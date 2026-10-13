package com.kulai.memory.speech

import android.content.Context
import android.os.Bundle
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.speech.tts.Voice

/** Application Context only. No synthesizeToFile, download intent or network voice fallback. */
class AndroidTtsBoundary(context: Context, onInitialized: (Boolean) -> Unit) : TtsBoundary {
    private val native = TextToSpeech(context.applicationContext) { onInitialized(it == TextToSpeech.SUCCESS) }
    private var installed = emptyMap<String, Voice>()
    override val voices: List<InstalledSpeechVoice> get() {
        installed = native.voices.orEmpty().associateBy { it.name }
        return installed.values.map { InstalledSpeechVoice(it.name, it.locale.toLanguageTag(),
            it.isNetworkConnectionRequired, TextToSpeech.Engine.KEY_FEATURE_NOT_INSTALLED !in it.features.orEmpty()) }
    }
    override val maxInputLength: Int get() = TextToSpeech.getMaxSpeechInputLength()
    override fun listen(listener: (String, Boolean) -> Unit): Boolean =
        native.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String) { }
            override fun onDone(utteranceId: String) = listener(utteranceId, true)
            @Deprecated("Android compatibility callback")
            override fun onError(utteranceId: String) = listener(utteranceId, false)
            override fun onError(utteranceId: String, errorCode: Int) = listener(utteranceId, false)
            override fun onStop(utteranceId: String, interrupted: Boolean) = listener(utteranceId, false)
        }) == TextToSpeech.SUCCESS
    override fun selectVoice(id: String): Boolean = installed[id]?.let { native.setVoice(it) == TextToSpeech.SUCCESS } == true
    override fun activeVoiceId(): String? = native.voice?.name
    override fun speak(text: String, id: String): Boolean = native.speak(text, TextToSpeech.QUEUE_FLUSH, Bundle(), id) == TextToSpeech.SUCCESS
    override fun stop(): Boolean = native.stop() == TextToSpeech.SUCCESS
    override fun shutdown() = native.shutdown()
}

package com.kulai.memory

import com.kulai.memory.models.*
import com.kulai.memory.speech.*
import kotlinx.coroutines.CompletableDeferred

internal val PL_VOICE = InstalledSpeechVoice("offline-pl", "pl-PL", false)
internal val EN_VOICE = InstalledSpeechVoice("offline-en", "en-US", false)
internal val MIXED_SPEECH = SpeechPlan(listOf(SpeechSegment("To jest polski. ", SpeechLanguage.PL),
    SpeechSegment("This is English. ", SpeechLanguage.EN), SpeechSegment("I znowu polski.", SpeechLanguage.PL)))
internal val MIXED_ANSWER = ANSWER.copy(answer = MIXED_SPEECH.segments.joinToString("") { it.text })

internal class FakeTtsBoundary : TtsBoundary {
    override var voices = listOf(PL_VOICE, EN_VOICE)
    override var maxInputLength = 4000
    var callback: ((String, Boolean) -> Unit)? = null
    var selected: String? = null
    var selectSucceeds = true
    var verifySucceeds = true
    var speakSucceeds = true
    var stopSucceeds = true
    var autoDone = true
    var stops = 0
    var shutdowns = 0
    val selections = mutableListOf<String>()
    data class Spoken(val voice: String?, val text: String, val id: String)
    val spoken = mutableListOf<Spoken>()
    override fun listen(listener: (String, Boolean) -> Unit): Boolean { callback = listener; return true }
    override fun selectVoice(id: String): Boolean { selections += id; selected = id; return selectSucceeds }
    override fun activeVoiceId(): String? = if (verifySucceeds) selected else "WRONG_VOICE"
    override fun speak(text: String, id: String): Boolean {
        spoken += Spoken(selected, text, id)
        if (autoDone) callback?.invoke(id, true)
        return speakSucceeds
    }
    override fun stop(): Boolean { stops++; return stopSucceeds }
    override fun shutdown() { shutdowns++ }
}
internal class FakeLocalSpeechEngine : LocalSpeechEngine {
    var initializations = 0
    var shutdowns = 0
    var stops = 0
    var unavailable = false
    var failSpeech = false
    var holdSpeech = false
    var pending = CompletableDeferred<Unit>()
    val spoken = mutableListOf<SpeechSegment>()
    val ids = mutableListOf<String>()
    override suspend fun initialize(): SelectedSpeechVoices {
        initializations++
        if (unavailable) error("PRIVATE voice payload")
        return SelectedSpeechVoices(PL_VOICE, EN_VOICE)
    }
    override suspend fun speak(segment: SpeechSegment, utteranceId: String) {
        spoken += segment; ids += utteranceId
        if (failSpeech) error("PRIVATE answer payload")
        if (holdSpeech) { pending = CompletableDeferred(); pending.await() }
    }
    override fun stop() { stops++; pending.cancel() }
    override fun shutdown() { shutdowns++ }
}

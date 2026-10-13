package com.kulai.memory.speech

import com.kulai.memory.models.SpeechLanguage
import com.kulai.memory.models.SpeechSegment
import kotlinx.coroutines.*
import java.util.concurrent.atomic.AtomicReference
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

data class InstalledSpeechVoice(val id: String, val locale: String, val networkRequired: Boolean, val installed: Boolean = true)
data class SelectedSpeechVoices(val polish: InstalledSpeechVoice, val english: InstalledSpeechVoice)
class LocalSpeechException(val unavailable: Boolean = false) : Exception("Could not speak answer.")

interface LocalSpeechEngine {
    suspend fun initialize(): SelectedSpeechVoices
    suspend fun speak(segment: SpeechSegment, utteranceId: String)
    fun stop()
    fun shutdown()
}

/** Small platform boundary so tests exercise production routing/callback ownership. */
interface TtsBoundary {
    val voices: List<InstalledSpeechVoice>
    val maxInputLength: Int
    fun listen(listener: (id: String, succeeded: Boolean) -> Unit): Boolean
    fun selectVoice(id: String): Boolean
    fun activeVoiceId(): String?
    fun speak(text: String, id: String): Boolean
    fun stop(): Boolean
    fun shutdown()
}
fun interface TtsBoundaryFactory { fun create(onInitialized: (Boolean) -> Unit): TtsBoundary }

/** One persistent engine, offline exact pl-PL/en-US voices, one awaited utterance at a time. */
class OfflineLocalSpeechEngine(private val factory: TtsBoundaryFactory) : LocalSpeechEngine {
    private var boundary: TtsBoundary? = null
    private var selected: SelectedSpeechVoices? = null
    @Volatile private var closed = false
    private data class Pending(val id: String, val continuation: CancellableContinuation<Unit>)
    private val pending = AtomicReference<Pending?>(null)

    override suspend fun initialize(): SelectedSpeechVoices {
        if (closed) throw LocalSpeechException(unavailable = true)
        selected?.let { return it }
        if (boundary != null) throw LocalSpeechException()
        val initialized = CompletableDeferred<Boolean>()
        try {
            val created = factory.create { initialized.complete(it) }
            boundary = created
            if (!withTimeout(10000) { initialized.await() } || closed) throw LocalSpeechException()
            if (!created.listen { id, success -> complete(id, success) }) throw LocalSpeechException()
            val offline = created.voices.filter { !it.networkRequired && it.installed && it.id.isNotBlank() }
            fun choose(locale: String) = offline.filter { it.locale.equals(locale, ignoreCase = true) }
                .sortedBy { it.id }.firstOrNull() ?: throw LocalSpeechException()
            return SelectedSpeechVoices(choose("pl-PL"), choose("en-US")).also { selected = it }
        } catch (cancel: CancellationException) { shutdown(); throw cancel }
        catch (_: Exception) { shutdown(); throw LocalSpeechException() }
    }
    override suspend fun speak(segment: SpeechSegment, utteranceId: String) {
        if (closed) throw LocalSpeechException(unavailable = true)
        val voices = selected ?: throw LocalSpeechException(unavailable = true)
        val native = boundary ?: throw LocalSpeechException(unavailable = true)
        val voice = if (segment.language == SpeechLanguage.PL) voices.polish else voices.english
        val limit = native.maxInputLength
        if (limit < 2 || segment.text.length > 6000 || utteranceId.isBlank()) throw LocalSpeechException()
        var start = 0; var chunk = 0
        while (start < segment.text.length) {
            currentCoroutineContext().ensureActive()
            var end = minOf(start + limit, segment.text.length)
            if (end < segment.text.length && segment.text[end - 1].isHighSurrogate() && segment.text[end].isLowSurrogate()) end--
            val id = "$utteranceId:${chunk++}"
            try {
                withTimeout(120000) { speakChunk(native, voice.id, segment.text.substring(start, end), id) }
            } catch (cancel: CancellationException) { throw cancel }
            catch (_: Exception) { stop(); throw LocalSpeechException() }
            start = end
        }
    }
    private suspend fun speakChunk(native: TtsBoundary, voice: String, text: String, id: String): Unit =
        suspendCancellableCoroutine { continuation ->
            val owned = Pending(id, continuation)
            if (closed || !pending.compareAndSet(null, owned)) {
                continuation.resumeWithException(LocalSpeechException()); return@suspendCancellableCoroutine
            }
            continuation.invokeOnCancellation {
                if (pending.compareAndSet(owned, null)) runCatching { native.stop() }
            }
            try {
                if (!native.selectVoice(voice) || native.activeVoiceId() != voice || !native.speak(text, id)) complete(id, false)
            } catch (_: Exception) { complete(id, false) }
        }
    private fun complete(id: String, success: Boolean) {
        val owned = pending.get() ?: return
        if (owned.id != id || !pending.compareAndSet(owned, null)) return
        if (success) owned.continuation.resume(Unit) else owned.continuation.resumeWithException(LocalSpeechException())
    }
    override fun stop() {
        pending.getAndSet(null)?.continuation?.cancel()
        if (!closed && selected != null && boundary?.stop() != true) {
            shutdown(); throw LocalSpeechException(unavailable = true)
        }
    }
    override fun shutdown() {
        if (closed) return
        closed = true
        pending.getAndSet(null)?.continuation?.cancel()
        boundary?.shutdown()
        boundary = null; selected = null
    }
}

package com.kulai.memory

import com.kulai.memory.audio.*
import com.kulai.memory.models.*
import com.kulai.memory.protocol.*
import com.kulai.memory.transport.*
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.util.UUID

enum class ChatPhase { IDLE, CREATING, CONNECTING, RECORDING, TRANSCRIBING, GENERATING, NO_SPEECH, ERROR }
data class ChatState(
    val conversations: List<Conversation> = emptyList(), val nextCursor: ConversationCursor? = null,
    val listLoaded: Boolean = false, val loadingList: Boolean = false,
    val selected: Conversation? = null, val messages: List<ChatMessage> = emptyList(),
    val nextBeforeSequence: Long? = null, val loadingHistory: Boolean = false,
    val query: String = "", val phase: ChatPhase = ChatPhase.IDLE, val error: String? = null,
    val permissionGranted: Boolean = false, val pendingRequestId: UUID? = null,
    val latestAnswer: ChatMessage? = null,
) {
    val busy get() = phase in setOf(ChatPhase.CREATING, ChatPhase.CONNECTING, ChatPhase.RECORDING,
        ChatPhase.TRANSCRIBING, ChatPhase.GENERATING)
    val canSend get() = selected != null && !loadingHistory && !busy && query.isNotBlank() &&
        query.length <= 10000 && '\u0000' !in query
    val canRetry get() = selected != null && !loadingHistory && !busy && pendingRequestId != null
}

/** Server-owned history. Only in-flight identities/transcripts live in memory; no audio cache. */
class ChatController(private val scope: CoroutineScope, private val api: ConversationApi,
    private val endpoint: String, private val sockets: VoiceSocketFactory,
    private val recorder: VoiceAudioRecorder, private val microphone: MicrophoneGate,
    private val io: CoroutineDispatcher = Dispatchers.IO) {
    private data class Pending(val conversationId: UUID, val requestId: UUID, val question: String?, val kind: ChatInputKind)
    private val pending = mutableMapOf<UUID, Pending>()
    private val mutableState = MutableStateFlow(ChatState())
    val state = mutableState.asStateFlow()
    private var requestJob: Job? = null
    private var historyJob: Job? = null
    private var listJob: Job? = null
    private var captureJob: Job? = null
    private var timeoutJob: Job? = null
    private var socket: VoiceSocket? = null
    private var lease: MicrophoneGate.Lease? = null
    @Volatile private var generation = 0L
    @Volatile private var closed = false
    private var listGeneration = 0L
    private var voiceRequest: Pending? = null
    private var submittedVoice = false
    private var replayOnly = false
    private var transcriptReceived = false
    private var validator = AssistantSequenceValidator()
    private val parser = AssistantEventParser(conversationMode = true)
    private val events = Channel<Pair<Long, SocketEvent>>(Channel.UNLIMITED)
    private val eventJob = scope.launch { for ((token, event) in events) if (owns(token)) handle(event, token) }

    fun refreshIfNeeded() { if (!state.value.listLoaded && !state.value.loadingList) refreshConversations() }
    fun refreshConversations(more: Boolean = false) {
        if (closed || more && (state.value.nextCursor == null || state.value.loadingList)) return
        val cursor = if (more) state.value.nextCursor else null
        val token = ++listGeneration
        listJob?.cancel()
        mutableState.value = state.value.copy(loadingList = true, error = null)
        listJob = scope.launch {
            try {
                val page = api.listConversations(cursor = cursor)
                if (!closed && listGeneration == token) mutableState.value = state.value.copy(
                    conversations = ((if (more) state.value.conversations else emptyList()) + page.conversations)
                        .distinctBy { it.conversationId }, nextCursor = page.nextCursor, listLoaded = true, loadingList = false)
            } catch (cancel: CancellationException) { throw cancel }
            catch (_: Exception) { if (!closed && listGeneration == token) mutableState.value = state.value.copy(
                loadingList = false, error = "Nie można pobrać rozmów. Sprawdź połączenie i spróbuj ponownie.") }
        }
    }
    fun newConversation() {
        if (closed || state.value.phase == ChatPhase.CREATING) return
        discardUnsubmittedVoice()
        val token = invalidate()
        mutableState.value = state.value.copy(phase = ChatPhase.CREATING, error = null, loadingHistory = false)
        requestJob = scope.launch {
            try {
                val conversation = api.createConversation()
                if (owns(token)) {
                    mutableState.value = state.value.copy(conversations = listOf(conversation) +
                        state.value.conversations.filterNot { it.conversationId == conversation.conversationId })
                    selectConversation(conversation)
                }
            } catch (cancel: CancellationException) { throw cancel }
            catch (_: Exception) { if (owns(token)) fail() }
        }
    }
    fun selectConversation(conversation: Conversation) {
        if (closed) return
        discardUnsubmittedVoice()
        val token = invalidate()
        mutableState.value = state.value.copy(selected = conversation, messages = emptyList(), nextBeforeSequence = null,
            query = "", phase = ChatPhase.IDLE, error = null, latestAnswer = null,
            pendingRequestId = pending[conversation.conversationId]?.requestId, loadingHistory = true)
        fetchMessages(token, conversation.conversationId, null)
    }
    fun showConversations() {
        if (closed) return
        discardUnsubmittedVoice(); invalidate()
        mutableState.value = state.value.copy(selected = null, messages = emptyList(), latestAnswer = null,
            loadingHistory = false, query = "", phase = ChatPhase.IDLE, error = null, pendingRequestId = null)
        refreshConversations()
    }
    fun refreshMessages() {
        val selected = state.value.selected ?: return
        if (closed || state.value.busy) return
        val token = invalidate()
        mutableState.value = state.value.copy(loadingHistory = true, error = null)
        fetchMessages(token, selected.conversationId, null)
    }
    fun olderMessages() {
        val id = state.value.selected?.conversationId ?: return
        val before = state.value.nextBeforeSequence ?: return
        if (closed || state.value.busy || state.value.loadingHistory) return
        mutableState.value = state.value.copy(loadingHistory = true, error = null)
        fetchMessages(generation, id, before)
    }
    private fun fetchMessages(token: Long, id: UUID, before: Long?) {
        historyJob = scope.launch {
            try {
                val page = api.conversationMessages(id, beforeSequence = before)
                if (owns(token)) {
                    val messages = (page.messages + if (before == null) emptyList() else state.value.messages)
                        .distinctBy { it.messageId }.sortedBy { it.sequence }
                    if (messages.any { it.requestId == pending[id]?.requestId && it.role == ChatRole.ASSISTANT }) pending.remove(id)
                    mutableState.value = state.value.copy(messages = messages, nextBeforeSequence = page.nextBeforeSequence,
                        loadingHistory = false, pendingRequestId = pending[id]?.requestId)
                }
            } catch (cancel: CancellationException) { throw cancel }
            catch (_: Exception) { if (owns(token)) mutableState.value = state.value.copy(loadingHistory = false,
                error = "Nie można pobrać historii. Odśwież rozmowę.") }
        }
    }
    fun setQuery(value: String) { if (!closed && !state.value.busy) mutableState.value = state.value.copy(query = value) }
    fun setMicrophonePermission(granted: Boolean) {
        mutableState.value = state.value.copy(permissionGranted = granted)
        if (!granted && state.value.phase in setOf(ChatPhase.CONNECTING, ChatPhase.RECORDING) && !replayOnly) fail()
    }
    fun sendText() {
        if (closed || !state.value.canSend) return
        val id = state.value.selected!!.conversationId
        val question = state.value.query
        // Pressing SEND again on an uncertain identical text request is also a retry.
        val previous = pending[id]?.takeIf { it.kind == ChatInputKind.TEXT && it.question == question }
        send(previous ?: Pending(id, UUID.randomUUID(), question, ChatInputKind.TEXT))
    }
    fun retry() {
        if (closed || !state.value.canRetry) return
        val request = pending[state.value.selected!!.conversationId] ?: return
        if (request.question != null) send(request) else connectVoice(request, replay = true)
    }
    private fun send(request: Pending) {
        val token = invalidate()
        pending[request.conversationId] = request
        mutableState.value = state.value.copy(phase = ChatPhase.GENERATING, error = null,
            pendingRequestId = request.requestId, latestAnswer = null, query = request.question!!)
        requestJob = scope.launch {
            try {
                val result = api.askConversation(request.conversationId, request.requestId, request.question, request.kind)
                if (owns(token)) complete(result.turn)
            } catch (cancel: CancellationException) { throw cancel }
            catch (_: Exception) { if (owns(token)) fail() }
        }
    }
    fun startVoice() {
        if (closed || state.value.selected == null || state.value.loadingHistory || state.value.busy ||
            !state.value.permissionGranted || captureJob?.isCompleted == false) return
        val acquired = microphone.acquire(CaptureOwner.QUESTION) ?: return
        connectVoice(Pending(state.value.selected!!.conversationId, UUID.randomUUID(), null, ChatInputKind.VOICE),
            replay = false, acquired = acquired)
    }
    private fun connectVoice(request: Pending, replay: Boolean, acquired: MicrophoneGate.Lease? = null) {
        val token = invalidate()
        lease = acquired
        voiceRequest = request; submittedVoice = false; replayOnly = replay; transcriptReceived = false
        validator = AssistantSequenceValidator(); pending[request.conversationId] = request
        mutableState.value = state.value.copy(phase = ChatPhase.CONNECTING, error = null, query = "",
            latestAnswer = null, pendingRequestId = request.requestId)
        socket = try { sockets.connect(endpoint) { events.trySend(token to it) } } catch (_: Exception) { null }
        if (socket == null) { fail(); return }
        timeoutJob = scope.launch {
            delay((VoiceAudioContract.MAX_DURATION_SECONDS + 300L) * 1000)
            if (owns(token) && state.value.busy) fail()
        }
    }
    fun stopVoice() {
        if (state.value.phase != ChatPhase.RECORDING) return
        mutableState.value = state.value.copy(phase = ChatPhase.TRANSCRIBING)
        recorder.requestStop()
    }
    fun cancel() {
        if (closed) return
        discardUnsubmittedVoice(); invalidate()
        mutableState.value = state.value.copy(phase = ChatPhase.IDLE, error = null, loadingHistory = false,
            pendingRequestId = state.value.selected?.let { pending[it.conversationId]?.requestId })
    }
    private suspend fun handle(event: SocketEvent, token: Long) {
        when (event) {
            SocketEvent.Opened -> {
                val request = voiceRequest ?: return
                if (socket?.sendText(AssistantClientMessages.conversationStart(request.conversationId, request.requestId)) != true) fail()
            }
            is SocketEvent.Text -> {
                val parsed = try { withContext(io) { parser.parse(event.value) }.also { if (owns(token)) validator.accept(it) } }
                catch (cancel: CancellationException) { throw cancel }
                catch (_: Exception) { if (owns(token)) fail(); return }
                if (!owns(token)) return
                when (parsed) {
                    is AssistantEvent.Ready -> {
                        if (state.value.phase != ChatPhase.CONNECTING) { fail(); return }
                        if (replayOnly) {
                            mutableState.value = state.value.copy(phase = ChatPhase.TRANSCRIBING)
                            submittedVoice = true
                            if (socket?.sendText(AssistantClientMessages.questionStop()) != true) fail()
                        } else capture(token)
                    }
                    is AssistantEvent.Transcript -> {
                        if (state.value.phase != ChatPhase.TRANSCRIBING || transcriptReceived) { fail(); return }
                        transcriptReceived = true
                        if (parsed.text.isBlank()) {
                            val request = voiceRequest!!
                            if (!replayOnly) pending.remove(request.conversationId)
                            retireSocket()
                            mutableState.value = state.value.copy(phase = ChatPhase.NO_SPEECH,
                                pendingRequestId = pending[request.conversationId]?.requestId,
                                error = if (replayOnly) "Brak zapisanej odpowiedzi. Odśwież historię lub nagraj pytanie ponownie." else null)
                        } else {
                            val request = voiceRequest!!.copy(question = parsed.text)
                            voiceRequest = request; pending[request.conversationId] = request
                            mutableState.value = state.value.copy(query = parsed.text)
                        }
                    }
                    is AssistantEvent.Started -> {
                        if (state.value.phase != ChatPhase.TRANSCRIBING || !transcriptReceived || voiceRequest?.question.isNullOrBlank()) fail()
                        else mutableState.value = state.value.copy(phase = ChatPhase.GENERATING)
                    }
                    is AssistantEvent.ConversationCompleted -> {
                        val request = voiceRequest!!; val turn = parsed.result.turn
                        if (state.value.phase != ChatPhase.GENERATING || turn.conversationId != request.conversationId ||
                            turn.requestId != request.requestId || turn.user.content != request.question || turn.user.inputKind != ChatInputKind.VOICE) fail()
                        else { complete(turn); retireSocket() }
                    }
                    is AssistantEvent.Completed, is AssistantEvent.Error -> fail()
                }
            }
            is SocketEvent.Closed, SocketEvent.Failed -> if (state.value.busy) fail()
        }
    }
    private fun capture(token: Long) {
        val transport = socket ?: return
        val ownedLease = lease
        captureJob = scope.launch {
            try {
                withContext(io) {
                    recorder.record(onStarted = { scope.launch { if (owns(token))
                        mutableState.value = state.value.copy(phase = ChatPhase.RECORDING) } },
                        onPcm = { owns(token) && transport.sendBinary(it) })
                }
                if (owns(token)) {
                    mutableState.value = state.value.copy(phase = ChatPhase.TRANSCRIBING)
                    submittedVoice = true
                    if (!transport.sendText(AssistantClientMessages.questionStop())) fail()
                }
            } catch (cancel: CancellationException) { throw cancel }
            catch (_: Exception) { if (owns(token)) fail() }
        }
        captureJob?.invokeOnCompletion { ownedLease?.release() }
    }
    private fun complete(turn: ChatTurn) {
        pending.remove(turn.conversationId)
        val updated = state.value.selected!!.copy(lastActivityAt = turn.assistant.createdAt)
        mutableState.value = state.value.copy(selected = updated, conversations =
            (listOf(updated) + state.value.conversations.filterNot { it.conversationId == updated.conversationId }),
            messages = (state.value.messages + listOf(turn.user, turn.assistant)).distinctBy { it.messageId }.sortedBy { it.sequence },
            phase = ChatPhase.IDLE, query = "", error = null, pendingRequestId = null, latestAnswer = turn.assistant)
    }
    private fun discardUnsubmittedVoice() {
        voiceRequest?.takeIf { !submittedVoice && !replayOnly }?.let {
            if (pending[it.conversationId]?.requestId == it.requestId) pending.remove(it.conversationId)
        }
    }
    private fun fail() {
        discardUnsubmittedVoice(); invalidate()
        mutableState.value = state.value.copy(phase = ChatPhase.ERROR, loadingHistory = false,
            pendingRequestId = state.value.selected?.let { pending[it.conversationId]?.requestId },
            error = "Nie udało się zakończyć pytania. Sprawdź połączenie. PONÓW zachowa identyfikator żądania.")
    }
    private fun owns(token: Long) = !closed && generation == token
    private fun retireSocket() {
        generation++; timeoutJob?.cancel(); socket?.close(); socket = null
        if (captureJob?.isCompleted != false) lease?.release()
    }
    private fun invalidate(): Long {
        generation++; requestJob?.cancel(); historyJob?.cancel(); timeoutJob?.cancel()
        socket?.cancel(); socket = null; recorder.requestStop(); captureJob?.cancel()
        if (captureJob?.isCompleted != false) lease?.release()
        voiceRequest = null
        return generation
    }
    fun shutdown() {
        if (closed) return
        closed = true; invalidate(); listGeneration++; listJob?.cancel(); events.close(); eventJob.cancel(); pending.clear()
    }
}

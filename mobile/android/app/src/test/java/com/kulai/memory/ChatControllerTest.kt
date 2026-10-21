package com.kulai.memory

import com.kulai.memory.audio.*
import com.kulai.memory.models.*
import com.kulai.memory.transport.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.*
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.util.UUID
import kotlin.coroutines.Continuation
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine

@OptIn(ExperimentalCoroutinesApi::class)
class ChatControllerTest {
    private class Fixture(scope: CoroutineScope, dispatcher: CoroutineDispatcher, val api: FakeConversationApi = FakeConversationApi()) {
        val gate = MicrophoneGate(); val sockets = QuestionSockets(); val recorder = QuestionRecorder()
        val chat = ChatController(scope, api, "ws://127.0.0.1/memory/ws/assistant", sockets, recorder, gate, dispatcher)
    }
    private fun TestScope.fixture(api: FakeConversationApi = FakeConversationApi()) =
        Fixture(backgroundScope, StandardTestDispatcher(testScheduler), api).also { it.chat.newConversation(); runCurrent() }
    private fun TestScope.voice(f: Fixture): QuestionSocket {
        f.chat.setMicrophonePermission(true); f.chat.startVoice(); runCurrent()
        return f.sockets.connections.last().also {
            it.emit(SocketEvent.Opened); it.emit(assistantEvent("session.ready", 1)); runCurrent()
            assertEquals(ChatPhase.RECORDING, f.chat.state.value.phase)
            f.chat.stopVoice(); runCurrent()
        }
    }
    private fun TestScope.transcript(socket: QuestionSocket, text: String = "A jak to promować?") {
        socket.emit(assistantEvent("transcript.final", 2, JSONObject().put("text", text).toString()))
        socket.emit(assistantEvent("rag.started", 3)); runCurrent()
    }
    @Test fun `new process controller loads existing server conversation and resumes without microphone`() = runTest {
        val first = fixture(); val id = first.chat.state.value.selected!!.conversationId
        first.chat.setQuery("Projekt Latarnia kosztuje 12 zł."); first.chat.sendText(); runCurrent()
        assertEquals(2, first.chat.state.value.messages.size)
        first.chat.shutdown()
        val second = Fixture(backgroundScope, StandardTestDispatcher(testScheduler), first.api)
        second.chat.refreshIfNeeded(); runCurrent(); assertEquals(id, second.chat.state.value.conversations.single().conversationId)
        second.chat.selectConversation(second.chat.state.value.conversations.single()); runCurrent()
        assertEquals(first.api.turns.single().assistant, second.chat.state.value.messages.last())
        assertNull(second.chat.state.value.latestAnswer) // Opening history never auto speaks an old answer.
        second.chat.setQuery("A ile na 10 sztukach?"); second.chat.sendText(); runCurrent()
        assertEquals(listOf(1L, 2L, 3L, 4L), second.chat.state.value.messages.map { it.sequence })
        assertNotEquals(first.api.requests[0].first, first.api.requests[1].first)
    }
    @Test fun `untitled list topics come from only first questions and survive a new controller`() = runTest {
        val api = FakeConversationApi()
        val polish = conversation(); val english = conversation(); val titled = conversation().copy(title = "Existing title")
        api.conversations += listOf(polish, english, titled)
        api.turns += chatTurn(polish.conversationId, UUID.randomUUID(), "  Warsztaty\nceramiki  ", ChatInputKind.VOICE)
        for (number in 2L..40L) api.turns += chatTurn(polish.conversationId, UUID.randomUUID(), "A co dalej?", number = number)
        api.turns += chatTurn(english.conversationId, UUID.randomUUID(), "Weekend pottery promotion")
        val first = Fixture(backgroundScope, StandardTestDispatcher(testScheduler), api)
        first.chat.refreshIfNeeded(); runCurrent()
        assertEquals(listOf("Warsztaty ceramiki", "Weekend pottery promotion", "Existing title"),
            first.chat.state.value.conversations.map { it.title })
        assertEquals(listOf(Triple(polish.conversationId, 1, 2L), Triple(english.conversationId, 1, 2L)), api.messageRequests)
        assertTrue(first.chat.state.value.messages.isEmpty())
        first.chat.refreshConversations(); runCurrent()
        assertEquals(2, api.messageRequests.size) // Keep resolved labels for this controller's lifetime.
        assertNull(api.conversations.first().title) // Presentation does not write server metadata.
        first.chat.shutdown()
        val second = Fixture(backgroundScope, StandardTestDispatcher(testScheduler), api)
        second.chat.refreshIfNeeded(); runCurrent()
        assertEquals(first.chat.state.value.conversations, second.chat.state.value.conversations)
        assertEquals(4, api.messageRequests.size)
        second.chat.shutdown()
    }
    @Test fun `first voice question names a conversation and later turns keep its topic`() = runTest {
        val f = fixture(); val socket = voice(f)
        transcript(socket, "Warsztaty ceramiki za 100 zł")
        val turn = chatTurn(f.chat.state.value.selected!!.conversationId, f.chat.state.value.pendingRequestId!!,
            "Warsztaty ceramiki za 100 zł", ChatInputKind.VOICE)
        f.api.turns += turn
        socket.emit(assistantEvent("conversation.completed", 4, chatTurnJson(turn))); runCurrent()
        assertEquals("Warsztaty ceramiki za 100 zł", f.chat.state.value.selected!!.title)
        f.chat.setQuery("Rozwiń drugi pomysł."); f.chat.sendText(); runCurrent()
        assertEquals("Warsztaty ceramiki za 100 zł", f.chat.state.value.selected!!.title)
        f.chat.showConversations(); runCurrent()
        assertEquals("Warsztaty ceramiki za 100 zł", f.chat.state.value.conversations.single().title)
        f.chat.shutdown()
    }
    @Test fun `topic previews cannot block the list or apply after cancellation`() = runTest {
        val api = FakeConversationApi(); val original = conversation(); val replacement = conversation().copy(title = "Replacement")
        api.conversations += original
        api.turns += chatTurn(original.conversationId, UUID.randomUUID(), "Old topic")
        val release = CompletableDeferred<Unit>()
        api.beforeMessages = { withContext(NonCancellable) { release.await() } }
        val f = Fixture(backgroundScope, StandardTestDispatcher(testScheduler), api)
        f.chat.refreshConversations(); runCurrent()
        assertTrue(f.chat.state.value.listLoaded); assertFalse(f.chat.state.value.loadingList)
        api.conversations.clear(); api.conversations += replacement
        f.chat.refreshConversations(); runCurrent(); release.complete(Unit); runCurrent()
        assertEquals(listOf(replacement), f.chat.state.value.conversations)
        f.chat.shutdown()
        api.conversations.clear(); api.conversations += original
        api.beforeMessages = { error("PRIVATE_PREVIEW_FAILURE") }
        val offline = Fixture(backgroundScope, StandardTestDispatcher(testScheduler), api)
        offline.chat.refreshConversations(); runCurrent()
        assertTrue(offline.chat.state.value.listLoaded); assertNull(offline.chat.state.value.error)
        assertEquals(listOf(original), offline.chat.state.value.conversations)
        offline.chat.shutdown()
    }
    @Test fun `first text question yields a bounded Unicode topic without changing content`() = runTest {
        val f = fixture(); val question = "  " + "🟢".repeat(90) + " PL and EN"
        f.chat.setQuery(question); f.chat.sendText(); runCurrent()
        val title = f.chat.state.value.selected!!.title!!
        assertEquals("🟢".repeat(80) + "…", title)
        assertEquals(question, f.api.turns.single().user.content)
        f.chat.shutdown()
    }
    @Test fun `uncertain commit retry keeps UUID and has no duplicate messages`() = runTest {
        val f = fixture()
        f.api.after = { error("PRIVATE RESPONSE LOST") }
        f.chat.setQuery("Pytanie"); f.chat.sendText(); runCurrent()
        assertEquals(ChatPhase.ERROR, f.chat.state.value.phase); assertTrue(f.chat.state.value.canRetry)
        val request = f.chat.state.value.pendingRequestId
        assertFalse(f.chat.state.value.error!!.contains("PRIVATE"))
        f.chat.retry(); runCurrent()
        assertEquals(1, f.api.turns.size); assertEquals(listOf(request, request), f.api.requests.map { it.first })
        assertEquals(2, f.chat.state.value.messages.size); assertFalse(f.chat.state.value.canRetry)
    }
    @Test fun `switching conversations cancels work and stale completion cannot replace selected history`() = runTest {
        val f = fixture(); val first = f.chat.state.value.selected!!
        val other = f.api.createConversation(); val release = CompletableDeferred<Unit>()
        f.api.before = { withContext(NonCancellable) {
            release.await()
            f.api.turns += chatTurn(first.conversationId, f.api.requests.last().first, "Pierwsza rozmowa")
        } }
        f.chat.setQuery("Pierwsza rozmowa"); f.chat.sendText(); runCurrent()
        val request = f.chat.state.value.pendingRequestId
        f.chat.selectConversation(other); runCurrent(); release.complete(Unit); runCurrent()
        assertEquals(other, f.chat.state.value.selected); assertTrue(f.chat.state.value.messages.isEmpty())
        f.chat.selectConversation(first); runCurrent()
        assertEquals(2, f.chat.state.value.messages.size); assertNull(f.chat.state.value.pendingRequestId)
        assertEquals(request, f.chat.state.value.messages.first().requestId)
    }
    @Test fun `text then voice then text share conversation and chronological messages`() = runTest {
        val f = fixture(); val id = f.chat.state.value.selected!!.conversationId
        f.chat.setQuery("Tworzymy notesy A5."); f.chat.sendText(); runCurrent()
        val socket = voice(f)
        val request = UUID.fromString(JSONObject(socket.text.first()).getString("request_id"))
        assertEquals(id.toString(), JSONObject(socket.text.first()).getString("conversation_id"))
        transcript(socket)
        val turn = chatTurn(id, request, "A jak to promować?", ChatInputKind.VOICE, 2)
        f.api.turns += turn
        socket.emit(assistantEvent("conversation.completed", 4, chatTurnJson(turn))); runCurrent()
        f.chat.setQuery("Rozwiń poprzednią odpowiedź."); f.chat.sendText(); runCurrent()
        assertEquals((1L..6L).toList(), f.chat.state.value.messages.map { it.sequence })
        assertEquals(listOf(ChatInputKind.TEXT, ChatInputKind.VOICE, ChatInputKind.TEXT),
            f.chat.state.value.messages.filter { it.role == ChatRole.USER }.map { it.inputKind })
        assertNull(f.gate.owner.value); assertEquals(1, f.recorder.requests)
    }
    @Test fun `voice retry after transcript uses same UUID and voice kind over HTTP without audio`() = runTest {
        val f = fixture(); val socket = voice(f); transcript(socket)
        val request = f.chat.state.value.pendingRequestId
        socket.emit(SocketEvent.Failed); runCurrent(); assertTrue(f.chat.state.value.canRetry)
        f.chat.retry(); runCurrent()
        assertEquals(request to ChatInputKind.VOICE, f.api.requests.single())
        assertEquals(1, f.recorder.requests); assertEquals(1, f.sockets.connections.size)
        assertEquals(ChatPhase.IDLE, f.chat.state.value.phase)
    }
    @Test fun `voice retry without received transcript replays same UUID without microphone`() = runTest {
        val f = fixture(); val original = voice(f)
        val id = f.chat.state.value.selected!!.conversationId; val request = f.chat.state.value.pendingRequestId!!
        original.emit(SocketEvent.Failed); runCurrent()
        f.chat.setMicrophonePermission(false); f.chat.retry(); runCurrent()
        val replay = f.sockets.connections.last()
        replay.emit(SocketEvent.Opened); replay.emit(assistantEvent("session.ready", 1)); runCurrent()
        assertEquals(request.toString(), JSONObject(replay.text.first()).getString("request_id"))
        assertEquals("question.stop", JSONObject(replay.text.last()).getString("type"))
        assertTrue(replay.pcm.isEmpty()); assertEquals(1, f.recorder.requests)
        transcript(replay)
        replay.emit(assistantEvent("conversation.completed", 4, chatTurnJson(chatTurn(id, request, "A jak to promować?", ChatInputKind.VOICE), false)))
        runCurrent(); assertEquals(2, f.chat.state.value.messages.size); assertFalse(f.chat.state.value.canRetry)
    }
    @Test fun `shared microphone lease prevents NOTE ASK and CHAT overlap and drains on cancel`() = runTest {
        val f = fixture(); f.chat.setMicrophonePermission(true)
        val note = f.gate.acquire(CaptureOwner.NOTE)!!
        f.chat.startVoice(); runCurrent(); assertTrue(f.sockets.connections.isEmpty())
        note.release(); val socket = voice(f)
        assertTrue(socket.pcm.isNotEmpty())
        f.chat.cancel(); runCurrent(); assertNull(f.gate.owner.value)
        assertTrue(socket.cancelled); assertNotNull(f.chat.state.value.pendingRequestId) // Submission may already have committed.
    }
    @Test fun `empty transcript malformed sequence and foreign conversation cannot append fake turns`() = runTest {
        for (failure in listOf("empty", "sequence", "foreign")) {
            val f = fixture(); val socket = voice(f)
            when (failure) {
                "empty" -> socket.emit(assistantEvent("transcript.final", 2, """{"text":" "}"""))
                "sequence" -> socket.emit(assistantEvent("rag.started", 4))
                else -> {
                    transcript(socket)
                    socket.emit(assistantEvent("conversation.completed", 4, chatTurnJson(chatTurn(UUID.randomUUID(), f.chat.state.value.pendingRequestId!!))))
                }
            }
            runCurrent(); assertTrue(f.chat.state.value.messages.isEmpty()); assertNull(f.gate.owner.value)
            assertEquals(if (failure == "empty") ChatPhase.NO_SPEECH else ChatPhase.ERROR, f.chat.state.value.phase)
            f.chat.shutdown()
        }
    }
    @Test fun `switch during active capture drains native recorder before releasing shared microphone`() = runTest {
        val api = FakeConversationApi(); val gate = MicrophoneGate(); val sockets = QuestionSockets()
        var drain: Continuation<RecordingEnd>? = null
        val recorder = object : VoiceAudioRecorder {
            override suspend fun record(onStarted: () -> Unit, onPcm: suspend (ByteArray) -> Boolean): RecordingEnd {
                onStarted(); return suspendCoroutine { drain = it }
            }
            override fun requestStop() { }
        }
        val chat = ChatController(backgroundScope, api, "ws://127.0.0.1/ws/assistant", sockets, recorder,
            gate, StandardTestDispatcher(testScheduler))
        chat.newConversation(); runCurrent(); chat.setMicrophonePermission(true); chat.startVoice()
        val socket = sockets.connections.single()
        socket.emit(SocketEvent.Opened); socket.emit(assistantEvent("session.ready", 1)); runCurrent()
        assertEquals(ChatPhase.RECORDING, chat.state.value.phase)
        val second = api.createConversation(); chat.selectConversation(second); runCurrent()
        assertEquals(CaptureOwner.QUESTION, gate.owner.value); assertNull(gate.acquire(CaptureOwner.NOTE))
        assertTrue(socket.cancelled); assertNull(chat.state.value.pendingRequestId)
        drain!!.resume(RecordingEnd.STOPPED); runCurrent()
        assertNull(gate.owner.value); assertEquals(second, chat.state.value.selected)
        assertTrue(chat.state.value.messages.isEmpty()); assertEquals(1, socket.text.size)
        chat.shutdown()
    }
}

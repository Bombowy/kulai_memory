package com.kulai.memory

import com.kulai.memory.audio.MicrophoneGate
import com.kulai.memory.models.*
import com.kulai.memory.transport.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.first
import okhttp3.OkHttpClient
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.util.UUID

/** Opt in via the existing owned-PostgreSQL Python fixture; never points at a user's server. */
class ChatPostgresBackendTest {
    @Test fun `actual Android client text voice restart and retry against SECURE FastAPI PostgreSQL 0005`() = runBlocking {
        val origin = System.getenv("KULAI_ANDROID_CHAT_TEST_ORIGIN")
        assumeTrue("Run backend owned PostgreSQL Android integration fixture.", origin != null)
        require(origin!!.startsWith("http://127.0.0.1:") && System.getenv("KULAI_ANDROID_CHAT_TEST_OWNED") == "1")
        val credential = DeviceCredential(UUID.fromString(System.getenv("KULAI_ANDROID_CHAT_TEST_DEVICE")),
            System.getenv("KULAI_ANDROID_CHAT_TEST_BEARER"), origin)
        val auth = RequestAuthentication(origin, credentials = CredentialProvider { credential })
        val client = OkHttpClient(); val api = MemoryHttpClient(origin, client, auth)
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        fun controller() = ChatController(scope, api, origin.replaceFirst("http", "ws") + "/ws/assistant",
            OkHttpVoiceSocketFactory(client, auth), QuestionRecorder(), MicrophoneGate())
        var chat = controller()
        suspend fun await(count: Int) = withTimeout(10000) { chat.state.first { !it.busy && !it.loadingHistory && it.messages.size == count } }
        try {
            chat.newConversation()
            withTimeout(10000) { chat.state.first { it.selected != null && !it.loadingHistory } }
            val id = chat.state.value.selected!!.conversationId
            chat.setQuery("Projekt testowy: notesy A5 po 30 zł. EN: notebooks."); chat.sendText(); await(2)
            val first = chat.state.value.messages.first()
            chat.setMicrophonePermission(true); chat.startVoice()
            withTimeout(10000) { chat.state.first { it.phase == ChatPhase.RECORDING } }
            chat.stopVoice(); await(4)
            assertEquals(ChatInputKind.VOICE, chat.state.value.messages[2].inputKind)
            assertTrue(chat.state.value.messages.all { it.conversationId == id })
            chat.shutdown() // Discard all controller/UI history, retain only the paired HTTP boundary.
            client.connectionPool.evictAll()
            chat = controller(); chat.refreshIfNeeded()
            withTimeout(10000) { chat.state.first { it.listLoaded } }
            chat.selectConversation(chat.state.value.conversations.single { it.conversationId == id }); await(4)
            assertNull(chat.state.value.latestAnswer)
            chat.setQuery("Rozwiń poprzednią odpowiedź."); chat.sendText(); await(6)
            val replay = api.askConversation(id, first.requestId, first.content)
            assertFalse(replay.created)
            chat.refreshMessages(); await(6)
            assertEquals((1L..6L).toList(), chat.state.value.messages.map { it.sequence })
            assertEquals(6, chat.state.value.messages.map { it.messageId }.distinct().size)
            assertTrue(chat.state.value.messages.filter { it.role == ChatRole.ASSISTANT }.all { it.memoryIds.isEmpty() })
            chat.newConversation()
            withTimeout(10000) { chat.state.first { it.selected != null && it.selected.conversationId != id && !it.loadingHistory } }
            assertTrue(chat.state.value.messages.isEmpty())
        } finally {
            chat.shutdown(); scope.cancel(); client.dispatcher.cancelAll(); client.connectionPool.evictAll()
            client.dispatcher.executorService.shutdown()
        }
    }
}

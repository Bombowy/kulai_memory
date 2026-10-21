package com.kulai.memory

import com.kulai.memory.audio.*
import com.kulai.memory.cache.PendingAudioCacheStore
import com.kulai.memory.models.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.*
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.util.UUID

@OptIn(ExperimentalCoroutinesApi::class)
class ChatSpeechTest {
    @get:Rule val folder = TemporaryFolder()
    @Test fun `CHAT and ASK share one TTS auto switch stops audio and never reads metadata`() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        val api = FakeMemoryApi(); val conversations = FakeConversationApi(); val gate = MicrophoneGate()
        val engine = FakeLocalSpeechEngine(); val speech = MemorySpeechController(backgroundScope, api, engine)
        val note = VoiceMemoryController(backgroundScope, "ws://127.0.0.1/ws/memory", QuestionSockets(), QuestionRecorder(),
            PendingAudioCacheStore(folder.root), dispatcher, microphoneGate = gate)
        val assistant = MemoryAssistantController(backgroundScope, api, "ws://127.0.0.1/ws/assistant", QuestionSockets(), QuestionRecorder(), gate, dispatcher)
        val chat = ChatController(backgroundScope, conversations, "ws://127.0.0.1/ws/assistant", QuestionSockets(), QuestionRecorder(), gate, dispatcher)
        val root = MemoryClientController(backgroundScope, note, assistant, MemoryHistoryController(backgroundScope, api), gate, speech, chat)
        speech.startup(); root.selectTab(MemoryTab.CHAT); chat.newConversation(); runCurrent()
        val memoryId = UUID.randomUUID()
        val prose = "Cena: 79 zł. This is the price."
        conversations.before = {
            if (conversations.turns.isEmpty()) {
                val turn = chatTurn(chat.state.value.selected!!.conversationId, conversations.requests.last().first,
                    answer = "Cena: 79 zł [memory_id: $memoryId]. This is the price.")
                conversations.turns += turn.copy(assistant = turn.assistant.copy(memoryIds = listOf(memoryId)))
            }
        }
        chat.setQuery("Synthetic question"); root.sendChat(); runCurrent()
        val answer = chat.state.value.latestAnswer!!
        root.chatAnswerRendered(answer); root.chatAnswerRendered(answer); runCurrent()
        assertEquals(listOf(prose), api.speechRequests); assertEquals(prose, engine.spoken.single().text)
        assertTrue(answer.content.contains("memory_id")); assertEquals(listOf(memoryId), answer.memoryIds)
        root.selectTab(MemoryTab.ASK); assistant.setQuery("V1 question"); root.ask(); runCurrent()
        root.answerRendered(assistant.state.value.answerVersion); runCurrent()
        assertEquals(2, api.speechRequests.size) // CHAT's UUID does not consume ASK's numeric answer version.
        root.selectTab(MemoryTab.CHAT); root.chatAnswerRendered(answer); runCurrent(); assertEquals(2, api.speechRequests.size)
        speech.setAutoSpeak(false); chat.setQuery("Next question"); root.sendChat(); runCurrent()
        root.chatAnswerRendered(chat.state.value.latestAnswer!!); runCurrent(); assertEquals(2, api.speechRequests.size)
        val referenced = chat.state.value.latestAnswer!!.copy(content = "Delivery is free. (memory_id: $memoryId)", memoryIds = listOf(memoryId))
        root.speakChat(referenced); runCurrent(); assertEquals("Delivery is free.", api.speechRequests.last())
        assertFalse(api.speechRequests.last().contains(referenced.memoryIds.single().toString()))
        val requestsBeforeEmpty = api.speechRequests.size
        root.speakChat(referenced.copy(content = "[memory_id: $memoryId]")); runCurrent()
        assertEquals(requestsBeforeEmpty, api.speechRequests.size); assertEquals(SpeechPhase.READY, speech.state.value.phase)
        engine.holdSpeech = true; root.speakChat(referenced); runCurrent(); assertEquals(SpeechPhase.SPEAKING, speech.state.value.phase)
        root.stopAudio(); runCurrent(); assertEquals(SpeechPhase.READY, speech.state.value.phase)
        root.selectTab(MemoryTab.MEMORY); runCurrent(); root.selectTab(MemoryTab.CHAT); runCurrent()
        assertEquals(4, chat.state.value.messages.size); assertEquals(1, engine.initializations)
        root.shutdown(); runCurrent(); assertEquals(1, engine.shutdowns)
    }

    @Test fun `inline Memory metadata is silent while PL EN prose values and stored references survive`() {
        val id = UUID.randomUUID(); val other = UUID.randomUUID()
        val message = chatTurn(UUID.randomUUID(), UUID.randomUUID()).assistant.copy(memoryIds = listOf(id, other))
        val cases = listOf(
            "Cena: 79 zł [memory_id: $id]. Dostawa: 12 zł." to "Cena: 79 zł. Dostawa: 12 zł.",
            "Price: 79 PLN. (MEMORY_ID=$id)" to "Price: 79 PLN.",
            "Cena: 79 zł. (**memory_id**: **$id**)" to "Cena: 79 zł.",
            "Answer. `memory_id`: `$id`" to "Answer.",
            "Answer. {\"used_memory_ids\":[\"$id\",\"$other\"]}" to "Answer.",
            "Answer. [memory_id]($id)" to "Answer.",
            "Answer. [$id]" to "Answer.",
            "Answer. $id" to "Answer.",
            "[memory_id: $id]" to "",
            "Zysk: 180 zł.\nBudget: 300 PLN." to "Zysk: 180 zł.\nBudget: 300 PLN.",
            "Pole memory_id identyfikuje Memory." to "Pole memory_id identyfikuje Memory.",
            "  Odpowiedź PL.\n\nEnglish  answer.  " to "  Odpowiedź PL.\n\nEnglish  answer.  ",
        )
        for ((raw, spoken) in cases) {
            val original = message.copy(content = raw)
            assertEquals(spoken, original.speechAnswer().answer)
            assertEquals(raw, original.content); assertEquals(listOf(id, other), original.memoryIds)
            assertTrue(original.speechAnswer().citations.isEmpty())
        }
        assertEquals("Answer.", message.copy(content = "Answer. (memory_id: ${UUID.randomUUID()})",
            memoryIds = emptyList()).speechAnswer().answer)
    }
}

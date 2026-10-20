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
        chat.setQuery("Synthetic question"); root.sendChat(); runCurrent()
        val answer = chat.state.value.latestAnswer!!
        root.chatAnswerRendered(answer); root.chatAnswerRendered(answer); runCurrent()
        assertEquals(listOf(answer.content), api.speechRequests); assertEquals(answer.content, engine.spoken.single().text)
        root.selectTab(MemoryTab.ASK); assistant.setQuery("V1 question"); root.ask(); runCurrent()
        root.answerRendered(assistant.state.value.answerVersion); runCurrent()
        assertEquals(2, api.speechRequests.size) // CHAT's UUID does not consume ASK's numeric answer version.
        root.selectTab(MemoryTab.CHAT); root.chatAnswerRendered(answer); runCurrent(); assertEquals(2, api.speechRequests.size)
        speech.setAutoSpeak(false); chat.setQuery("Next question"); root.sendChat(); runCurrent()
        root.chatAnswerRendered(chat.state.value.latestAnswer!!); runCurrent(); assertEquals(2, api.speechRequests.size)
        val referenced = chat.state.value.latestAnswer!!.copy(memoryIds = listOf(UUID.randomUUID()))
        root.speakChat(referenced); runCurrent(); assertEquals(referenced.content, api.speechRequests.last())
        assertFalse(api.speechRequests.last().contains(referenced.memoryIds.single().toString()))
        engine.holdSpeech = true; root.speakChat(referenced); runCurrent(); assertEquals(SpeechPhase.SPEAKING, speech.state.value.phase)
        root.stopAudio(); runCurrent(); assertEquals(SpeechPhase.READY, speech.state.value.phase)
        root.selectTab(MemoryTab.MEMORY); runCurrent(); root.selectTab(MemoryTab.CHAT); runCurrent()
        assertEquals(4, chat.state.value.messages.size); assertEquals(1, engine.initializations)
        root.shutdown(); runCurrent(); assertEquals(1, engine.shutdowns)
    }
}

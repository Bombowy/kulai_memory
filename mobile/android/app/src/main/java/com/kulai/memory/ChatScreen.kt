package com.kulai.memory

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.kulai.memory.models.ChatRole
import java.time.format.DateTimeFormatter

@Composable
internal fun ChatContent(chat: ChatController, root: MemoryClientController, speech: MemorySpeechState,
    microphoneFree: Boolean, onPermission: () -> Unit) {
    val state by chat.state.collectAsStateWithLifecycle()
    val scroll = rememberLazyListState()
    LaunchedEffect(state.selected?.conversationId, state.messages.lastOrNull()?.messageId) {
        if (state.messages.isNotEmpty()) scroll.animateScrollToItem(state.messages.lastIndex +
            if (state.nextBeforeSequence != null) 1 else 0)
    }
    LaunchedEffect(state.latestAnswer?.messageId) { state.latestAnswer?.let(root::chatAnswerRendered) }
    Column(Modifier.fillMaxSize().imePadding().padding(horizontal = 16.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)) {
        if (state.selected == null) {
            Text("CHAT", style = MaterialTheme.typography.headlineMedium)
            Text("Twoje rozmowy · tekst i głos", style = MaterialTheme.typography.bodyMedium)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = root::newChat, enabled = !state.busy) { Text("NOWA ROZMOWA") }
                OutlinedButton(onClick = { chat.refreshConversations() }, enabled = !state.loadingList) { Text("ODŚWIEŻ") }
            }
            state.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            if (state.loadingList || state.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            LazyColumn(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                items(state.conversations, key = { it.conversationId.toString() }) { conversation ->
                    Card(Modifier.fillMaxWidth().clickable(enabled = !state.busy) { root.selectChat(conversation) }) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                            Text(conversation.title ?: "Nowa rozmowa", style = MaterialTheme.typography.titleMedium,
                                maxLines = 2, overflow = TextOverflow.Ellipsis)
                            Text(conversation.lastActivityAt.atZoneSameInstant(java.time.ZoneId.systemDefault())
                                .format(DateTimeFormatter.ofPattern("dd.MM.yyyy · HH:mm")), style = MaterialTheme.typography.bodySmall)
                        }
                    }
                }
                if (state.listLoaded && state.conversations.isEmpty()) item {
                    Text("Zacznij nową rozmowę. Historia będzie dostępna także po ponownym otwarciu aplikacji.")
                }
                if (state.nextCursor != null) item {
                    OutlinedButton(onClick = { chat.refreshConversations(more = true) }, enabled = !state.loadingList) { Text("WIĘCEJ ROZMÓW") }
                }
            }
        } else {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically) {
                TextButton(onClick = root::chatList) { Text("‹ ROZMOWY") }
                Text("CHAT", style = MaterialTheme.typography.titleLarge)
                TextButton(onClick = chat::refreshMessages, enabled = !state.busy && !state.loadingHistory) { Text("ODŚWIEŻ") }
            }
            LazyColumn(Modifier.weight(1f).fillMaxWidth(), state = scroll,
                verticalArrangement = Arrangement.spacedBy(10.dp), contentPadding = PaddingValues(vertical = 8.dp)) {
                if (state.nextBeforeSequence != null) item {
                    TextButton(onClick = chat::olderMessages, enabled = !state.loadingHistory && !state.busy) { Text("WCZEŚNIEJSZE WIADOMOŚCI") }
                }
                items(state.messages, key = { it.messageId.toString() }) { message ->
                    val user = message.role == ChatRole.USER
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = if (user) Arrangement.End else Arrangement.Start) {
                        Surface(color = if (user) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surfaceContainerHigh,
                            shape = MaterialTheme.shapes.large, modifier = Modifier.fillMaxWidth(0.94f)) {
                            Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                                Text(if (user) "TY · ${if (message.inputKind == com.kulai.memory.models.ChatInputKind.VOICE) "głos" else "tekst"}" else "KulAI",
                                    style = MaterialTheme.typography.labelMedium)
                                SelectionContainer { Text(message.content, style = MaterialTheme.typography.bodyLarge) }
                                if (!user) {
                                    if (message.memoryIds.isNotEmpty()) Text("Źródła Memories: ${message.memoryIds.size}", style = MaterialTheme.typography.bodySmall)
                                    TextButton(onClick = { root.speakChat(message) }, enabled = speech.available && !speech.busy &&
                                        !state.busy && microphoneFree && message.content.length <= 6000) { Text("SPEAK") }
                                }
                            }
                        }
                    }
                }
                if (state.messages.isEmpty() && !state.loadingHistory) item { Text("O czym porozmawiamy?") }
            }
            if (state.loadingHistory || state.busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            Text(when {
                state.loadingHistory -> "Ładowanie historii…"
                else -> when (state.phase) {
                    ChatPhase.IDLE -> "Gotowe"
                    ChatPhase.CREATING -> "Tworzenie rozmowy…"
                    ChatPhase.CONNECTING -> "Łączenie…"
                    ChatPhase.RECORDING -> "Nagrywanie — dotknij STOP, aby wysłać"
                    ChatPhase.TRANSCRIBING -> "Transkrypcja…"
                    ChatPhase.GENERATING -> "KulAI przygotowuje odpowiedź…"
                    ChatPhase.NO_SPEECH -> "Nie wykryto mowy"
                    ChatPhase.ERROR -> "Nie udało się zakończyć pytania"
                }
            }, style = MaterialTheme.typography.bodySmall)
            state.error?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
            OutlinedTextField(value = state.query, onValueChange = chat::setQuery, modifier = Modifier.fillMaxWidth(),
                label = { Text("Napisz wiadomość") }, maxLines = 4, enabled = !state.busy && !state.loadingHistory,
                isError = state.query.length > 10000)
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = root::sendChat, enabled = state.canSend) { Text("WYŚLIJ") }
                OutlinedButton(onClick = if (state.phase == ChatPhase.RECORDING) chat::stopVoice else if (!state.permissionGranted) onPermission else root::startChatVoice,
                    enabled = state.phase == ChatPhase.RECORDING || !state.busy && !state.loadingHistory && microphoneFree) {
                    Text(if (state.phase == ChatPhase.RECORDING) "STOP 🎙" else "MIKROFON")
                }
                if (state.busy) TextButton(onClick = chat::cancel) { Text("ANULUJ") }
                else if (state.canRetry) TextButton(onClick = root::retryChat) { Text("PONÓW") }
            }
        }
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            Checkbox(checked = speech.autoSpeak, onCheckedChange = { root.speech?.setAutoSpeak(it) })
            Text("Czytaj odpowiedzi", style = MaterialTheme.typography.bodySmall, modifier = Modifier.weight(1f))
            TextButton(onClick = root::stopAudio, enabled = speech.busy) { Text("STOP AUDIO") }
        }
        if (!speech.available) Text("TTS PL/EN niedostępne", style = MaterialTheme.typography.bodySmall)
        else if (speech.phase in setOf(SpeechPhase.PLANNING, SpeechPhase.SPEAKING, SpeechPhase.ERROR)) Text(
            when (speech.phase) {
                SpeechPhase.PLANNING -> "Przygotowanie audio…"
                SpeechPhase.SPEAKING -> "Odczytywanie PL/EN…"
                else -> "Nie udało się odczytać odpowiedzi. Możesz ponowić SPEAK."
            }, style = MaterialTheme.typography.bodySmall,
            color = if (speech.phase == SpeechPhase.ERROR) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface)
    }
}

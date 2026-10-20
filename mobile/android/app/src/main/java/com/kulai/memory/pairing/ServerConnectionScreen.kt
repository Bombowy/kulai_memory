package com.kulai.memory.pairing

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle

@Composable
fun ServerConnectionScreen(controller: PairingController, allowLocalDev: Boolean, onBack: () -> Unit) {
    val state by controller.state.collectAsStateWithLifecycle()
    var origin by remember(state.origin) { mutableStateOf(state.origin) }
    var localDev by remember(state.localDev) { mutableStateOf(state.localDev) }
    // PL/EN: invite stays only in composition memory, never saved in an Android state Bundle.
    var session by remember { mutableStateOf("") }
    var invitation by remember { mutableStateOf("") }
    var name by remember { mutableStateOf("Android") }
    Column(Modifier.fillMaxSize().systemBarsPadding().imePadding().verticalScroll(rememberScrollState()).padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("Server Connection", style = MaterialTheme.typography.headlineMedium)
        Text(if (state.localDev) "LOCAL_DEV (loopback)" else when (state.status) {
            ConnectionStatus.NOT_PAIRED -> "Not paired"
            ConnectionStatus.PAIRING -> "Pairing — waiting for administrator approval"
            ConnectionStatus.PAIRED -> "Paired"
            ConnectionStatus.EXPIRED -> "Expired — pair again"
            ConnectionStatus.REVOKED -> "Revoked — pair again"
            ConnectionStatus.UNAVAILABLE -> "Unavailable"
        })
        state.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        OutlinedTextField(origin, { origin = it }, label = { Text("Server URL (HTTPS or local loopback)") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        if (allowLocalDev) Row {
            Switch(localDev, { localDev = it })
            Text("LOCAL_DEV via ADB reverse", modifier = Modifier.padding(top = 12.dp))
        }
        Button(onClick = { controller.configure(origin, localDev) }) { Text("APPLY SERVER") }
        if (!state.localDev && state.status != ConnectionStatus.PAIRED && state.status != ConnectionStatus.PAIRING) {
            Text("Pair Device", style = MaterialTheme.typography.titleLarge)
            OutlinedTextField(session, { session = it }, label = { Text("Invitation session UUID") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(invitation, { invitation = it }, label = { Text("One-time invitation secret") },
                visualTransformation = PasswordVisualTransformation(), keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(name, { name = it }, label = { Text("Device name (letters/numbers)") }, singleLine = true)
            Button(onClick = { controller.begin(session.trim(), invitation.trim(), name); invitation = "" }, enabled = session.isNotBlank() && invitation.isNotBlank()) { Text("PAIR DEVICE") }
        }
        state.sessionId?.let { Text("Session: $it") }
        state.deviceId?.let { Text("Device: $it") }
        state.verificationCode?.let { Text("Compare this code with the administrator before approval:\n$it") }
        OutlinedButton(onClick = controller::refresh) { Text("CHECK / RETRY") }
        OutlinedButton(onClick = controller::forget) { Text(if (state.status == ConnectionStatus.PAIRING) "CANCEL PAIRING" else "UNPAIR / REMOVE LOCAL CREDENTIAL") }
        if (state.privateAccess) Button(onClick = onBack) { Text("BACK TO MEMORY") }
    }
}

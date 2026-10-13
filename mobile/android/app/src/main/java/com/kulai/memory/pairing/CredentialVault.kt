package com.kulai.memory.pairing

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import org.json.JSONObject
import java.io.File
import java.security.KeyStore
import java.time.Instant
import java.util.UUID
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

interface VaultFile { fun read(): ByteArray?; fun write(bytes: ByteArray); fun delete() }
interface SecretCipher { fun encrypt(bytes: ByteArray): ByteArray; fun decrypt(bytes: ByteArray): ByteArray }
interface ConnectionStore { fun read(): ConnectionRecord?; fun write(record: ConnectionRecord); fun clear() }

/** These objects intentionally have no generated toString/component methods with secrets. */
class PendingPairing(val sessionId: UUID, val receiverSecret: String, val invitationSecret: String,
    val clientId: UUID, val label: String, val claiming: Boolean = false, val registered: Boolean = false) {
    override fun toString() = "PendingPairing([redacted])"
}
class StoredCredential(val deviceId: UUID, val tokenId: UUID, val token: String, val expiresAt: Instant) {
    override fun toString() = "StoredCredential([redacted])"
}
class ConnectionRecord(val origin: String, val localDev: Boolean, val pending: PendingPairing? = null,
    val credential: StoredCredential? = null) {
    override fun toString() = "ConnectionRecord([redacted])"
}

/** Atomic replacement of the entire encrypted record; snapshots publish only after durable write. */
class EncryptedConnectionStore(private val file: VaultFile, private val cipher: SecretCipher) : ConnectionStore {
    @Synchronized override fun read(): ConnectionRecord? = try {
        val bytes = file.read() ?: return null
        require(bytes.size <= 32768)
        val json = JSONObject(String(cipher.decrypt(bytes), Charsets.UTF_8))
        require(json.getInt("version") == 1)
        val pending = json.optJSONObject("pending")?.let { PendingPairing(UUID.fromString(it.getString("session_id")),
            it.getString("receiver"), it.getString("invitation"), UUID.fromString(it.getString("client_id")),
            it.getString("label"), it.getBoolean("claiming"), it.optBoolean("registered", false)) }
        val credential = json.optJSONObject("credential")?.let { StoredCredential(UUID.fromString(it.getString("device_id")),
            UUID.fromString(it.getString("token_id")), it.getString("token"), Instant.parse(it.getString("expires_at"))) }
        ConnectionRecord(json.getString("origin"), json.getBoolean("local_dev"), pending, credential)
    } catch (_: Exception) {
        // PL/EN: lost/invalid Keystore key or ciphertext means re-pair; no plaintext fallback.
        throw PairingException()
    }

    @Synchronized override fun write(record: ConnectionRecord) {
        try {
            val json = JSONObject().put("version", 1).put("origin", record.origin).put("local_dev", record.localDev)
            record.pending?.let { json.put("pending", JSONObject().put("session_id", it.sessionId.toString())
                .put("receiver", it.receiverSecret).put("invitation", it.invitationSecret)
                .put("client_id", it.clientId.toString()).put("label", it.label).put("claiming", it.claiming)
                .put("registered", it.registered)) }
            record.credential?.let { json.put("credential", JSONObject().put("device_id", it.deviceId.toString())
                .put("token_id", it.tokenId.toString()).put("token", it.token).put("expires_at", it.expiresAt.toString())) }
            file.write(cipher.encrypt(json.toString().toByteArray(Charsets.UTF_8)))
        } catch (_: Exception) { throw PairingException() }
    }
    @Synchronized override fun clear() { try { file.delete() } catch (_: Exception) { throw PairingException() } }
}

class AndroidVaultFile(context: Context) : VaultFile {
    private val atomic = AtomicFile(File(context.noBackupFilesDir, "server-connection.enc"))
    override fun read(): ByteArray? {
        if (!atomic.baseFile.exists() && !File(atomic.baseFile.path + ".bak").exists()) return null
        return atomic.openRead().use { input ->
            val output = java.io.ByteArrayOutputStream()
            val chunk = ByteArray(4096)
            while (true) {
                val count = input.read(chunk)
                if (count < 0) break
                require(output.size() + count <= 32768)
                output.write(chunk, 0, count)
            }
            output.toByteArray()
        }
    }
    override fun write(bytes: ByteArray) {
        var stream: java.io.FileOutputStream? = null
        try {
            stream = atomic.startWrite(); stream.write(bytes)
            stream.fd.sync() // PL/EN: propagate sync failures before publishing a credential.
            atomic.finishWrite(stream)
            // AtomicFile can log a rename failure instead of throwing; verify replacement.
            check(read()?.contentEquals(bytes) == true)
        } catch (error: Exception) { atomic.failWrite(stream); throw error }
    }
    override fun delete() = atomic.delete()
}

/** Standard AES-GCM; random IV from provider, authenticated version binding. */
class AesGcmCipher(private val key: () -> SecretKey) : SecretCipher {
    private val aad = "KulAI Memory connection v1".toByteArray(Charsets.UTF_8)
    override fun encrypt(bytes: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key()); cipher.updateAAD(aad)
        require(cipher.iv.size == 12)
        return byteArrayOf(1) + cipher.iv + cipher.doFinal(bytes)
    }
    override fun decrypt(bytes: ByteArray): ByteArray {
        require(bytes.size >= 29 && bytes[0] == 1.toByte())
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, bytes.copyOfRange(1, 13)))
        cipher.updateAAD(aad)
        return cipher.doFinal(bytes.copyOfRange(13, bytes.size))
    }
}

fun androidConnectionStore(context: Context): ConnectionStore {
    val alias = "kulai-memory-connection-v1"
    val key = {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey) ?: KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
            .apply { init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256).setRandomizedEncryptionRequired(true).build()) }.generateKey()
    }
    return EncryptedConnectionStore(AndroidVaultFile(context.applicationContext), AesGcmCipher(key))
}

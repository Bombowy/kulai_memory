package com.kulai.memory.cache

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.File

private const val OWNED_PREFIX = "note-"
private const val OWNED_SUFFIX = ".pcm"

class PendingAudioCache private constructor(
    private val root: File,
    val file: File,
    private var output: BufferedOutputStream?,
) {
    val size: Long
        get() = file.length()

    @Synchronized
    fun append(bytes: ByteArray) {
        check(bytes.isNotEmpty() && bytes.size % 2 == 0) {
            "PCM chunks must contain complete 16-bit samples."
        }
        val active = output ?: error("Pending audio cache is closed for writing.")
        active.write(bytes)
        active.flush()
    }

    @Synchronized
    fun finishWriting() {
        output?.close()
        output = null
    }

    fun forEachChunk(maxBytes: Int, consume: (ByteArray) -> Boolean): Boolean {
        require(maxBytes > 0 && maxBytes % 2 == 0)
        finishWriting()
        BufferedInputStream(file.inputStream()).use { input ->
            val buffer = ByteArray(maxBytes)
            while (true) {
                val read = input.read(buffer)
                if (read < 0) {
                    return true
                }
                if (read == 0) {
                    continue
                }
                var accepted = read
                if (accepted % 2 != 0) {
                    val next = input.read()
                    if (next < 0) {
                        throw IllegalStateException("Owned PCM cache ended mid-sample.")
                    }
                    buffer[accepted] = next.toByte()
                    accepted += 1
                }
                if (!consume(buffer.copyOf(accepted))) {
                    return false
                }
            }
        }
    }

    fun delete(): Boolean {
        finishWriting()
        if (!isOwnedFile(root, file)) {
            return false
        }
        return !file.exists() || file.delete()
    }

    companion object {
        internal fun create(root: File, file: File): PendingAudioCache =
            PendingAudioCache(
                root = root,
                file = file,
                output = BufferedOutputStream(file.outputStream()),
            )
    }
}

class PendingAudioCacheStore(
    cacheDir: File,
    private val nowMillis: () -> Long = System::currentTimeMillis,
) {
    private val root = File(cacheDir, "kulai_voice_pending")

    fun create(): PendingAudioCache {
        ensureRoot()
        val file = File.createTempFile(OWNED_PREFIX, OWNED_SUFFIX, root)
        return PendingAudioCache.create(root, file)
    }

    fun cleanupOrphans(maxAgeMillis: Long = DEFAULT_ORPHAN_AGE_MILLIS): Int {
        require(maxAgeMillis >= 0)
        if (!root.exists()) {
            return 0
        }
        val cutoff = nowMillis() - maxAgeMillis
        return root.listFiles().orEmpty().count { candidate ->
            candidate.isFile &&
                candidate.lastModified() < cutoff &&
                isOwnedFile(root, candidate) &&
                candidate.delete()
        }
    }

    private fun ensureRoot() {
        if (!root.exists() && !root.mkdirs()) {
            throw IllegalStateException("Private audio cache is unavailable.")
        }
        if (!root.isDirectory) {
            throw IllegalStateException("Private audio cache is unavailable.")
        }
    }

    companion object {
        const val DEFAULT_ORPHAN_AGE_MILLIS = 60 * 60 * 1000L
    }
}

private fun isOwnedFile(root: File, candidate: File): Boolean {
    val canonicalRoot = try {
        root.canonicalFile
    } catch (_: Exception) {
        return false
    }
    val canonicalCandidate = try {
        candidate.canonicalFile
    } catch (_: Exception) {
        return false
    }
    return canonicalCandidate.parentFile == canonicalRoot &&
        canonicalCandidate.name.startsWith(OWNED_PREFIX) &&
        canonicalCandidate.name.endsWith(OWNED_SUFFIX)
}

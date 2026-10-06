package com.kulai.memory.cache

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class PendingAudioCacheTest {
    @get:Rule
    val temporaryFolder = TemporaryFolder()

    @Test
    fun `owned cache streams chunks and deletes after success`() {
        val store = PendingAudioCacheStore(temporaryFolder.root)
        val cache = store.create()
        cache.append(byteArrayOf(1, 2, 3, 4))
        cache.append(byteArrayOf(5, 6))
        cache.finishWriting()
        val chunks = mutableListOf<ByteArray>()

        assertTrue(cache.forEachChunk(4) { chunks.add(it); true })
        assertArrayEquals(byteArrayOf(1, 2, 3, 4, 5, 6), chunks.flattenBytes())
        assertTrue(cache.delete())
        assertFalse(cache.file.exists())
    }

    @Test
    fun `orphan cleanup touches only owned old files`() {
        val now = 10_000L
        val store = PendingAudioCacheStore(temporaryFolder.root) { now }
        val owned = store.create()
        owned.append(byteArrayOf(1, 2))
        owned.finishWriting()
        owned.file.setLastModified(1_000L)
        val root = owned.file.parentFile!!
        val foreign = File(root, "foreign.pcm").apply {
            writeBytes(byteArrayOf(9))
            setLastModified(1_000L)
        }

        assertEquals(1, store.cleanupOrphans(maxAgeMillis = 5_000L))
        assertFalse(owned.file.exists())
        assertTrue(foreign.exists())
    }

    @Test
    fun `reconnectable failure keeps cache until explicit deletion`() {
        val cache = PendingAudioCacheStore(temporaryFolder.root).create()
        cache.append(byteArrayOf(7, 8))
        cache.finishWriting()

        assertTrue(cache.file.exists())
        assertEquals(2L, cache.size)
    }
}

private fun List<ByteArray>.flattenBytes(): ByteArray {
    val result = ByteArray(sumOf { it.size })
    var offset = 0
    forEach { bytes ->
        bytes.copyInto(result, offset)
        offset += bytes.size
    }
    return result
}

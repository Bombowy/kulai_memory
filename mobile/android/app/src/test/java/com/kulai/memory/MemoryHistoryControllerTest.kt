package com.kulai.memory

import com.kulai.memory.models.*
import com.kulai.memory.transport.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.*
import org.junit.Assert.*
import org.junit.Test
import java.util.UUID
import kotlin.coroutines.Continuation
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine

@OptIn(ExperimentalCoroutinesApi::class)
class MemoryHistoryControllerTest {
    @Test fun `initial refresh active archived and filter reset`() = runTest {
        val api = FakeMemoryApi(); val controller = MemoryHistoryController(backgroundScope, api)
        controller.refresh(); assertTrue(controller.state.value.refreshing); runCurrent()
        assertEquals(listOf(historyItem()), controller.state.value.items)
        assertEquals(Triple(false, 20, null), api.lists.single())
        controller.setFilter(MemoryFilter.ARCHIVED); assertTrue(controller.state.value.items.isEmpty()); runCurrent()
        assertTrue(controller.state.value.items.single().archived)
        assertEquals(MemoryFilter.ARCHIVED, controller.state.value.filter)
        controller.setFilter(MemoryFilter.ARCHIVED); runCurrent(); assertEquals(2, api.lists.size)
    }
    @Test fun `load more passes cursor deduplicates and stops at null`() = runTest {
        val api = FakeMemoryApi(); val cursor = MemoryHistoryCursor(FIXTURE_DATE, FIXTURE_ID)
        val second = historyItem(UUID.randomUUID())
        api.list = { _, before -> if (before == null) MemoryHistoryPage(listOf(historyItem()), cursor)
            else MemoryHistoryPage(listOf(historyItem(), second), null) }
        val controller = MemoryHistoryController(backgroundScope, api)
        controller.refresh(); runCurrent(); controller.loadMore(); controller.loadMore(); runCurrent()
        assertEquals(listOf(historyItem(), second), controller.state.value.items)
        assertEquals(cursor, api.lists.last().third)
        controller.loadMore(); runCurrent(); assertEquals(2, api.lists.size)
        controller.refresh(); runCurrent(); assertEquals(listOf(historyItem()), controller.state.value.items)
    }
    @Test fun `empty history and null cursor perform no pagination`() = runTest {
        val api = FakeMemoryApi(); api.list = { _, _ -> MemoryHistoryPage(emptyList(), null) }
        val c = MemoryHistoryController(backgroundScope, api); c.refresh(); runCurrent(); c.loadMore(); runCurrent()
        assertTrue(c.state.value.items.isEmpty()); assertEquals(1, api.lists.size)
    }
    @Test fun `late active response cannot overwrite archived`() = runTest {
        val api = FakeMemoryApi(); val replies = mutableListOf<Continuation<MemoryHistoryPage>>()
        api.list = { _, _ -> suspendCoroutine { replies += it } }
        val c = MemoryHistoryController(backgroundScope, api); c.refresh(); runCurrent()
        c.setFilter(MemoryFilter.ARCHIVED); runCurrent()
        replies[1].resume(MemoryHistoryPage(listOf(historyItem(archived = true)), null)); runCurrent()
        replies[0].resume(MemoryHistoryPage(listOf(historyItem()), null)); runCurrent()
        assertEquals(MemoryFilter.ARCHIVED, c.state.value.filter); assertTrue(c.state.value.items.single().archived)
    }
    @Test fun `late load more cannot append after refresh`() = runTest {
        val api = FakeMemoryApi(); val cursor = MemoryHistoryCursor(FIXTURE_DATE, FIXTURE_ID)
        var late: Continuation<MemoryHistoryPage>? = null
        api.list = { _, before -> if (before == null) MemoryHistoryPage(listOf(historyItem()), cursor)
            else suspendCoroutine { late = it } }
        val c = MemoryHistoryController(backgroundScope, api); c.refresh(); runCurrent(); c.loadMore(); runCurrent()
        c.refresh(); runCurrent(); late!!.resume(MemoryHistoryPage(listOf(historyItem(UUID.randomUUID())), null)); runCurrent()
        assertEquals(listOf(historyItem()), c.state.value.items); assertEquals(cursor, c.state.value.nextCursor)
    }
    @Test fun `detail back and late detail selection ownership`() = runTest {
        val api = FakeMemoryApi(); val replies = mutableListOf<Continuation<MemoryDetail>>()
        api.detail = { suspendCoroutine { replies += it } }
        val c = MemoryHistoryController(backgroundScope, api); val other = UUID.randomUUID()
        c.selectMemory(FIXTURE_ID); runCurrent(); c.selectMemory(other); runCurrent()
        replies[1].resume(detail(other)); runCurrent(); replies[0].resume(detail()); runCurrent()
        assertEquals(other, c.state.value.selectedDetail!!.id)
        c.clearDetail(); assertNull(c.state.value.selectedDetail); assertFalse(c.state.value.detailLoading)
    }
    @Test fun `detail back and filter change reject old detail`() = runTest {
        val api = FakeMemoryApi(); var late: Continuation<MemoryDetail>? = null
        api.detail = { suspendCoroutine { late = it } }
        val c = MemoryHistoryController(backgroundScope, api); c.selectMemory(FIXTURE_ID); runCurrent()
        c.setFilter(MemoryFilter.ARCHIVED); runCurrent(); late!!.resume(detail()); runCurrent()
        assertNull(c.state.value.selectedDetail)
    }
    @Test fun `shutdown cancels owned requests and ignores late responses`() = runTest {
        val api = FakeMemoryApi(); var late: Continuation<MemoryHistoryPage>? = null
        api.list = { _, _ -> suspendCoroutine { late = it } }
        val c = MemoryHistoryController(backgroundScope, api); c.refresh(); runCurrent(); c.shutdown()
        val stopped = c.state.value; late!!.resume(MemoryHistoryPage(listOf(historyItem()), null)); runCurrent()
        assertEquals(stopped, c.state.value); c.refresh(); runCurrent(); assertEquals(1, api.lists.size)
    }
    @Test fun `errors are authored and refresh retry succeeds`() = runTest {
        val api = FakeMemoryApi(); api.list = { _, _ -> error("PRIVATE content password") }
        val c = MemoryHistoryController(backgroundScope, api); c.refresh(); runCurrent()
        assertEquals("Memory history could not be loaded.", c.state.value.error); assertFalse(c.state.value.loading)
        api.list = { _, _ -> MemoryHistoryPage(emptyList(), null) }; c.refresh(); runCurrent(); assertNull(c.state.value.error)
        api.detail = { throw MemoryHttpException(HttpFailure.NOT_FOUND) }; c.selectMemory(FIXTURE_ID); runCurrent()
        assertEquals("Memory not found.", c.state.value.error)
    }
    @Test fun `wrong detail identity and mismatched filter fail safely`() = runTest {
        val api = FakeMemoryApi(); api.detail = { detail(UUID.randomUUID()) }
        val c = MemoryHistoryController(backgroundScope, api); c.selectMemory(FIXTURE_ID); runCurrent()
        assertNull(c.state.value.selectedDetail); assertNotNull(c.state.value.error)
        api.list = { _, _ -> MemoryHistoryPage(listOf(historyItem(archived = true)), null) }; c.refresh(); runCurrent()
        assertTrue(c.state.value.items.isEmpty()); assertNotNull(c.state.value.error)
    }
    @Test fun `stale cache refresh is explicit and bounded limits enforced`() = runTest {
        val api = FakeMemoryApi(); val c = MemoryHistoryController(backgroundScope, api)
        c.refreshIfStale(); runCurrent(); c.refreshIfStale(); runCurrent(); assertEquals(1, api.lists.size)
        c.markStale(); c.refreshIfStale(); runCurrent(); assertEquals(2, api.lists.size)
        for (limit in listOf(0, 101)) assertThrows(IllegalArgumentException::class.java) { MemoryHistoryController(backgroundScope, api, limit) }
    }
    @Test fun `repeated pagination cursor is rejected`() = runTest {
        val api = FakeMemoryApi(); val cursor = MemoryHistoryCursor(FIXTURE_DATE, FIXTURE_ID)
        api.list = { _, _ -> MemoryHistoryPage(listOf(historyItem()), cursor) }
        val c = MemoryHistoryController(backgroundScope, api); c.refresh(); runCurrent(); c.loadMore(); runCurrent()
        assertNotNull(c.state.value.error); assertFalse(c.state.value.loadingMore); assertEquals(1, c.state.value.items.size)
    }
}

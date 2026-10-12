package com.kulai.memory

import com.kulai.memory.models.*
import com.kulai.memory.transport.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.util.UUID

enum class MemoryFilter { ACTIVE, ARCHIVED }
data class MemoryHistoryState(
    val filter: MemoryFilter = MemoryFilter.ACTIVE,
    val items: List<MemoryHistoryItem> = emptyList(),
    val loading: Boolean = false,
    val loadingMore: Boolean = false,
    val refreshing: Boolean = false,
    val nextCursor: MemoryHistoryCursor? = null,
    val selectedDetail: MemoryDetail? = null,
    val detailLoading: Boolean = false,
    val error: String? = null,
    val stale: Boolean = true,
)

/** Owned by the root ViewModel scope. Tokens also reject non-cooperative late responses. */
class MemoryHistoryController(private val scope: CoroutineScope, private val api: MemoryApi, private val limit: Int = 20) {
    init { require(limit in 1..100) }
    private val mutableState = MutableStateFlow(MemoryHistoryState())
    val state = mutableState.asStateFlow()
    private var generation = 0L
    private var detailGeneration = 0L
    private var listJob: Job? = null
    private var detailJob: Job? = null
    private var closed = false

    fun markStale() { if (!closed) mutableState.value = mutableState.value.copy(stale = true) }
    fun refreshIfStale() { if (state.value.stale && !state.value.loading && !state.value.loadingMore) refresh() }
    fun setFilter(filter: MemoryFilter) {
        if (closed || filter == state.value.filter) return
        mutableState.value = MemoryHistoryState(filter = filter)
        refresh()
    }
    fun refresh() {
        if (closed) return
        listJob?.cancel()
        clearDetail()
        val token = ++generation
        val filter = state.value.filter
        mutableState.value = MemoryHistoryState(filter = filter, loading = true, refreshing = true, stale = false)
        listJob = scope.launch {
            try {
                val page = api.listMemories(filter == MemoryFilter.ARCHIVED, limit)
                validatePage(page, filter)
                if (owns(token)) mutableState.value = state.value.copy(items = page.items,
                    nextCursor = page.nextCursor, loading = false, refreshing = false)
            } catch (cancel: CancellationException) { throw cancel }
            catch (error: Exception) {
                if (owns(token)) mutableState.value = state.value.copy(loading = false, refreshing = false,
                    error = safeHistoryError(error), stale = true)
            }
        }
    }
    fun loadMore() {
        val current = state.value
        val cursor = current.nextCursor ?: return
        if (closed || current.loading || current.loadingMore) return
        val token = generation
        mutableState.value = current.copy(loadingMore = true, error = null)
        listJob = scope.launch {
            try {
                val page = api.listMemories(current.filter == MemoryFilter.ARCHIVED, limit, cursor)
                validatePage(page, current.filter)
                if (page.nextCursor == cursor) throw IllegalArgumentException("Invalid history page.")
                if (owns(token)) mutableState.value = state.value.copy(
                    items = (state.value.items + page.items).distinctBy { it.id },
                    nextCursor = page.nextCursor, loadingMore = false)
            } catch (cancel: CancellationException) { throw cancel }
            catch (error: Exception) {
                if (owns(token)) mutableState.value = state.value.copy(loadingMore = false, error = safeHistoryError(error))
            }
        }
    }
    fun selectMemory(id: UUID) {
        if (closed) return
        detailJob?.cancel()
        val token = ++detailGeneration
        mutableState.value = state.value.copy(selectedDetail = null, detailLoading = true, error = null)
        detailJob = scope.launch {
            try {
                val detail = api.getMemory(id)
                if (detail.id != id) throw IllegalArgumentException("Invalid detail.")
                if (ownsDetail(token)) mutableState.value = state.value.copy(selectedDetail = detail, detailLoading = false)
            } catch (cancel: CancellationException) { throw cancel }
            catch (error: Exception) {
                if (ownsDetail(token)) mutableState.value = state.value.copy(detailLoading = false, error = safeHistoryError(error))
            }
        }
    }
    fun clearDetail() {
        detailGeneration++
        detailJob?.cancel()
        mutableState.value = state.value.copy(selectedDetail = null, detailLoading = false, error = null)
    }
    fun shutdown() {
        closed = true; generation++; detailGeneration++
        listJob?.cancel(); detailJob?.cancel()
    }
    private fun owns(token: Long) = !closed && generation == token
    private fun ownsDetail(token: Long) = !closed && detailGeneration == token
    private fun validatePage(page: MemoryHistoryPage, filter: MemoryFilter) {
        if (page.items.size > limit || page.items.any { it.archived != (filter == MemoryFilter.ARCHIVED) })
            throw IllegalArgumentException("Invalid history page.")
    }
    private fun safeHistoryError(error: Exception): String = when ((error as? MemoryHttpException)?.failure) {
        HttpFailure.UNAVAILABLE -> "Local server is unavailable."
        HttpFailure.INVALID_RESPONSE -> "The server response was invalid."
        HttpFailure.NOT_FOUND -> "Memory not found."
        else -> "Memory history could not be loaded."
    }
}

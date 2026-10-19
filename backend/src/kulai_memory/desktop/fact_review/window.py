"""Isolated Qt review UI. All database/provider operations run in its worker."""

from uuid import uuid4
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QTimer, Qt, Slot, QStandardPaths
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QComboBox, QGroupBox, QHBoxLayout, QLabel, QMainWindow,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .evidence import set_evidence
from .models import Intent, Query, safe_message
from .worker import FactReviewThread


TYPE_LABELS = {"fact": "Fact / Fakt", "preference": "Preference / Preferencja",
               "plan": "Plan / Intencja", "hypothesis": "Hypothesis / Hipoteza"}
RELATION_LABELS = {"exact_duplicate": "Exact duplicate", "semantic_duplicate": "Semantic duplicate",
                  "related": "Related", "possible_conflict": "Possible conflict",
                  "possible_supersession": "Possible supersession"}


def private_editor():
    editor = QPlainTextEdit()
    editor.setReadOnly(True)
    editor.setUndoRedoEnabled(False)
    return editor


def _ensure_local_font():
    # Windows' offscreen Qt plugin has no system font database. Reuse installed
    # OS fonts for readable headless acceptance captures; no font downloads.
    if QFontDatabase.families():
        return
    family = None
    for directory in QStandardPaths.standardLocations(QStandardPaths.StandardLocation.FontsLocation):
        for filename in ("segoeui.ttf", "seguiemj.ttf", "seguisym.ttf"):
            path = Path(directory) / filename
            if path.is_file():
                font_id = QFontDatabase.addApplicationFont(str(path))
                families = QFontDatabase.applicationFontFamilies(font_id)
                if family is None and families:
                    family = families[0]
    if family:
        QApplication.instance().setFont(QFont(family, 10))


class EvidencePanel(QGroupBox):
    def __init__(self, title):
        super().__init__(title)
        layout = QVBoxLayout(self)
        self.meta = QLabel()
        self.meta.setWordWrap(True)
        self.meta.setTextFormat(Qt.TextFormat.PlainText)
        self.claim = private_editor()
        self.claim.setMaximumHeight(85)
        self.source = private_editor()
        layout.addWidget(self.meta)
        layout.addWidget(self.claim)
        layout.addWidget(QLabel("Oryginalny tekst · dokładne evidence wyróżnione kolorem"))
        layout.addWidget(self.source)

    def clear(self):
        self.meta.clear()
        self.claim.clear()
        self.source.setExtraSelections([])
        self.source.clear()

    def display(self, snapshot, *, preserve=False):
        if not preserve:
            self.clear()
        candidate, source = snapshot.record.candidate, snapshot.source
        if not preserve:
            set_evidence(self.source, source.text, candidate.provenance.evidence)
        fact = candidate.fact
        if not preserve:
            self.claim.setPlainText(fact.text)
        self.meta.setText(f"{TYPE_LABELS[fact.information_type]} · {snapshot.record.status} · wersja {snapshot.record.version}\n"
            f"Podmiot: {fact.subject} · projekt: {fact.project_context or '—'} · czas: {fact.temporal_context or '—'}\n"
            f"Memory: {source.memory_id} · revision {source.revision} · źródło aktualne\n"
            f"SHA-256: {source.content_sha256}\nFact: {snapshot.record.fact_id}\n"
            f"Propozycja: {snapshot.record.id} · lock_version: {snapshot.record.lock_version}")


class FactReviewWindow(QMainWindow):
    def __init__(self, *, backend_factory=None, worker=None, poll_ms=1000, page_size=20):
        super().__init__()
        _ensure_local_font()
        if worker is None:
            if backend_factory is None:
                from .demo import demo_backend
                backend_factory = demo_backend
            worker = FactReviewThread(backend_factory, self)
        self.worker = worker
        self.setWindowTitle("KulAI Memory · Fact Review · TEST DATA")
        self.resize(1250, 900)
        self._ready = self._busy = self._closing = self._finished = False
        self._background_read = False
        self._read_started = 0.0
        self._token = 0
        self._selected = None
        self._detail = self._view = None
        self._pending_intent = None
        self._after = self._next_after = None
        self._pages = []
        self._page_size = page_size
        self._build()
        self.worker.ready.connect(self._on_ready)
        self.worker.completed.connect(self._on_completed)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._on_finished)
        self._poll = QTimer(self)
        self._poll.setInterval(max(100, poll_ms))
        self._poll.timeout.connect(self._poll_refresh)
        self._lease = QTimer(self)
        self._lease.setSingleShot(True)
        self._lease.timeout.connect(self._expire)
        self._lease_ms = max(500, poll_ms * 2)
        self._enable()
        self.worker.start()

    def _build(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)
        self.banner = QLabel("TEST DATA · kontrolowany provider · osobna marker-owned PostgreSQL · bez main")
        self.banner.setStyleSheet("background:#ffe499;color:#16202a;padding:9px;font-weight:bold")
        self.database = QLabel("Tworzenie własnej testowej bazy…")
        layout.addWidget(self.banner)
        layout.addWidget(self.database)
        controls = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItem("Fakty", "facts")
        self.mode.addItem("Relacje i sprzeczności", "relations")
        self.status_filter = QComboBox()
        for value in ("PENDING", "APPROVED", "REJECTED", "REVOKED"):
            self.status_filter.addItem(value, value)
        self.type_filter = QComboBox()
        self.type_filter.addItem("Wszystkie typy", None)
        for value, label in TYPE_LABELS.items():
            self.type_filter.addItem(label, value)
        self.relation_filter = QComboBox()
        self.relation_filter.addItem("Wszystkie relacje", None)
        for value, label in RELATION_LABELS.items():
            self.relation_filter.addItem(label, value)
        self.refresh = QPushButton("REFRESH")
        for widget in (self.mode, self.status_filter, self.type_filter, self.relation_filter, self.refresh):
            controls.addWidget(widget)
        layout.addLayout(controls)
        self.counts = QLabel("Oczekujące: —")
        layout.addWidget(self.counts)
        self.table = QTableWidget(0, 5)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMaximumHeight(220)
        layout.addWidget(self.table)
        paging = QHBoxLayout()
        self.previous = QPushButton("Poprzednia")
        self.next = QPushButton("Następna")
        self.page_label = QLabel("Strona 1")
        for widget in (self.previous, self.page_label, self.next):
            paging.addWidget(widget)
        layout.addLayout(paging)
        splitter = QSplitter()
        self.left, self.right = EvidencePanel("Propozycja / lewa strona"), EvidencePanel("Prawa strona relacji")
        splitter.addWidget(self.left)
        splitter.addWidget(self.right)
        layout.addWidget(splitter, 1)
        self.history = private_editor()
        self.history.setMaximumHeight(135)
        layout.addWidget(self.history)
        decisions = QHBoxLayout()
        self.approve = QPushButton("APPROVE")
        self.reject = QPushButton("REJECT")
        self.revoke = QPushButton("REVOKE")
        self.resolve = QPushButton("ODCZYTAJ WYNIK ŻĄDANIA")
        self.retry = QPushButton("RETRY · TEN SAM REQUEST ID")
        for widget in (self.approve, self.reject, self.revoke, self.resolve, self.retry):
            decisions.addWidget(widget)
        layout.addLayout(decisions)
        self.message = QLabel("Wybierz propozycję, przeczytaj źródło i podejmij osobną decyzję.")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.message)
        for combo in (self.mode, self.status_filter, self.type_filter, self.relation_filter):
            combo.currentIndexChanged.connect(self._filter_changed)
        self.refresh.clicked.connect(self._refresh)
        self.previous.clicked.connect(self._previous_page)
        self.next.clicked.connect(self._next_page)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        for button, action in ((self.approve, "approve"), (self.reject, "reject"), (self.revoke, "revoke")):
            button.clicked.connect(lambda checked=False, action=action: self._decide(action))
        self.resolve.clicked.connect(lambda: self._send("resolve", self._pending_intent))
        self.retry.clicked.connect(lambda: self._send("decide", self._pending_intent))

    def _enable(self):
        usable = self._ready and not self._busy and not self._closing
        readable = self._ready and (not self._busy or self._background_read) and not self._closing
        for widget in (self.mode, self.status_filter, self.refresh, self.table):
            widget.setEnabled(readable)
        self.type_filter.setEnabled(readable and self.mode.currentData() == "facts")
        self.relation_filter.setEnabled(readable and self.mode.currentData() == "relations")
        self.previous.setEnabled(usable and bool(self._pages))
        self.next.setEnabled(usable and self._next_after is not None)
        can_decide = usable and self._detail is not None and self._pending_intent is None
        pending = can_decide and self._detail.status == "PENDING"
        relation_ready = not can_decide or not self._detail.suggestion or all(s.record.status == "APPROVED" for s in self._detail.snapshots)
        self.approve.setEnabled(bool(pending and relation_ready))
        self.reject.setEnabled(bool(pending))
        self.revoke.setEnabled(bool(can_decide and self._detail.status == "APPROVED"))
        for widget in (self.resolve, self.retry):
            widget.setVisible(self._pending_intent is not None)
            widget.setEnabled(usable and self._pending_intent is not None)

    def _clear_private(self):
        self._lease.stop()
        self._view = self._detail = None
        self._next_after = None
        self.table.blockSignals(True)
        self.table.clearContents()
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        self.left.clear()
        self.right.clear()
        self.history.clear()
        self.counts.setText("Oczekujące: —")
        self._enable()

    def _expire(self):
        self._token += 1  # A late read must not renew an already expired display.
        self._selected = None
        self._clear_private()
        if not self._busy:
            self._refresh()

    @Slot(str)
    def _on_ready(self, name):
        if self._closing:
            return
        self._ready = True
        self.database.setText("TEST DB: " + name + " · zamknięcie okna usuwa tę własną bazę demo")
        self._poll.start()
        self._refresh()

    def _query(self):
        return Query(mode=self.mode.currentData(), status=self.status_filter.currentData(),
            information_type=self.type_filter.currentData(), relation=self.relation_filter.currentData(),
            after=self._after, limit=self._page_size, selected=self._selected)

    def _send(self, operation, value, *, background=False):
        if self._busy or not self._ready or self._closing or value is None:
            return
        self._busy = True
        self._background_read = background and operation == "read"
        if operation == "read":
            self._read_started = monotonic()
        if not self._background_read:
            self._clear_private()
        self._token += 1
        if not self.worker.submit(self._token, operation, value):
            self._busy = False
            self._background_read = False
            self.message.setText("Worker nie przyjął operacji. Odśwież widok.")
        self._enable()

    def _refresh(self):
        if self._busy and self._background_read:
            self._token += 1
            self._clear_private()
            return  # Current response is discarded, then a fresh read is queued.
        if self._ready and not self._busy and not self._closing:
            self._send("read", self._query())

    def _poll_refresh(self):
        if self._ready and not self._busy and not self._closing:
            self._send("read", self._query(), background=True)

    def _filter_changed(self):
        self._token += 1  # Discard any result queued for a previous filter.
        self._selected = self._after = self._next_after = None
        self._pages.clear()
        self._clear_private()
        self._refresh()

    def _next_page(self):
        if self._next_after is not None:
            self._pages.append(self._after)
            self._after, self._selected = self._next_after, None
            self._refresh()

    def _previous_page(self):
        if self._pages:
            self._after, self._selected = self._pages.pop(), None
            self._refresh()

    def _selection_changed(self):
        if (self._busy and not self._background_read) or self._view is None:
            return
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return
        row = indexes[0].row()
        if 0 <= row < len(self._view.rows):
            self._selected = self._view.rows[row].id
            self._refresh()

    def _decide(self, action):
        if self._busy or self._pending_intent is not None or self._detail is None:
            return
        detail = self._detail
        self._pending_intent = Intent(detail.id, "relation" if detail.suggestion else "candidate",
                                      action, detail.lock_version, uuid4())
        self.message.setText("Decyzja w toku; wyświetlone dane wyczyszczono.")
        self._send("decide", self._pending_intent)

    @Slot(int, str, object)
    def _on_completed(self, token, operation, result):
        if self._closing:
            return
        if token != self._token:
            self._busy = False
            self._background_read = False
            self._clear_private()
            QTimer.singleShot(0, self._refresh)
            return
        self._busy = False
        self._background_read = False
        if operation == "read":
            if (monotonic() - self._read_started) * 1000 >= self._lease_ms:
                self._expire()
                return
            try:
                if result != self._view:
                    self._display(result)
                else:
                    self._lease.start(self._lease_ms)
            except Exception:
                self._clear_private()
                self.message.setText("Niezgodne evidence. Treść usunięto z panelu; wymagana nowa analiza.")
        else:
            self._clear_private()
            self._selected = None  # Receipts/retries must never repopulate an old source panel.
            if result.state == "recorded":
                self._pending_intent = None
                self._selected = None
                self.message.setText(f"Zapisano decyzję {result.receipt.action}. Request: {result.receipt.request_id}")
            elif result.state == "denied":
                self._pending_intent = None
                self._selected = None
                self.message.setText(safe_message(result.error_code))
            else:
                self.message.setText("Wynik niepotwierdzony. Odczytaj ten sam request ID lub jawnie ponów TEN SAM request. Nowe decyzje są zablokowane.")
            # Defer until the worker releases its single-operation admission lock.
            QTimer.singleShot(0, self._refresh)
        self._enable()

    def _display(self, view):
        old = self._view
        same_sources = bool(old and old.detail and view.detail and
            len(old.detail.snapshots) == len(view.detail.snapshots) and all(
                a.source == b.source and a.source_token == b.source_token and
                a.record.candidate == b.record.candidate
                for a, b in zip(old.detail.snapshots, view.detail.snapshots)))
        scrolls = [(w, w.verticalScrollBar().value(), w.horizontalScrollBar().value())
                   for w in (self.table, self.history)]
        if not same_sources:
            self.left.clear()
            self.right.clear()
            self.history.clear()
        self._view, self._detail, self._next_after = view, view.detail, view.next_after
        self.counts.setText(f"Oczekujące fakty: {view.pending_count} · wyników filtra: {view.total_count}")
        self.page_label.setText(f"Strona {len(self._pages) + 1} · limit {self._page_size}")
        self.table.blockSignals(True)
        headers = ("Propozycja", "Typ", "Podmiot", "Projekt", "Status") if self.mode.currentData() == "facts" else ("Relacja", "Status", "Lewa propozycja", "Prawa propozycja")
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(view.rows))
        self.table.clearSelection()
        for index, row in enumerate(view.rows):
            for column, value in enumerate(row.cells):
                if self.mode.currentData() == "facts" and column == 1:
                    value = TYPE_LABELS[value]
                self.table.setItem(index, column, QTableWidgetItem(value))
            if row.id == self._selected:
                self.table.selectRow(index)
        self.table.resizeColumnsToContents()
        if self.mode.currentData() == "facts":
            self.table.setColumnWidth(0, 430)
        else:
            for column, width in enumerate((200, 100, 430, 430)):
                self.table.setColumnWidth(column, width)
        self.table.blockSignals(False)
        self.right.setVisible(view.detail is not None and len(view.detail.snapshots) == 2)
        if view.detail:
            detail = view.detail
            self.left.display(detail.snapshots[0], preserve=same_sources)
            if len(detail.snapshots) == 2:
                self.right.display(detail.snapshots[1], preserve=same_sources)
            lines = [f"Historia decyzji (maks. 100): {len(detail.decisions)}"]
            lines.extend(f"{d.decided_at.isoformat()} · {d.reviewer_id} · {d.action} · {d.request_id}" for d in detail.decisions)
            lines.append("Wersje (maks. 100): " + ", ".join(f"v{v}={s}" for v, s in detail.versions))
            lines.extend(f"Powiązanie: {kind} · {status} · {identifier}" for identifier, kind, status in detail.relations)
            if detail.suggestion:
                lines.insert(0, f"{detail.suggestion.relation} · {detail.suggestion.status} · lock_version: {detail.lock_version}")
                lines.append(detail.suggestion.suggestion.rationale)
                lines.append("Zgoda na relację wymaga obu zatwierdzonych faktów. Nie usuwa starszej Memory.")
            history_text = "\n".join(lines)
            if self.history.toPlainText() != history_text:
                self.history.setPlainText(history_text)
        if view.stale_selection:
            self._selected = None
            self.message.setText("Źródło zmieniło się lub zostało usunięte. Panel wyczyszczono; wymagana nowa analiza.")
        self._lease.start(self._lease_ms)
        if old:
            for widget, vertical, horizontal in scrolls:
                widget.verticalScrollBar().setValue(vertical)
                widget.horizontalScrollBar().setValue(horizontal)

    @Slot(int, str, str)
    def _on_failed(self, token, operation, message):
        if self._closing:
            return
        if token != -1 and token != self._token:
            self._busy = False
            self._background_read = False
            self._clear_private()
            QTimer.singleShot(0, self._refresh)
            return
        self._busy = False
        self._background_read = False
        self._selected = None
        self._clear_private()
        self.message.setText(message)
        # Keep a decision's technical intent after any unclassified worker
        # failure. Only a receipt or explicit domain denial can release it.
        self._enable()

    @Slot()
    def _on_finished(self):
        self._finished = True
        self._ready = False
        self._poll.stop()
        self._clear_private()
        if self._closing:
            self.close()
        else:
            self.message.setText(self.message.text() + " Demo zatrzymane; zamknij okno.")

    def closeEvent(self, event):
        if self._finished:
            event.accept()
            return
        self._closing = True
        self._poll.stop()
        self._clear_private()
        self.message.setText("Zamykanie operacji, połączeń i własnej bazy demo…")
        self.worker.request_shutdown()
        # A test/local adapter can finish synchronously. Do not undo the
        # finished handler's close with an outer ignored close event.
        if self._finished:
            event.accept()
        else:
            event.ignore()

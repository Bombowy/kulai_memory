"""Two tabs, one existing review widget, one worker and one owned database."""

from time import monotonic

from PySide6.QtCore import QTimer, Qt, Slot
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QTabWidget,QLineEdit,
    QPushButton,QLabel,QListWidget,QListWidgetItem,QComboBox,QMessageBox)

from ..fact_review.window import FactReviewWindow,EvidencePanel,private_editor,TYPE_LABELS
from .backend import Display,Mutation,workspace_backend
from .worker import WorkspaceThread


class FactWorkspaceWindow(FactReviewWindow):
    def __init__(self, *, real_models=False, backend_factory=None, worker=None, confirm=None, poll_ms=1000):
        self._packet=None
        self._active_operation=None
        self._handles_loaded=False
        self._last_error=None
        self._confirm=confirm
        self.real_models=real_models
        if worker is None:
            factory=backend_factory or (lambda:workspace_backend(real_models=real_models))
            worker=WorkspaceThread(factory)
        super().__init__(worker=worker,poll_ms=poll_ms)
        self.setWindowTitle('KulAI Memory · Local V1 Workspace · TEST DATA')
        self.worker.progress.connect(self._progress)
        self.worker.denied.connect(self._denied)
        self._answer_lease=QTimer(self)
        self._answer_lease.setSingleShot(True)
        self._answer_lease.timeout.connect(self._expire_answer)

    def _build(self):
        super()._build()
        review=self.takeCentralWidget()
        self.tabs=QTabWidget()
        self.tabs.addTab(review,'Fact Review')
        ask=QWidget()
        layout=QVBoxLayout(ask)
        self.ai_mode=QLabel('REAL AI: Qwen/BGE dla ASK i grounding' if self.real_models
                           else 'CONTROLLED: szybki test, bez Ollama')
        layout.addWidget(self.ai_mode)
        layout.addWidget(QLabel('TEST DATA · wspólne źródła i decyzje · ekstrakcja/weryfikacja kandydatów: kontrolowane adnotacje'))
        self.question=QLineEdit()
        self.question.setPlaceholderText('Czy MCP jest już wdrożony?')
        self.question.setMaxLength(4000)
        row=QHBoxLayout(); row.addWidget(self.question)
        self.ask_button=QPushButton('ASK')
        self.sources_button=QPushButton('Zatwierdzone źródła')
        self.cancel_button=QPushButton('ANULUJ ASK')
        for w in (self.ask_button,self.sources_button,self.cancel_button): row.addWidget(w)
        layout.addLayout(row)
        self.ask_status=QLabel('Zatwierdź fakty w Fact Review, potem zadaj pytanie.')
        self.ask_status.setTextFormat(Qt.TextFormat.PlainText)
        self.ask_status.setWordWrap(True)
        layout.addWidget(self.ask_status)
        self.answer=private_editor(); self.answer.setMaximumHeight(110)
        layout.addWidget(self.answer)
        self.coverage=QLabel('Wyszukiwanie obejmuje najwyżej 50 kandydatów według UUID; nie jest globalnym przeszukaniem pamięci.')
        self.coverage.setWordWrap(True)
        layout.addWidget(self.coverage)
        self.citations=QListWidget(); self.citations.setMaximumHeight(100)
        layout.addWidget(self.citations)
        self.citation_panel=EvidencePanel('Zweryfikowane cytowanie / źródło')
        # Keep all six metadata lines readable instead of letting expanding
        # text editors squeeze the QLabel to a single line at 900px height.
        for panel in (self.left,self.right,self.citation_panel):
            panel.meta.setMinimumHeight(100)
            panel.meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            panel.claim.setMinimumHeight(35); panel.claim.setMaximumHeight(60)
            panel.source.setMinimumHeight(75)
        self.history.setMaximumHeight(100)
        layout.addWidget(self.citation_panel,1)
        self.generation=QLabel(); self.generation.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.generation)
        self.show_review=QPushButton('Pokaż wybrany fakt w Review')
        layout.addWidget(self.show_review)
        self.tabs.addTab(ask,'Ask Approved Facts')
        self.setCentralWidget(self.tabs)
        # These actions exist only in this synthetic TEST DATA launcher.
        controls=QHBoxLayout()
        self.source_selector=QComboBox()
        controls.addWidget(QLabel('TEST DATA: źródło'))
        controls.addWidget(self.source_selector)
        self.source_refresh=QPushButton('Odśwież stan źródeł')
        controls.addWidget(self.source_refresh)
        self.mutations={}
        for action in ('edit','archive','restore','delete'):
            button=QPushButton(action.upper()+' TEST')
            button.clicked.connect(lambda checked=False,a=action:self._mutate(a))
            self.mutations[action]=button; controls.addWidget(button)
        review.layout().insertLayout(3,controls)
        self.ask_button.clicked.connect(self._ask)
        self.question.returnPressed.connect(self._ask)
        self.sources_button.clicked.connect(lambda:self._send('sources',True))
        self.cancel_button.clicked.connect(self._cancel)
        self.citations.currentItemChanged.connect(self._citation_selected)
        self.show_review.clicked.connect(self._to_review)
        self.source_refresh.clicked.connect(lambda:self._send('handles',True))
        self.source_selector.currentIndexChanged.connect(self._enable)
        self.tabs.currentChanged.connect(self._tab_changed)

    def _enable(self):
        super()._enable()
        if not hasattr(self,'ask_button'): return
        idle=self._ready and not self._busy and not self._closing
        allowed=idle and self._pending_intent is None
        for w in (self.question,self.ask_button,self.sources_button,self.source_refresh,self.source_selector):
            w.setEnabled(allowed)
        self.cancel_button.setEnabled(self._busy and self._active_operation in WorkspaceThread.reads and not self._closing)
        self.citations.setEnabled(idle)
        self.show_review.setEnabled(idle and self.citations.currentItem() is not None)
        source=self.source_selector.currentData()
        for action,button in self.mutations.items():
            button.setEnabled(bool(allowed and source and not source.deleted and
                (source.archived if action=='restore' else True if action=='delete' else not source.archived)))

    def _clear_answer(self):
        if hasattr(self,'_answer_lease'): self._answer_lease.stop()
        self._packet=None
        self.answer.clear()
        self.citations.blockSignals(True); self.citations.clear(); self.citations.blockSignals(False)
        self.citation_panel.clear(); self.generation.clear()

    def _send(self,operation,value,*,background=False):
        if self._busy or not self._ready or self._closing: return
        self._active_operation=operation
        if operation in {'ask','sources','decide','resolve','mutate'}: self._clear_answer()
        if operation=='open_citation': self.citation_panel.clear(); self.generation.clear()
        if operation=='ask': self._last_error=None; self.ask_status.setText('Wyszukiwanie / generowanie…')
        super()._send(operation,value,background=background)

    def _ask(self):
        if not self.question.text().strip() or self._pending_intent is not None: return
        self._send('ask',self.question.text())

    def _cancel(self):
        self._clear_answer()
        self.worker.cancel_read(self._token,self._active_operation)
        self._token+=1
        self.ask_status.setText('Anulowanie operacji…')

    def _refresh(self):
        if hasattr(self,'tabs') and self.tabs.currentIndex()==1: return
        super()._refresh()

    def _poll_refresh(self):
        if self.tabs.currentIndex()==1:
            if self._packet is not None: self._send('check_display',self._packet.id)
        else: super()._poll_refresh()

    def _tab_changed(self,index):
        if index==1:
            self._lease.stop(); self._clear_private()
        else:
            self._clear_answer(); self._refresh()

    def _expire_answer(self):
        self._token+=1
        self._clear_answer()
        self.ask_status.setText('Wygasła aktualność widoku. Zadaj nowe pytanie lub ponownie otwórz źródła.')
        self._enable()

    def _citation_selected(self,item,previous):
        if item is not None and self._packet is not None:
            self._send('open_citation',(self._packet.id,item.data(Qt.ItemDataRole.UserRole)))

    def _to_review(self):
        item=self.citations.currentItem()
        if self._busy or item is None: return
        target=item.data(Qt.ItemDataRole.UserRole)
        self.tabs.setCurrentIndex(0)
        self.mode.setCurrentIndex(self.mode.findData('facts'))
        self.status_filter.setCurrentIndex(self.status_filter.findData('APPROVED'))
        self._selected=target; self._after=None; self._pages.clear(); self._refresh()

    def _mutate(self,action):
        source=self.source_selector.currentData()
        if self._busy or source is None or source.deleted or self._pending_intent is not None: return
        text=f'{action.upper()} syntetycznego źródła {source.label}? EDIT dopisuje linię TEST DATA; DELETE usuwa źródło i fakty. Zwykła baza nie jest podłączona.'
        approved=self._confirm(text) if self._confirm else QMessageBox.question(self,'TEST DATA — jawna zmiana',text,
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)==QMessageBox.StandardButton.Yes
        if approved: self._send('mutate',Mutation(source,action))

    @Slot(int,str)
    def _progress(self,token,stage):
        if token==self._token and self._busy and not self._closing: self.ask_status.setText(stage)

    @Slot(int,str)
    def _denied(self,token,code):
        if token==self._token: self._last_error=code

    def _on_completed(self,token,operation,result):
        if operation in {'read','decide','resolve'}:
            super()._on_completed(token,operation,result)
            if self.tabs.currentIndex()==1: self._lease.stop(); self._clear_private()
            if not self._handles_loaded and self._ready and not self._busy:
                QTimer.singleShot(0,lambda:self._send('handles',True))
            return
        if self._closing: return
        self._busy=False; self._background_read=False
        if token!=self._token:
            self._clear_answer(); self._enable(); return
        try:
            if operation in {'ask','sources'}:
                if monotonic()-result.checked_at>self._lease_ms/1000: raise ValueError
                self._packet=result
                self.answer.setPlainText(result.answer.answer if result.answer else 'Aktualnie zatwierdzone źródła — bez generowania odpowiedzi.')
                self.citations.blockSignals(True)
                for f in result.facts:
                    item=QListWidgetItem(f'{TYPE_LABELS[f.information_type]}: {f.text}')
                    item.setData(Qt.ItemDataRole.UserRole,f.reference.candidate_id); self.citations.addItem(item)
                self.citations.blockSignals(False)
                self.coverage.setText(('NIEPEŁNE POKRYCIE — ' if result.partial else 'Mała, ograniczona kolekcja — ')+
                    (', '.join(result.answer.partial_reasons) if result.answer else 'widok maks. 20 źródeł')+
                    '. Retrieval: najwyżej 50 kandydatów według UUID, nie globalne wyszukiwanie.')
                self.ask_status.setText('Odpowiedź zweryfikowana.' if result.answer else 'Źródła ponownie zweryfikowane.')
                self._answer_lease.start(self._lease_ms)
            elif operation=='open_citation':
                if monotonic()-result.checked_at>self._lease_ms/1000 or self._packet is None: raise ValueError
                self.citation_panel.display(result.snapshot)
                r=result.reference
                self.generation.setText(f'Incarnation: {r.incarnation} · generation: {r.generation} · fact version: {r.fact_version}')
                self._answer_lease.start(self._lease_ms)
            elif operation=='check_display':
                if self._packet is None or result[0]!=self._packet.id or monotonic()-result[1]>self._lease_ms/1000: raise ValueError
                self._answer_lease.start(self._lease_ms)
            elif operation in {'handles','mutate'}:
                selected=self.source_selector.currentIndex()
                self.source_selector.blockSignals(True); self.source_selector.clear()
                for source in result:
                    self.source_selector.addItem(source.label+(' · DELETED' if source.deleted else ' · ARCHIVED' if source.archived else ' · ACTIVE'),source)
                self.source_selector.setCurrentIndex(max(0,selected)); self.source_selector.blockSignals(False)
                self._handles_loaded=True
                if operation=='mutate':
                    self.message.setText('Źródło TEST zmienione. Stara zgoda nie wraca po RESTORE. Odświeżono stan.')
                self._refresh()
        except Exception:
            self._clear_answer(); self.ask_status.setText('Widok nieaktualny lub niezgodne evidence. Treść wyczyszczono.')
        self._enable()

    def _on_failed(self,token,operation,message):
        self._clear_answer()
        super()._on_failed(token,operation,message)
        self.ask_status.setText(message)

    def _on_finished(self):
        self._clear_answer()
        super()._on_finished()

    def closeEvent(self,event):
        self._clear_answer()
        super().closeEvent(event)

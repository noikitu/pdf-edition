"""Sessions de documents en mémoire, avec pile d'annulation."""

from __future__ import annotations

import threading
import time
import uuid

import fitz

MAX_HISTORY = 25          # nombre d'états conservés pour l'annulation
SESSION_TTL = 6 * 3600    # durée de vie d'une session sans activité (secondes)


class Session:
    def __init__(self, name: str, data: bytes):
        self.name = name
        self.doc = fitz.open(stream=data, filetype="pdf")
        self.undo_stack: list[bytes] = []
        self.redo_stack: list[bytes] = []
        self.version = 0
        self.touched = time.time()

    # -- historique ---------------------------------------------------------
    def snapshot(self) -> None:
        """À appeler avant toute modification."""
        self.undo_stack.append(self.doc.tobytes())
        del self.undo_stack[:-MAX_HISTORY]
        self.redo_stack.clear()

    def _swap(self, take: list[bytes], put: list[bytes]) -> bool:
        if not take:
            return False
        put.append(self.doc.tobytes())
        del put[:-MAX_HISTORY]
        data = take.pop()
        self.doc.close()
        self.doc = fitz.open(stream=data, filetype="pdf")
        self.version += 1
        return True

    def replace_doc(self, data: bytes) -> None:
        """Remplace le document par une version sérialisée (ex. après compression),
        en conservant l'historique déjà accumulé — seul l'état courant change."""
        self.doc.close()
        self.doc = fitz.open(stream=data, filetype="pdf")
        self.version += 1

    def undo(self) -> bool:
        return self._swap(self.undo_stack, self.redo_stack)

    def redo(self) -> bool:
        return self._swap(self.redo_stack, self.undo_stack)

    @property
    def can_undo(self) -> bool:
        return bool(self.undo_stack)

    @property
    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    def close(self) -> None:
        try:
            self.doc.close()
        except Exception:
            pass


class Store:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, name: str, data: bytes) -> tuple[str, Session]:
        session = Session(name, data)
        doc_id = uuid.uuid4().hex
        with self._lock:
            self._sessions[doc_id] = session
        self.purge()
        return doc_id, session

    def get(self, doc_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(doc_id)
        if session:
            session.touched = time.time()
        return session

    def drop(self, doc_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(doc_id, None)
        if session:
            session.close()

    def purge(self) -> None:
        cutoff = time.time() - SESSION_TTL
        with self._lock:
            stale = [k for k, s in self._sessions.items() if s.touched < cutoff]
            dropped = [self._sessions.pop(k) for k in stale]
        for session in dropped:
            session.close()


store = Store()

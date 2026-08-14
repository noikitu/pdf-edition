"""Sessions de documents en mémoire, avec pile d'annulation."""

from __future__ import annotations

import threading
import time
import uuid

import fitz

from . import persist
from .config import MAX_SESSIONS, SESSION_TTL

MAX_HISTORY = 25          # nombre d'états conservés pour l'annulation


class Session:
    def __init__(self, name: str, data: bytes, doc_id: str = ""):
        self.doc_id = doc_id
        self.name = name
        self.doc = fitz.open(stream=data, filetype="pdf")
        self.undo_stack: list[bytes] = []
        self.redo_stack: list[bytes] = []
        self.version = 0
        self.saved_version = -1
        self.size = len(data)     # poids du document, tenu à jour par autosave
        self.touched = time.time()

    def autosave(self) -> None:
        """Écrit l'état courant s'il a changé depuis la dernière sauvegarde.

        Appelé au terme de chaque requête qui modifie le document. Sérialiser
        coûte ici ce que coûte déjà l'instantané d'annulation, pris à chaque
        modification lui aussi.
        """
        if not persist.enabled() or self.saved_version == self.version:
            return
        data = self.doc.tobytes(garbage=3, deflate=True)
        # Le poids est relevé au passage : le recalculer pour la fiche du
        # document exigerait de sérialiser à nouveau tout le PDF.
        self.size = len(data)
        persist.save(self.doc_id, self.name, data, self.version)
        self.saved_version = self.version

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
        doc_id = uuid.uuid4().hex
        session = Session(name, data, doc_id)
        with self._lock:
            self._sessions[doc_id] = session
        self.purge()
        self._evict()
        session.autosave()
        return doc_id, session

    def get(self, doc_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(doc_id)
        if session is None:
            session = self._reopen(doc_id)
        if session:
            session.touched = time.time()
        return session

    def _reopen(self, doc_id: str) -> Session | None:
        """Recharge un document depuis le disque.

        Sert à la reprise après un redémarrage, mais aussi quand une session a été
        évincée faute de place : le document revient de lui-même au premier accès.
        L'historique d'annulation, lui, n'est pas conservé.
        """
        saved = persist.load(doc_id)
        if saved is None:
            return None
        name, data = saved
        try:
            session = Session(name, data, doc_id)
        except Exception:
            return None
        session.saved_version = session.version
        with self._lock:
            self._sessions[doc_id] = session
        return session

    def drop(self, doc_id: str) -> None:
        """Ferme un document et efface sa sauvegarde : la fermeture est explicite."""
        with self._lock:
            session = self._sessions.pop(doc_id, None)
        if session:
            session.close()
        persist.drop(doc_id)

    def purge(self) -> None:
        cutoff = time.time() - SESSION_TTL
        with self._lock:
            stale = [k for k, s in self._sessions.items() if s.touched < cutoff]
            dropped = [self._sessions.pop(k) for k in stale]
        for session in dropped:
            session.close()
        persist.purge()

    def _evict(self) -> None:
        """Garde au plus MAX_SESSIONS documents ouverts, en fermant les plus anciens.

        Les documents résident en mémoire : sans cette borne, une instance
        partagée finirait par gonfler jusqu'à se faire tuer par l'hébergeur. La
        sauvegarde sur disque est conservée — un accès ultérieur rouvrira le
        document sans que l'utilisateur s'aperçoive de rien.
        """
        with self._lock:
            excess = len(self._sessions) - MAX_SESSIONS
            if excess <= 0:
                return
            oldest = sorted(self._sessions, key=lambda k: self._sessions[k].touched)
            dropped = [self._sessions.pop(k) for k in oldest[:excess]]
        for session in dropped:
            session.close()


store = Store()

"""
core/agent_knowledge.py — Per-agent knowledge base.

Each agent owns a private AgentKnowledgeBase seeded from its skills file
in knowledge/agents/{agent_name}_skills.md. The agent can query its own KB
to inform decisions — without touching the shared ZEUS KB or another agent's KB.

Design rule: agents never share their private KB. ZEUS's shared KB is for
cross-agent memory. Each agent's KB is for domain expertise only.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("agent_knowledge")

AGENT_KNOWLEDGE_DIR = Path("knowledge/agents")
CHROMA_BASE_PATH    = Path("data/agent_chroma")


class AgentKnowledgeBase:
    """
    Lightweight per-agent KB. Loads one markdown skills file.
    Supports simple keyword search (no ChromaDB dependency — agents
    stay light). Upgrades to vector search in Phase 2 if needed.
    """

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        self._chunks: list[str] = []
        self._load()

    def query(self, question: str, n_results: int = 3) -> list[str]:
        """
        Return the most relevant chunks from this agent's skills file.
        Uses TF-IDF-style keyword overlap (no external dependencies).
        """
        if not self._chunks:
            return []

        question_words = set(question.lower().split())
        scored = []
        for chunk in self._chunks:
            chunk_words = set(chunk.lower().split())
            score = len(question_words & chunk_words)
            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [chunk for _, chunk in scored[:n_results]]

    def get_all(self) -> str:
        """Return full skills content — used for system prompt injection."""
        return "\n\n".join(self._chunks)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> None:
        skills_file = AGENT_KNOWLEDGE_DIR / f"{self.agent_name}_skills.md"
        if not skills_file.exists():
            logger.info("[AGENT-KB] No skills file for %s — starting empty.", self.agent_name)
            return
        text = skills_file.read_text(encoding="utf-8")
        self._chunks = self._split_by_section(text)
        logger.info("[AGENT-KB] Loaded %d sections for %s.", len(self._chunks), self.agent_name)

    @staticmethod
    def _split_by_section(text: str) -> list[str]:
        """Split markdown by ## headings into coherent chunks."""
        lines   = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        for line in lines:
            if line.startswith("## ") and current:
                chunks.append("\n".join(current).strip())
                current = [line]
            else:
                current.append(line)
        if current:
            chunks.append("\n".join(current).strip())
        return [c for c in chunks if c]

"""Ready-made search settings, offered in the console. A person picks one, tries it on real questions, and it becomes a normal draft."""
from __future__ import annotations

RAG_PRESETS: dict[str, dict] = {
    "strict": {"faq_threshold": 0.9, "min_score": 0.5, "top_k": 3},       # answers less and says "I'll confirm" more
    "balanced": {"faq_threshold": 0.86, "min_score": 0.35, "top_k": 4},
    "broad": {"faq_threshold": 0.8, "min_score": 0.25, "top_k": 6},       # finds more, and may bring a passage that is only close
}

from __future__ import annotations

import json
import os
import re
from collections import defaultdict

from app.config import Settings
from app.db import Database
from app.embedding import Embedder, TOKEN, cosine
from app.observability import Trace


STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does",
    "for", "from", "how", "i", "in", "is", "it", "me", "of", "on", "or",
    "our", "the", "their", "there", "to", "us", "was", "what", "when",
    "where", "which", "who", "why", "with", "you", "your",
}
CITATION = re.compile(r"\[(\d+)\]")


def keywords(text: str) -> set[str]:
    result = set()
    for raw in TOKEN.findall(text.lower()):
        if raw in STOP_WORDS or len(raw) < 3:
            continue
        word = raw
        if word.endswith("ing") and len(word) > 6:
            word = word[:-3]
        elif word.endswith("ed") and len(word) > 5:
            word = word[:-2]
        elif word.endswith("es") and len(word) > 5:
            word = word[:-2]
        elif word.endswith("s") and len(word) > 4:
            word = word[:-1]
        result.add(word)
    return result


def query_parts(question: str) -> list[str]:
    # Decompose compound questions only when each side carries useful terms.
    parts = re.split(r"\s+(?:and|also)\s+|\s*;\s*", question, maxsplit=2, flags=re.I)
    if len(parts) > 1 and all(len(keywords(part)) >= 2 for part in parts):
        return parts[:3]
    return [question]


def fts_expression(query: str) -> str:
    tokens = TOKEN.findall(query.lower())
    useful = [token for token in tokens if token not in STOP_WORDS]
    return " OR ".join(f'"{token}"' for token in (useful or tokens)[:12])


def _coverage(question: str, passage: str) -> float:
    requested = keywords(question)
    if not requested:
        return 0.0
    return len(requested & keywords(passage)) / len(requested)


class QueryEngine:
    def __init__(self, db: Database, embedder: Embedder, settings: Settings):
        self.db = db
        self.embedder = embedder
        self.settings = settings

    def _retrieve(
        self, search_query: str, tenant: str, scope: str,
        search, trace: Trace, attempt: str,
        original_question: str | None = None, dense_probe: str | None = None,
    ) -> list[dict]:
        question = original_question or search_query
        with trace.stage("query_analysis", attempt=attempt) as details:
            parts = query_parts(search_query)
            details["part_count"] = len(parts)
        with trace.stage("embed_query", attempt=attempt):
            vectors = self.embedder.embed([dense_probe or part for part in parts])
        fused: dict[str, float] = defaultdict(float)
        candidates: dict[str, dict] = {}
        for part_index, (part, vector) in enumerate(zip(parts, vectors, strict=True)):
            expression = fts_expression(part)
            with trace.stage("lexical_search", attempt=attempt, part=part_index) as details:
                lexical = search.lexical_search(expression, tenant, scope, 20) if expression else []
                details["result_count"] = len(lexical)
            with trace.stage("dense_search", attempt=attempt, part=part_index) as details:
                dense = search.dense_search(vector, tenant, scope, 20)
                details["result_count"] = len(dense)
            with trace.stage("rrf_fusion", attempt=attempt, part=part_index) as details:
                for ranking in (lexical, dense):
                    for rank, row in enumerate(ranking, start=1):
                        fused[row["id"]] += 1 / (60 + rank)
                        candidates[row["id"]] = row
                details["fused_count"] = len(fused)
        with trace.stage("rerank", attempt=attempt) as details:
            ranked = []
            query_vector = self.embedder.embed([question])[0]
            for child_id, rrf in fused.items():
                row = candidates[child_id]
                similarity = cosine(query_vector, json.loads(row["embedding"]))
                coverage = _coverage(question, row["text"])
                row = dict(row)
                row["similarity"] = similarity
                row["coverage"] = coverage
                row["rerank_score"] = 0.55 * max(0.0, similarity) + 0.4 * coverage + 0.05 * min(1.0, 30 * rrf)
                ranked.append(row)
            details["candidate_count"] = len(ranked)
            return sorted(ranked, key=lambda row: row["rerank_score"], reverse=True)[:8]

    def _rewrite(self, question: str, trace: Trace) -> str:
        if os.getenv("OPENAI_API_KEY"):
            try:
                with trace.stage("query_rewrite_llm"):
                    from openai import OpenAI
                    client = OpenAI()
                    response = client.chat.completions.create(
                        model=self.settings.llm_model,
                        temperature=0,
                        messages=[
                            {"role": "system", "content": "Rewrite the question as a concise search query. Return only the query."},
                            {"role": "user", "content": question},
                        ],
                    )
                    rewritten = (response.choices[0].message.content or "").strip()
                    if rewritten:
                        return rewritten[:300]
            except Exception:
                pass
        with trace.stage("query_rewrite_heuristic"):
            return " ".join(sorted(keywords(question)))

    def _hypothetical(self, question: str, trace: Trace) -> str | None:
        if not os.getenv("OPENAI_API_KEY"):
            return None
        try:
            with trace.stage("hyde_generation"):
                from openai import OpenAI
                client = OpenAI()
                response = client.chat.completions.create(
                    model=self.settings.llm_model,
                    temperature=0,
                    messages=[
                        {"role": "system", "content": (
                            "Write a short hypothetical document passage that would answer the question. "
                            "It is only a retrieval probe, not evidence. Return only the passage."
                        )},
                        {"role": "user", "content": question},
                    ],
                )
                return (response.choices[0].message.content or "").strip()[:600] or None
        except Exception:
            return None

    def _enough(self, question: str, rows: list[dict]) -> bool:
        if not rows:
            return False
        best = rows[0]
        if best["rerank_score"] < 0.2 or best["coverage"] < 0.25:
            return False
        covered = set().union(*(keywords(row["text"]) for row in rows[:4]))
        requested = keywords(question)
        return bool(requested) and len(requested & covered) / len(requested) >= 0.5

    def _evidence(self, rows: list[dict], tenant: str, scope: str, search) -> list[dict]:
        selected = []
        seen = set()
        for row in rows:
            if row["parent_id"] not in seen and row["coverage"] >= 0.2:
                selected.append(row)
                seen.add(row["parent_id"])
            if len(selected) == 4:
                break
        parents = search.get_parents([row["parent_id"] for row in selected], tenant, scope)
        evidence = []
        for row in selected:
            parent = parents.get(row["parent_id"])
            if parent:
                evidence.append({
                    "citation": len(evidence) + 1,
                    "document_id": row["document_id"],
                    "version_id": row.get("version_id"),
                    "filename": row["filename"],
                    "section": parent["heading"],
                    "excerpt": parent["text"],
                    "matched_text": row["text"],
                    "parent_text": parent["text"],
                })
        return evidence

    def _extractive_answer(self, question: str, evidence: list[dict]) -> str:
        scored = []
        for item in evidence:
            sentences = re.split(r"(?<=[.!?])\s+", item["excerpt"])
            for sentence in sentences:
                if sentence.strip():
                    scored.append((_coverage(question, sentence), item["citation"], sentence.strip()))
        scored.sort(reverse=True)
        chosen = scored[:min(2, len(query_parts(question)))]
        return " ".join(f"{sentence} [{citation}]" for _, citation, sentence in chosen)

    def _grounded(self, answer: str, evidence: list[dict]) -> bool:
        by_number = {item["citation"]: item["parent_text"] for item in evidence}
        clauses = [clause.strip() for clause in re.split(r"(?<=[.!?])\s+|\n+", answer) if clause.strip()]
        if not clauses:
            return False
        for clause in clauses:
            refs = [int(value) for value in CITATION.findall(clause)]
            if not refs or any(ref not in by_number for ref in refs):
                return False
            source = " ".join(by_number[ref] for ref in refs)
            unsupported_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", CITATION.sub("", clause))) - set(
                re.findall(r"\b\d+(?:\.\d+)?\b", source)
            )
            if unsupported_numbers:
                return False
            claim_words = keywords(CITATION.sub("", clause))
            if claim_words and len(claim_words & keywords(source)) / len(claim_words) < 0.45:
                return False
        return True

    def _generate(self, question: str, evidence: list[dict], trace: Trace) -> tuple[str, str]:
        if os.getenv("OPENAI_API_KEY"):
            try:
                with trace.stage("llm_generation"):
                    from openai import OpenAI
                    client = OpenAI()
                    context = "\n\n".join(
                        f"[{item['citation']}] {item['filename']} / {item['section']}\n{item['parent_text']}"
                        for item in evidence
                    )
                    response = client.chat.completions.create(
                        model=self.settings.llm_model,
                        temperature=0,
                        messages=[
                            {"role": "system", "content": (
                                "Answer using only the numbered evidence. Cite every sentence with [n]. "
                                "Do not add facts that the evidence does not state. If it cannot answer, say so."
                            )},
                            {"role": "user", "content": f"Question: {question}\n\nEvidence:\n{context}"},
                        ],
                    )
                    answer = (response.choices[0].message.content or "").strip()
                with trace.stage("grounding_check") as details:
                    accepted = self._grounded(answer, evidence)
                    details["accepted"] = accepted
                if accepted:
                    return answer, "llm"
            except Exception:
                pass
        with trace.stage("extractive_answer"):
            return self._extractive_answer(question, evidence), "extractive"

    def _passes_gate(self, question: str, rows: list[dict], trace: Trace, attempt: str) -> bool:
        with trace.stage("relevance_gate", attempt=attempt) as details:
            passed = self._enough(question, rows)
            details.update(candidate_count=len(rows), passed=passed)
            return passed

    def _search_attempt(self, search_query: str, question: str, tenant: str,
                        scope: str, trace: Trace, attempt: str,
                        dense_probe: str | None = None) -> list[dict]:
        # Each attempt pins one snapshot through both searches and parent
        # expansion. External rewrite/HyDE calls happen between attempts.
        with self.db.search_session(tenant, scope) as search:
            rows = self._retrieve(
                search_query, tenant, scope, search, trace, attempt,
                original_question=question, dense_probe=dense_probe,
            )
            if not self._passes_gate(question, rows, trace, attempt):
                return []
            with trace.stage("parent_expansion", attempt=attempt) as details:
                evidence = self._evidence(rows, tenant, scope, search)
                details["evidence_count"] = len(evidence)
            return evidence

    def _retrieve_evidence(self, question: str, tenant: str, scope: str,
                           trace: Trace) -> tuple[list[dict], str]:
        evidence = self._search_attempt(question, question, tenant, scope, trace, "initial")
        if evidence:
            return evidence, "hybrid"
        rewritten = self._rewrite(question, trace)
        if rewritten and rewritten.lower() != question.lower():
            evidence = self._search_attempt(rewritten, question, tenant, scope, trace, "rewrite")
            if evidence:
                return evidence, "rewrite+hybrid"
        hypothetical = self._hypothetical(question, trace)
        if hypothetical:
            evidence = self._search_attempt(
                question, question, tenant, scope, trace, "hyde", dense_probe=hypothetical,
            )
            if evidence:
                return evidence, "hyde+hybrid"
        return [], "hybrid"

    def answer(self, question: str, tenant: str, scope: str, trace: Trace | None = None) -> dict:
        trace = trace or Trace.new(self.db, tenant, scope, "query")
        with trace.stage("query_total") as total:
            question = question.strip()
            if not question or not keywords(question):
                total["status"] = "abstained"
                return {"answer": "Please ask a more specific question.", "status": "abstained",
                        "citations": [], "trace_id": trace.trace_id}
            evidence, strategy = self._retrieve_evidence(question, tenant, scope, trace)
            with trace.stage("evidence_sufficiency") as details:
                requested = keywords(question)
                supported = set().union(*(keywords(item["parent_text"]) for item in evidence))
                sufficient = bool(evidence) and len(requested & supported) / len(requested) >= 0.5
                details.update(evidence_count=len(evidence), passed=sufficient)
            if not sufficient:
                total.update(status="abstained", strategy=strategy)
                return {
                    "answer": "I could not find enough relevant evidence in the permitted documents.",
                    "status": "abstained", "citations": [], "strategy": strategy,
                    "trace_id": trace.trace_id,
                }
            answer, mode = self._generate(question, evidence, trace)
            with trace.stage("citation_binding") as details:
                used = {int(value) for value in CITATION.findall(answer)}
                citations = [
                    {key: value for key, value in item.items() if key != "parent_text"}
                    for item in evidence if item["citation"] in used
                ]
                details["citation_count"] = len(citations)
            total.update(status="answered", strategy=strategy, generation_mode=mode)
            return {
                "answer": answer, "status": "answered", "citations": citations,
                "strategy": strategy, "generation_mode": mode, "trace_id": trace.trace_id,
            }

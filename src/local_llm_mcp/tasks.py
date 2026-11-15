"""Logica dei tool. Indipendente da MCP: valori Python in ingresso e in uscita.

Errori:
- InputError / LLMError: input non valido o problema di API/rete -> il tool fallisce.
- Risposta del modello non valida anche dopo la richiesta di riscrittura -> {"ok": false,
  "error": {...}}, senza dati inventati.

Ogni risultato riuscito contiene i dati del modello già verificati dal server (citazioni, patch,
schema) e "stats" con il costo del compito.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from . import prompts as P
from .agent import LOW_EFFORT, Agent, AgentResult, Progress, add_usage
from .calllog import CallLog
from .citations import (
    citation_stats,
    find_quote,
    normalize_quote,
    verify_citation,
    verify_citations,
)
from .client import ChatResult, LLMClient, LLMError
from .config import Settings
from .diffs import code_text, evidence_line, new_file_lines, quoted_from, skip_generated_files
from .files import read_text_file, sensitive_reason
from .patches import check_patch
from .safety import InputError, ensure_max_chars, ensure_not_empty, redact_secrets, redaction_note
from .schemas import check_schema, json_validator, parse_json_answer, response_format
from .tracking import track
from .workspace import Workspace

# Effort predefinito per tool. Prove reali del 13/11/2026: review_changes con medium ha trovato
# gli stessi bug che con high in 46 s invece di 210; critique_plan con high ha superato i 500 s.
# high resta solo per le patch, dove un errore costa di più.
DEFAULT_EFFORTS = {
    "ask_codebase": "medium",
    "review_changes": "medium",
    "diagnose_error": "medium",
    "propose_change": "high",
    "write_tests": "medium",
    "critique_plan": "medium",
    "analyze_documents": "medium",
    "extract_structured_data": "low",
}
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
MAX_ISSUES = 30
MAX_PATHS = 20
MAX_DOCUMENTS = 20
MAX_LISTED_FILES = 50
MAX_HINT_CHARS = 300


class Tasks:
    def __init__(
        self, settings: Settings, client: LLMClient, call_log: CallLog | None = None
    ) -> None:
        self.settings = settings
        self.client = client
        self.call_log = call_log

    # --- tool con agente ----------------------------------------------------------------

    async def ask_codebase(
        self,
        question: str,
        root: str,
        paths: list[str] | None = None,
        effort: str | None = None,
        language: str | None = None,
        progress: Progress | None = None,
    ) -> dict[str, Any]:
        tool = "ask_codebase"
        question, found = self._text(question, "question")
        ws = self._workspace(root)
        user = P.ask_codebase_user(question, _hints(paths), language)
        async with self._track(tool, len(question), found, effort) as rec:
            data, result = await self._agent(rec, ws, user, P.ASK_CODEBASE_SCHEMA, progress)
            if data is None:
                return _invalid(result, rec)
            data["citations"] = verify_citations(data["citations"], ws.text_of)
            rec.update(citation_stats(data["citations"]))
            return self._ok(data, rec, result, ws, found)

    async def review_changes(
        self,
        root: str,
        diff: str | None = None,
        path: str | None = None,
        focus: str | None = None,
        effort: str | None = None,
        language: str | None = None,
        progress: Progress | None = None,
    ) -> dict[str, Any]:
        tool = "review_changes"
        raw = self._load(diff, path, "diff")
        kept, skipped = skip_generated_files(raw)
        ensure_not_empty(kept, "diff (senza lockfile e file generati)")
        kept, found = redact_secrets(kept)
        ws = self._workspace(root)
        user = P.review_user(kept, focus, language)
        async with self._track(tool, len(kept), found, effort) as rec:
            data, result = await self._agent(rec, ws, user, P.REVIEW_SCHEMA, progress)
            if data is None:
                return _invalid(result, rec)
            diff_code, lines = code_text(kept), new_file_lines(kept)
            issues = [_review_issue(i, diff_code, lines, ws) for i in data["issues"]]
            issues.sort(key=lambda i: SEVERITY_RANK.get(i["severity"], 9))
            payload: dict[str, Any] = {"summary": data["summary"], "issues": issues[:MAX_ISSUES]}
            if skipped:
                payload["skipped_files"] = skipped
            rec["citations_total"] = len(issues)
            rec["citations_verified"] = sum(1 for i in issues if i["evidence_found"])
            return self._ok(payload, rec, result, ws, found)

    async def diagnose_error(
        self,
        root: str,
        error: str | None = None,
        path: str | None = None,
        context: str | None = None,
        paths: list[str] | None = None,
        effort: str | None = None,
        language: str | None = None,
        progress: Progress | None = None,
    ) -> dict[str, Any]:
        tool = "diagnose_error"
        error_text, found = redact_secrets(self._load(error, path, "error"))
        context, found_context = self._optional_text(context, "context")
        found += found_context
        ws = self._workspace(root)
        user = P.diagnose_user(error_text, context, _hints(paths), language)
        async with self._track(tool, len(error_text), found, effort) as rec:
            data, result = await self._agent(rec, ws, user, P.DIAGNOSE_SCHEMA, progress)
            if data is None:
                return _invalid(result, rec)
            every: list[dict[str, Any]] = []
            for hypothesis in data["hypotheses"]:
                hypothesis["evidence"] = verify_citations(hypothesis["evidence"], ws.text_of)
                every += hypothesis["evidence"]
            rec.update(citation_stats(every))
            return self._ok(data, rec, result, ws, found)

    async def propose_change(
        self,
        task: str,
        root: str,
        constraints: str | None = None,
        paths: list[str] | None = None,
        effort: str | None = None,
        language: str | None = None,
        progress: Progress | None = None,
    ) -> dict[str, Any]:
        tool = "propose_change"
        task, found = self._text(task, "task")
        constraints, found_constraints = self._optional_text(constraints, "constraints")
        found += found_constraints
        ws = self._workspace(root)
        user = P.propose_user(task, constraints, _hints(paths), language)
        schema_check = json_validator(P.PROPOSE_SCHEMA)

        def validate(content: str) -> str | None:
            # Una patch che non si applica torna al modello una volta, con i problemi trovati.
            reason = schema_check(content)
            if reason is not None:
                return reason
            data, _ = parse_json_answer(content, P.PROPOSE_SCHEMA)
            check = check_patch(data["patch"], ws.text_of)
            return None if check["applies"] else "the patch does not apply: " + _problems(check)

        async with self._track(tool, len(task), found, effort) as rec:
            data, result = await self._agent(
                rec, ws, user, P.PROPOSE_SCHEMA, progress, validate=validate
            )
            if data is None:
                return _invalid(result, rec)
            check = check_patch(data["patch"], ws.text_of)
            rec["applies"] = int(check["applies"])
            data["citations"] = verify_citations(data["citations"], ws.text_of)
            rec.update(citation_stats(data["citations"]))
            payload = {
                "applies": check["applies"],
                # La diff corretta dal server quando si applica, altrimenti quella del modello.
                "patch": check["patch"] if check["applies"] else data["patch"],
                "files": check["files"],
                **{k: v for k, v in data.items() if k != "patch"},
            }
            if check.get("error"):
                payload["patch_error"] = check["error"]
            return self._ok(payload, rec, result, ws, found)

    async def write_tests(
        self,
        target: str,
        root: str,
        symbol: str | None = None,
        framework: str | None = None,
        effort: str | None = None,
        language: str | None = None,
        progress: Progress | None = None,
    ) -> dict[str, Any]:
        tool = "write_tests"
        ws = self._workspace(root)
        if ws.text_of(target) is None:
            raise InputError(f"'target' non è un file leggibile del progetto: {target}")
        user = P.write_tests_user(target, _short(symbol), _short(framework), language)
        async with self._track(tool, len(target), None, effort) as rec:
            data, result = await self._agent(rec, ws, user, P.WRITE_TESTS_SCHEMA, progress)
            if data is None:
                return _invalid(result, rec)
            existing = ws.text_of(data["test_path"]) is not None
            data["test_path_exists"] = existing
            if existing and data["mode"] == "new_file":
                data["warning"] = (
                    "test_path esiste già ma mode è 'new_file': non sovrascriverlo, unisci i test"
                )
            data["citations"] = verify_citations(data["citations"], ws.text_of)
            rec.update(citation_stats(data["citations"]))
            return self._ok(data, rec, result, ws, None)

    async def critique_plan(
        self,
        plan: str,
        root: str | None = None,
        context: str | None = None,
        paths: list[str] | None = None,
        effort: str | None = None,
        language: str | None = None,
        progress: Progress | None = None,
    ) -> dict[str, Any]:
        tool = "critique_plan"
        plan, found = self._text(plan, "plan")
        context, found_context = self._optional_text(context, "context")
        found += found_context
        ws = self._workspace(root) if root else None
        user = P.critique_user(plan, context, _hints(paths), ws is not None, language)
        async with self._track(tool, len(plan), found, effort) as rec:
            if ws is None:
                data, result = await self._single(rec, P.NO_TOOLS_SYSTEM, user, P.CRITIQUE_SCHEMA)
                if data is None:
                    return _invalid(result, rec)
                data["citations"] = []
                return self._ok(data, rec, result, None, found)
            data, result = await self._agent(rec, ws, user, P.CRITIQUE_SCHEMA, progress)
            if data is None:
                return _invalid(result, rec)
            data["citations"] = verify_citations(data["citations"], ws.text_of)
            rec.update(citation_stats(data["citations"]))
            return self._ok(data, rec, result, ws, found)

    # --- tool senza agente ----------------------------------------------------------------

    async def analyze_documents(
        self,
        question: str,
        text: str | None = None,
        paths: list[str] | None = None,
        effort: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        tool = "analyze_documents"
        question, found = self._text(question, "question")
        documents = self._documents(text, paths)
        redacted: list[tuple[str, str]] = []
        for name, content in documents:
            content, doc_found = redact_secrets(content)
            found += doc_found
            redacted.append((name, content))
        total = sum(len(c) for _, c in redacted)
        user = P.analyze_user(redacted, question, language)
        async with self._track(tool, total, found, effort) as rec:
            data, result = await self._single(rec, P.ANALYZE_SYSTEM, user, P.ANALYZE_SCHEMA)
            if data is None:
                return _invalid(result, rec)
            data["citations"] = [_document_citation(c, redacted) for c in data["citations"]]
            data["documents"] = [name for name, _ in redacted]
            rec.update(citation_stats(data["citations"]))
            return self._ok(data, rec, result, None, found)

    async def extract_structured_data(
        self,
        json_schema: dict[str, Any],
        text: str | None = None,
        path: str | None = None,
        instructions: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any]:
        tool = "extract_structured_data"
        check_schema(json_schema)
        content, found = redact_secrets(self._load(text, path, "text"))
        instructions, found_instructions = self._optional_text(instructions, "instructions")
        found += found_instructions
        user = P.extract_user(content, instructions)
        async with self._track(tool, len(content), found, effort) as rec:
            data, result = await self._single(rec, P.EXTRACT_SYSTEM, user, json_schema)
            if data is None:
                return _invalid(result, rec)
            return self._ok({"data": data}, rec, result, None, found)

    # --- motore ---------------------------------------------------------------------------

    async def _agent(
        self,
        rec: dict[str, Any],
        ws: Workspace,
        user: str,
        schema: dict[str, Any],
        progress: Progress | None,
        validate: Callable[[str], str | None] | None = None,
    ) -> tuple[dict[str, Any] | None, AgentResult]:
        agent = Agent(self.client, self.settings, ws, effort=rec["effort"], progress=progress)
        try:
            result = await agent.run(
                P.AGENT_SYSTEM,
                user,
                response_format=response_format(rec["tool"], schema),
                validate=validate or json_validator(schema),
            )
        finally:
            rec.update(
                files_read=len(ws.files_read),
                read_chars=ws.read_chars,
            )
            if ws.redactions:
                rec["redacted_secrets"] = rec.get("redacted_secrets", 0) + sum(
                    ws.redactions.values()
                )
        _record(rec, result)
        data, error = parse_json_answer(result.content, schema)
        if error:
            rec["error"] = error
        return data, result

    async def _single(
        self, rec: dict[str, Any], system: str, user: str, schema: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, AgentResult]:
        """Una richiesta senza funzioni; se la risposta non rispetta lo schema, una seconda con
        il motivo."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        out = AgentResult(content="", finish_reason=None, steps=0)
        fmt = response_format(rec["tool"], schema)
        data: dict[str, Any] | None = None
        error: str | None = None
        for attempt in range(2):
            chat = await self._chat(messages, fmt, rec["effort"])
            add_usage(out, chat)
            if chat.finish_reason == "length" and rec["effort"] != LOW_EFFORT:
                out.effort_lowered += 1
                chat = await self._chat(messages, fmt, LOW_EFFORT)
                add_usage(out, chat)
            _require_answer(chat)
            data, error = parse_json_answer(chat.content, schema)
            out.content, out.finish_reason = chat.content, chat.finish_reason
            if error is None:
                break
            if attempt == 0:
                out.reformatted = True
                messages += [
                    {"role": "assistant", "content": chat.content},
                    {
                        "role": "user",
                        "content": f"Your answer is not valid ({error}). Write it again as a "
                        "single JSON object that follows the schema, with the same content.",
                    },
                ]
        _record(rec, out)
        if error:
            rec["error"] = error
        return data, out

    async def _chat(
        self, messages: list[dict[str, Any]], fmt: dict[str, Any], effort: str
    ) -> ChatResult:
        return await self.client.chat(
            messages,
            model=self.settings.model,
            max_tokens=self.settings.glm_max_output_tokens,
            response_format=fmt,
            reasoning_effort=effort,
        )

    # --- input ----------------------------------------------------------------------------

    def _workspace(self, root: str) -> Workspace:
        roots = self.settings.allowed_read_roots
        if not roots:
            raise InputError(
                "Lettura dei progetti disattivata: imposta LOCAL_LLM_ALLOWED_READ_ROOTS"
            )
        path = Path(root).expanduser()
        if not path.is_absolute():
            raise InputError("'root' deve essere il percorso ASSOLUTO della cartella del progetto")
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, OSError):
            raise InputError(f"Cartella non trovata: {root}") from None
        if not resolved.is_dir():
            raise InputError(f"'root' non è una cartella: {root}")
        if not any(resolved.is_relative_to(r) for r in roots):
            raise InputError(
                "'root' è fuori dalle cartelle consentite (LOCAL_LLM_ALLOWED_READ_ROOTS): "
                + "; ".join(str(r) for r in roots)
            )
        if sensitive_reason(resolved / "x"):
            raise InputError("'root' è una cartella riservata")
        return Workspace(
            root=resolved,
            max_read_chars=self.settings.agent_max_read_chars,
            max_file_chars=self.settings.document_max_chars,
        )

    def _load(self, text: str | None, path: str | None, label: str) -> str:
        limit = self.settings.document_max_chars
        env = "LOCAL_LLM_DOCUMENT_MAX_CHARS"
        if text is not None and path is not None:
            raise InputError(f"Passa '{label}' oppure 'path', non entrambi")
        if path is not None:
            content = read_text_file(path, self.settings.allowed_read_roots, limit, env)
        elif text is not None:
            content = text
        else:
            raise InputError(f"Passa '{label}' oppure 'path'")
        ensure_not_empty(content, label)
        ensure_max_chars(len(content), limit, "path" if path is not None else label, env)
        return content

    def _text(self, text: str, label: str) -> tuple[str, Counter[str]]:
        ensure_not_empty(text, label)
        ensure_max_chars(
            len(text), self.settings.document_max_chars, label, "LOCAL_LLM_DOCUMENT_MAX_CHARS"
        )
        return redact_secrets(text)

    def _optional_text(self, text: str | None, label: str) -> tuple[str | None, Counter[str]]:
        if text is None or not text.strip():
            return None, Counter()
        return self._text(text, label)

    def _documents(self, text: str | None, paths: list[str] | None) -> list[tuple[str, str]]:
        if text is not None and paths:
            raise InputError("Passa 'text' oppure 'paths', non entrambi")
        if text is not None:
            ensure_not_empty(text, "text")
            documents = [("text", text)]
        elif paths:
            if len(paths) > MAX_DOCUMENTS:
                raise InputError(f"Al massimo {MAX_DOCUMENTS} documenti in 'paths'")
            limit = self.settings.document_max_chars
            documents = [
                (
                    Path(p).name,
                    read_text_file(
                        p, self.settings.allowed_read_roots, limit, "LOCAL_LLM_DOCUMENT_MAX_CHARS"
                    ),
                )
                for p in paths
            ]
        else:
            raise InputError("Passa 'text' oppure 'paths'")
        total = sum(len(t) for _, t in documents)
        ensure_max_chars(
            total, self.settings.document_max_chars, "documenti", "LOCAL_LLM_DOCUMENT_MAX_CHARS"
        )
        return documents

    # --- uscita ---------------------------------------------------------------------------

    @asynccontextmanager
    async def _track(
        self, tool: str, input_chars: int, found: Counter[str] | None, effort: str | None
    ) -> AsyncIterator[dict[str, Any]]:
        async with track(self.call_log, tool, self.settings.model, input_chars, found) as rec:
            rec["effort"] = effort or DEFAULT_EFFORTS.get(tool, self.settings.default_effort)
            yield rec

    def _ok(
        self,
        payload: dict[str, Any],
        rec: dict[str, Any],
        result: AgentResult,
        ws: Workspace | None,
        found: Counter[str] | None,
    ) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "effort": rec["effort"],
            "requests": result.steps,
            "prompt_tokens": result.usage["prompt_tokens"],
            "completion_tokens": result.usage["completion_tokens"],
        }
        if ws is not None:
            stats["function_calls"] = result.tool_calls
            stats["files_read"] = sorted(ws.files_read)[:MAX_LISTED_FILES]
            stats["read_chars"] = ws.read_chars
        if result.stopped:
            stats["stopped"] = result.stopped
        if result.effort_lowered:
            stats["effort_lowered"] = result.effort_lowered
        if "citations_total" in rec:
            stats["citations_verified"] = (
                f"{rec.get('citations_verified', 0)}/{rec['citations_total']}"
            )
        out: dict[str, Any] = {"ok": True, **payload, "model": self.settings.model}
        out["stats"] = stats
        redactions = Counter(found or {}) + (ws.redactions if ws is not None else Counter())
        note = redaction_note(redactions)
        if note:
            out["redaction_note"] = note
        rec["output_chars"] = len(result.content)
        return out


# --- funzioni di supporto ------------------------------------------------------------------


def _record(rec: dict[str, Any], result: AgentResult) -> None:
    rec.update(
        steps=result.steps,
        tool_calls=result.tool_calls,
        prompt_tokens=result.usage["prompt_tokens"],
        completion_tokens=result.usage["completion_tokens"],
        finish_reason=result.finish_reason,
        request_id=result.request_id,
    )
    if result.stopped:
        rec["stopped"] = result.stopped
    if result.effort_lowered:
        # Stessa colonna dei vecchi tool: un nuovo tentativo con meno ragionamento.
        rec["retried_without_reasoning"] = result.effort_lowered


def _invalid(result: AgentResult, rec: dict[str, Any]) -> dict[str, Any]:
    reason = rec.pop("error", "invalid output")
    rec["outcome"] = "invalid_output:schema"
    return {
        "ok": False,
        "error": {
            "code": "invalid_output",
            "message": f"La risposta del modello non è valida anche dopo un nuovo tentativo: "
            f"{reason}. Riprova, eventualmente con effort più alto, o fallo tu.",
            "request_id": result.request_id,
        },
    }


def _require_answer(chat: ChatResult) -> None:
    if chat.finish_reason == "length":
        raise LLMError(
            "Risposta troncata: il modello ha esaurito il limite di output (anche il ragionamento "
            "consuma token). Riduci l'input, usa effort più basso o aumenta "
            "LOCAL_LLM_MAX_OUTPUT_TOKENS",
            request_id=chat.request_id,
        )
    if not chat.content.strip():
        raise LLMError("Il modello ha restituito una risposta vuota", request_id=chat.request_id)


def _hints(paths: list[str] | None) -> list[str] | None:
    if not paths:
        return None
    if len(paths) > MAX_PATHS:
        raise InputError(f"Al massimo {MAX_PATHS} percorsi in 'paths'")
    return [p.strip()[:MAX_HINT_CHARS] for p in paths if p and p.strip()]


def _short(text: str | None) -> str | None:
    return text.strip()[:MAX_HINT_CHARS] if text and text.strip() else None


def _problems(check: dict[str, Any]) -> str:
    if check.get("error"):
        return check["error"]
    return "; ".join(
        f"{f['path']}: {', '.join(f['problems'])}" for f in check["files"] if f["problems"]
    )


def _review_issue(
    issue: dict[str, Any],
    diff_code: str,
    lines: dict[str, list[tuple[int, str]]],
    ws: Workspace,
) -> dict[str, Any]:
    """Il problema con la citazione cercata prima nella diff, poi nel file del progetto."""
    out = dict(issue)
    evidence = issue["evidence"]
    if quoted_from(evidence, diff_code):
        out["evidence_found"] = "diff"
        exact = evidence_line(evidence, lines.get(issue["file"].removeprefix("./"), []))
        if exact is not None:
            out["line"] = exact
        return out
    check = verify_citation(
        {"path": issue["file"], "line": issue["line"], "quote": evidence}, ws.text_of
    )
    if check["verified"]:
        out["evidence_found"] = "file"
        out["file"], out["line"] = check["path"], check["line"]
    else:
        out["evidence_found"] = None
    return out


def _document_citation(item: dict[str, Any], documents: list[tuple[str, str]]) -> dict[str, Any]:
    number = item.get("document")
    out = {"document": number, "quote": item.get("quote", ""), "verified": False}
    if not isinstance(number, int) or not 1 <= number <= len(documents):
        out["problem"] = "unknown document number"
        return out
    name, text = documents[number - 1]
    out["name"] = name
    hits = find_quote(normalize_quote(out["quote"]), text) if out["quote"].strip() else []
    if hits:
        out["verified"] = True
        out["line"] = hits[0][0]
    else:
        out["problem"] = "quote not found in the document"
    return out

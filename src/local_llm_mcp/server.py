"""Server MCP: definisce i tool e le loro descrizioni. La logica è in tasks.py, l'HTTP in client.py."""

import argparse
import asyncio
import functools
import logging
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .calllog import CallLog
from .client import LLMClient, LLMError
from .config import ConfigError, Settings, load_env_file, obsolete_variables
from .safety import InputError
from .tasks import DEFAULT_EFFORTS, Tasks

SERVER_NAME = "local-llm"

# Arriva a Claude in ogni sessione: solo le regole generali, i dettagli stanno nelle descrizioni
# dei tool, che Claude legge quando li usa.
INSTRUCTIONS = """Tool che delegano a GLM, modello forte su codice e ragionamento, compiti che ti costerebbero molti token e molte letture di file: domande su un progetto, review, diagnosi di errori, patch, test, seconde opinioni su un piano, analisi di documenti, estrazione di dati. GLM esplora il progetto da solo, in sola lettura.
- Le citazioni sono verificate dal server ("verified"): fidati di quelle, ricontrolla il resto.
- Patch e test non vengono scritti su disco: applicarli, eseguirli e verificarli resta compito tuo, come le decisioni e la risposta finale.
- Per un progetto passa 'root' assoluto; per un file non ancora letto passa 'path' assoluto invece del testo. Una diff salvala prima in un file in una cartella consentita ma fuori dal repository, o passala come testo.
- Un compito con esplorazione dura da 30 secondi a qualche minuto: non delegare ciò che fai con una lettura o una ricerca.
- Non inviare segreti o dati personali: l'oscuramento è best-effort.
- Se un tool fallisce o dà {"ok": false}, riprova con effort più alto o fallo tu.
"""

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True
)


def _tool_errors[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Trasforma gli errori previsti in ToolError, con il messaggio leggibile per Claude."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except (InputError, LLMError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def create_server(
    settings: Settings, client: LLMClient | None = None, call_log: CallLog | None = None
) -> FastMCP:
    llm = client or LLMClient(settings)
    if call_log is None and settings.call_log is not None:
        call_log = CallLog(settings.call_log, settings.call_log_days)
    tasks = Tasks(settings, llm, call_log)

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield {}
        finally:
            await llm.aclose()

    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS, version=__version__, lifespan=lifespan)

    roots = "; ".join(str(root) for root in settings.allowed_read_roots)
    path_help = (
        "Percorso ASSOLUTO di un file di testo (UTF-8 o UTF-16 con BOM) da leggere al posto del "
        f"contenuto testuale (sola lettura, solo nelle cartelle consentite: {roots}). "
        "Preferiscilo per contenuti lunghi che non hai ancora letto."
        if roots
        else "Lettura da file disattivata (LOCAL_LLM_ALLOWED_READ_ROOTS vuota): passa il "
        "contenuto come testo."
    )

    glm_model = settings.model
    max_doc = f"{settings.document_max_chars:,}".replace(",", ".")
    root_help = (
        "Percorso ASSOLUTO della cartella del progetto (la radice del repository). GLM vi legge "
        "i file da solo, in sola lettura, senza vedere .env, chiavi e .git. Deve stare nelle "
        f"cartelle consentite: {roots}."
        if roots
        else "Lettura dei progetti disattivata (LOCAL_LLM_ALLOWED_READ_ROOTS vuota): il tool "
        "fallisce."
    )
    paths_help = (
        "Percorsi relativi alla radice da cui GLM può partire (file o cartelle che conosci già). "
        "Facoltativo: GLM sa cercare da solo."
    )
    language_help = (
        "Lingua dei testi liberi della risposta, es. 'italiano'. Default inglese, più compatto: "
        "passala solo se mostrerai il testo all'utente così com'è."
    )

    def effort_help(tool: str) -> str:
        return (
            "Quanto ragiona GLM prima di rispondere: low, medium, high. Più alto è più accurato, "
            f"più lento e consuma più token. Default: {DEFAULT_EFFORTS[tool]}."
        )

    Effort = Literal["low", "medium", "high"]
    verified_note = (
        "Le citazioni {path, line, quote} sono cercate dal server nei file: 'verified': true vuol "
        "dire che la riga citata esiste (righe corrette in 'line_corrected'), false che il "
        "modello l'ha inventata o sbagliata. Fidati delle citazioni verificate, ricontrolla il "
        "resto. 'stats' riporta file letti, passi e token."
    )

    def report(ctx: Context):
        async def progress(step: int, total: int, message: str) -> None:
            await ctx.report_progress(step, total, f"GLM: {message}")

        return progress

    @mcp.tool(
        description=(
            f"Fa rispondere {glm_model} (modello forte su codice e ragionamento) a una domanda su "
            "un progetto: esplora da solo il codice in sola lettura (elenca, cerca, legge) e "
            "restituisce una risposta compatta con le citazioni. Usalo al posto di leggere tu "
            "molti file: 'come funziona X', 'dove si gestisce Y', 'chi chiama Z', 'cosa succede "
            "se...'. Non usarlo per domande che richiedono di eseguire codice, per file che hai "
            "già nel contesto o per una sola riga che troveresti con una ricerca. Restituisce "
            '{"ok": true, "answer", "citations": [{path, line, quote, verified}], "open_points" '
            '(ciò che non ha potuto verificare), "stats"}. ' + verified_note
        ),
        annotations=READ_ONLY,
        output_schema=None,
    )
    @_tool_errors
    async def ask_codebase(
        question: Annotated[
            str, Field(min_length=1, description="La domanda, completa e autosufficiente.")
        ],
        root: Annotated[str, Field(description=root_help)],
        ctx: Context,
        paths: Annotated[list[str] | None, Field(description=paths_help)] = None,
        effort: Annotated[Effort | None, Field(description=effort_help("ask_codebase"))] = None,
        language: Annotated[str | None, Field(max_length=40, description=language_help)] = None,
    ) -> dict[str, Any]:
        return await tasks.ask_codebase(question, root, paths, effort, language, report(ctx))

    @mcp.tool(
        description=(
            f"Review approfondita di una diff con {glm_model}: legge anche il resto del progetto "
            "(chiamanti, definizioni, test) per capire l'impatto delle modifiche. Cerca bug, "
            "regressioni, contratti rotti, errori non gestiti e problemi di sicurezza, non lo "
            "stile. Usalo prima di un commit o di una PR, al posto di rileggere tu i file "
            "toccati. Passa la diff come testo o salvala in un file e passa 'path' (assoluto, in "
            "una cartella consentita ma fuori dal repository). Restituisce "
            '{"ok": true, "summary", "issues": [{file, line, severity '
            "(critical|high|medium|low), title, explanation, evidence, suggestion, "
            'evidence_found}], "skipped_files", "stats"}. evidence_found indica dove il server '
            "ha trovato la citazione: 'diff', 'file' oppure null (citazione assente: probabile "
            "falso positivo). I problemi sono ipotesi forti: verifica quelli che correggi. "
            "Lockfile e file generati sono esclusi. "
            f"Limite: {max_doc} caratteri."
        ),
        annotations=READ_ONLY,
        output_schema=None,
    )
    @_tool_errors
    async def review_changes(
        root: Annotated[str, Field(description=root_help)],
        ctx: Context,
        diff: Annotated[
            str | None, Field(description="La diff unified (alternativa a 'path').")
        ] = None,
        path: Annotated[str | None, Field(description=path_help)] = None,
        focus: Annotated[
            str | None,
            Field(max_length=300, description="Aspetto su cui concentrarsi, es. 'sicurezza'."),
        ] = None,
        effort: Annotated[Effort | None, Field(description=effort_help("review_changes"))] = None,
        language: Annotated[str | None, Field(max_length=40, description=language_help)] = None,
    ) -> dict[str, Any]:
        return await tasks.review_changes(root, diff, path, focus, effort, language, report(ctx))

    @mcp.tool(
        description=(
            f"Diagnosi di un errore con {glm_model}: dato uno stack trace, un messaggio d'errore "
            "o un log, segue il codice del progetto fino alla causa e restituisce ipotesi "
            "ordinate con prove e verifiche. Usalo per errori il cui percorso attraversa più "
            "file, invece di aprirli tu uno per uno. Non esegue codice né comandi: se servono "
            "dati di runtime, li propone in how_to_confirm. Passa l'errore come testo o 'path' a "
            'un file di log. Restituisce {"ok": true, "summary", "hypotheses": [{cause, '
            "confidence (high|medium|low), evidence: [citazioni], how_to_confirm, fix}], "
            '"open_points", "stats"}. ' + verified_note
        ),
        annotations=READ_ONLY,
        output_schema=None,
    )
    @_tool_errors
    async def diagnose_error(
        root: Annotated[str, Field(description=root_help)],
        ctx: Context,
        error: Annotated[
            str | None,
            Field(description="Stack trace, messaggio d'errore o log (alternativo a 'path')."),
        ] = None,
        path: Annotated[str | None, Field(description=path_help)] = None,
        context: Annotated[
            str | None,
            Field(description="Cosa stavi facendo, cosa ti aspettavi, cosa hai già escluso."),
        ] = None,
        paths: Annotated[list[str] | None, Field(description=paths_help)] = None,
        effort: Annotated[Effort | None, Field(description=effort_help("diagnose_error"))] = None,
        language: Annotated[str | None, Field(max_length=40, description=language_help)] = None,
    ) -> dict[str, Any]:
        return await tasks.diagnose_error(
            root, error, path, context, paths, effort, language, report(ctx)
        )

    @mcp.tool(
        description=(
            f"Fa scrivere a {glm_model} la patch per una modifica al progetto: legge il codice "
            "coinvolto e i chiamanti e restituisce una unified diff. NON modifica file: il "
            "server prova la diff in memoria sui file attuali ('applies') e, se si applica, la "
            "restituisce corretta e pronta per 'git apply'; se non si applica, dopo un nuovo "
            "tentativo, restituisce quella del modello con i problemi in 'files'. Usalo per "
            "modifiche ben definite che toccano codice che non hai ancora letto: rileggi la "
            "patch prima di applicarla. Non usarlo per decisioni di design (usa critique_plan) "
            'o modifiche di una riga. Restituisce {"ok": true, "applies", "patch", "files": '
            '[{path, status, hunks, problems}], "rationale", "risks", "tests_to_run", '
            '"citations", "stats"}.'
        ),
        annotations=READ_ONLY,
        output_schema=None,
    )
    @_tool_errors
    async def propose_change(
        task: Annotated[
            str,
            Field(
                min_length=1,
                description="La modifica da fare: cosa, dove se lo sai, comportamento atteso.",
            ),
        ],
        root: Annotated[str, Field(description=root_help)],
        ctx: Context,
        constraints: Annotated[
            str | None,
            Field(description="Vincoli: API da non cambiare, stile, dipendenze vietate..."),
        ] = None,
        paths: Annotated[list[str] | None, Field(description=paths_help)] = None,
        effort: Annotated[Effort | None, Field(description=effort_help("propose_change"))] = None,
        language: Annotated[str | None, Field(max_length=40, description=language_help)] = None,
    ) -> dict[str, Any]:
        return await tasks.propose_change(
            task, root, constraints, paths, effort, language, report(ctx)
        )

    @mcp.tool(
        description=(
            f"Fa scrivere a {glm_model} i test per un file o una funzione: trova i test "
            "esistenti e ne segue framework, struttura e convenzioni, legge il codice per "
            "aspettative corrette. NON scrive file: restituisce il contenuto da salvare. Usalo "
            "per test di codice che non hai ancora letto; eseguili sempre prima di fidarti. "
            'Restituisce {"ok": true, "test_path", "mode" (new_file|append: in append il '
            'contenuto va aggiunto al file esistente), "test_path_exists", "content", '
            '"framework", "run_command", "cases", "assumptions", "citations", "stats"}.'
        ),
        annotations=READ_ONLY,
        output_schema=None,
    )
    @_tool_errors
    async def write_tests(
        target: Annotated[
            str,
            Field(min_length=1, description="Percorso relativo alla radice del file da testare."),
        ],
        root: Annotated[str, Field(description=root_help)],
        ctx: Context,
        symbol: Annotated[
            str | None, Field(description="Funzione, classe o metodo da testare (facoltativo).")
        ] = None,
        framework: Annotated[
            str | None,
            Field(description="Framework da usare, se il progetto non ne ha già uno."),
        ] = None,
        effort: Annotated[Effort | None, Field(description=effort_help("write_tests"))] = None,
        language: Annotated[str | None, Field(max_length=40, description=language_help)] = None,
    ) -> dict[str, Any]:
        return await tasks.write_tests(
            target, root, symbol, framework, effort, language, report(ctx)
        )

    @mcp.tool(
        description=(
            f"Seconda opinione critica di {glm_model} su un piano, un design o una decisione "
            "tecnica: assunzioni sbagliate, rischi, casi dimenticati, alternative più semplici. "
            "Con 'root' verifica il piano sul codice reale del progetto (consigliato). Usalo "
            "prima di implementare piani non banali; le decisioni restano tue e dell'utente. "
            'Restituisce {"ok": true, "verdict" (sound|needs_changes|flawed), "summary", '
            '"issues": [{issue, severity, suggestion}], "missing_cases", "alternatives", '
            '"questions", "citations", "stats"}.'
        ),
        annotations=READ_ONLY,
        output_schema=None,
    )
    @_tool_errors
    async def critique_plan(
        plan: Annotated[str, Field(min_length=1, description="Il piano completo da valutare.")],
        ctx: Context,
        root: Annotated[
            str | None,
            Field(description=root_help + " Facoltativo: senza, nessun accesso al codice."),
        ] = None,
        context: Annotated[
            str | None, Field(description="Obiettivo, vincoli, cosa è già deciso.")
        ] = None,
        paths: Annotated[list[str] | None, Field(description=paths_help)] = None,
        effort: Annotated[Effort | None, Field(description=effort_help("critique_plan"))] = None,
        language: Annotated[str | None, Field(max_length=40, description=language_help)] = None,
    ) -> dict[str, Any]:
        return await tasks.critique_plan(plan, root, context, paths, effort, language, report(ctx))

    @mcp.tool(
        description=(
            f"Analisi di uno o più documenti lunghi con {glm_model}: domande, riassunti, "
            "confronti tra documenti o versioni, con citazioni verificate dal server nel testo. "
            "Usalo per documenti, specifiche, verbali o log che non vuoi caricare nel tuo "
            "contesto: passa 'paths' (assoluti, nelle cartelle consentite) invece del testo. Il "
            "modello risponde solo da quello che c'è nei documenti. Restituisce "
            '{"ok": true, "answer", "citations": [{document, name, quote, verified, line}], '
            '"not_found" (parti della domanda senza risposta nei documenti), "documents", '
            f'"stats"}}. Limite: {max_doc} caratteri in tutto, al massimo 20 documenti.'
        ),
        annotations=READ_ONLY,
        output_schema=None,
    )
    @_tool_errors
    async def analyze_documents(
        question: Annotated[
            str,
            Field(
                min_length=1,
                description="Cosa fare: domanda, riassunto (con il focus), confronto...",
            ),
        ],
        text: Annotated[
            str | None, Field(description="Un documento come testo (alternativo a 'paths').")
        ] = None,
        paths: Annotated[
            list[str] | None,
            Field(description="Percorsi ASSOLUTI dei documenti (alternativo a 'text')."),
        ] = None,
        effort: Annotated[
            Effort | None, Field(description=effort_help("analyze_documents"))
        ] = None,
        language: Annotated[str | None, Field(max_length=40, description=language_help)] = None,
    ) -> dict[str, Any]:
        return await tasks.analyze_documents(question, text, paths, effort, language)

    @mcp.tool(
        description=(
            f"Estrae dati da un testo con {glm_model} in un JSON che rispetta il JSON Schema "
            "che passi (oggetti annidati, liste, enum): il server valida la risposta contro lo "
            "schema e, se non è valida dopo un nuovo tentativo, restituisce "
            '{"ok": false, "error"}. Usalo per fatture, contratti, verbali, log, configurazioni: '
            "per copiare valori, non per calcolarli. I valori assenti sono null se lo schema lo "
            "consente. Passa 'path' oppure 'text'. Restituisce "
            '{"ok": true, "data", "stats"}. Ricontrolla nella fonte i valori critici (importi, '
            f"date, codici). Limite: {max_doc} caratteri."
        ),
        annotations=READ_ONLY,
        output_schema=None,
    )
    @_tool_errors
    async def extract_structured_data(
        json_schema: Annotated[
            dict[str, Any],
            Field(
                description='JSON Schema dell\'oggetto da restituire, con "type": "object" e '
                "le descrizioni dei campi (migliorano la precisione)."
            ),
        ],
        text: Annotated[
            str | None, Field(description="Testo sorgente (alternativo a 'path').")
        ] = None,
        path: Annotated[str | None, Field(description=path_help)] = None,
        instructions: Annotated[
            str | None,
            Field(description="Indicazioni aggiuntive, es. formato delle date o unità."),
        ] = None,
        effort: Annotated[
            Effort | None, Field(description=effort_help("extract_structured_data"))
        ] = None,
    ) -> dict[str, Any]:
        return await tasks.extract_structured_data(json_schema, text, path, instructions, effort)

    return mcp


def setup_logging(level: str) -> None:
    """Log su stderr: stdout è riservato al protocollo MCP."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("local_llm_mcp")
    logger.handlers[:] = [handler]
    logger.setLevel(level)
    logger.propagate = False
    logging.getLogger("httpx").setLevel(logging.WARNING)


# Parametri senza i quali i tool non funzionano. reasoning_effort no: i modelli che non lo
# dichiarano lo accettano comunque (prove del 14/11/2026, docs/piano-modelli-2026-11-14.md).
REQUIRED_PARAMETERS = ("tools", "response_format")
# Stima prudente per codice e testo misto: un token ogni 3 caratteri.
CHARS_PER_TOKEN = 3


def check_model(model: dict[str, Any], settings: Settings) -> tuple[list[str], list[str]]:
    """Parametri obbligatori mancanti e avvisi sul modello scelto, dai dati di GET /models."""
    params = set(model.get("supported_parameters") or [])
    missing = [p for p in REQUIRED_PARAMETERS if p not in params]
    warnings = []
    if "reasoning_effort" not in params:
        warnings.append(
            "reasoning_effort non dichiarato: viene inviato lo stesso, ma 'effort' può non avere "
            "effetto"
        )
    context = model.get("context_length")
    if isinstance(context, int) and 0 < context <= settings.glm_max_output_tokens:
        warnings.append(
            f"LOCAL_LLM_GLM_MAX_OUTPUT_TOKENS ({settings.glm_max_output_tokens}) non sta nel "
            f"contesto del modello ({context} token): ogni richiesta verrebbe rifiutata"
        )
    elif isinstance(context, int) and context > 0:
        room = context - settings.glm_max_output_tokens
        fits = room * CHARS_PER_TOKEN // 1000 * 1000
        too_big = [
            name
            for name, value in (
                ("LOCAL_LLM_DOCUMENT_MAX_CHARS", settings.document_max_chars),
                ("LOCAL_LLM_AGENT_MAX_READ_CHARS", settings.agent_max_read_chars),
            )
            if value > fits
        ]
        if too_big:
            warnings.append(
                f"{' e '.join(too_big)} oltre il contesto del modello ({context} token, di cui "
                f"{settings.glm_max_output_tokens} per la risposta): usa al massimo {fits} "
                "caratteri, o i documenti grandi falliranno"
            )
    return missing, warnings


async def check(settings: Settings) -> int:
    """Verifica configurazione e raggiungibilità dell'API. Restituisce il codice di uscita."""
    client = LLMClient(settings)
    try:
        models = await client.list_models()
    except LLMError as exc:
        print(f"ERRORE: {exc}")
        return 1
    finally:
        await client.aclose()
    print(f"OK: {settings.base_url} raggiungibile, {len(models)} modelli disponibili.")
    model = next((m for m in models if m["id"].casefold() == settings.model.casefold()), None)
    if model is None:
        print(f"  modello: {settings.model} -> NON presente nell'elenco (LOCAL_LLM_MODEL)")
        return 1
    missing, warnings = check_model(model, settings)
    print(
        f"  modello: {settings.model} -> presente, contesto {model.get('context_length')} token"
        + (f"; parametri NON dichiarati: {', '.join(missing)}" if missing else "")
    )
    for warning in warnings:
        print(f"  attenzione: {warning}")
    roots = "; ".join(str(r) for r in settings.allowed_read_roots) or "disattivata"
    print(f"  cartelle leggibili: {roots}")
    print(
        f"  effort predefinito: {settings.default_effort}  passi: {settings.agent_max_steps}  "
        f"lettura: {settings.agent_max_read_chars} caratteri  durata: "
        f"{settings.task_timeout_seconds:g}s"
    )
    print(
        f"  tier: {settings.tier or 'default della chiave'}  timeout per richiesta: "
        f"{settings.timeout_seconds:g}s  output: {settings.glm_max_output_tokens} token"
    )
    print(f"  registro chiamate: {settings.call_log or 'disattivato'}")
    json_mode = "a ogni passo" if settings.json_while_exploring else "solo alla fine"
    print(f"  risposta JSON dell'agente: {json_mode} (LOCAL_LLM_JSON_WHILE_EXPLORING)")
    obsolete = obsolete_variables()
    if obsolete:
        print(f"  variabili non più usate (si possono togliere da .env): {', '.join(obsolete)}")
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="local-llm-mcp",
        description="Server MCP (stdio) che delega a GLM compiti su codice e documenti.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verifica configurazione e connessione all'API, poi esce",
    )
    args = parser.parse_args(argv)
    # Su Windows, se rediretti, stdout/stderr userebbero la codepage locale (accenti illeggibili).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    load_env_file()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"[local-llm-mcp] Configurazione non valida: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    setup_logging(settings.log_level)

    if args.check:
        raise SystemExit(asyncio.run(check(settings)))
    create_server(settings).run(show_banner=False)


if __name__ == "__main__":
    main()

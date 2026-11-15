# local-llm-mcp — guida per Claude Code

## Cos'è
Server MCP (stdio) che espone a Claude Code 8 tool per delegare a `glm-5.3-flash` (API
OpenAI-compatible) compiti su codice e documenti che costerebbero a Claude molti token:
`ask_codebase`, `review_changes`, `diagnose_error`, `propose_change`, `write_tests`,
`critique_plan`, `analyze_documents`, `extract_structured_data`. GLM esplora i progetti da solo in
sola lettura (function calling); il server verifica citazioni, patch e JSON prima di restituirli.
Documentazione utente: `README.md`. Piano: `docs/piano-glm.md`; test reali: `docs/report-test-myfuel-2026-11-14.md`; modello di riserva (`qwen3-coder-next`): `docs/report-riserva-2026-11-14.md`. Modello di
`CLAUDE.md` per chi usa il server: `docs/CLAUDE-delega.md`.

## Comandi
- `uv sync`: installa le dipendenze (su Windows fallisce se il server MCP è in esecuzione, perché
  `local-llm-mcp.exe` è bloccato: usa `uv run --no-sync ...` finché il server è attivo)
- `uv run pytest`: test (HTTP simulato, nessuna chiamata reale)
- `uv run ruff check src tests` e `uv run ruff format src tests`
- `uv run local-llm-mcp --check`: verifica configurazione, modello e parametri (solo `GET /models`)
- `uv run local-llm-dashboard`: dashboard web delle chiamate su `127.0.0.1:8765`
- Entry point: `local-llm-mcp` → `local_llm_mcp.server:main`

## Architettura
- `config.py`: `Settings` immutabile, letto solo da variabili d'ambiente `LOCAL_LLM_*` (+ `.env`).
  `OBSOLETE_VARIABLES`: variabili dei vecchi tool, ignorate e segnalate da `--check`
- `client.py`: `LLMClient` su `httpx.AsyncClient`. Retry, rate limit (60 richieste di chat al
  minuto), errori e `tool_calls`; non sa nulla di MCP
- `workspace.py`: le funzioni in sola lettura offerte a GLM (`list_dir`, `read_file`,
  `search_text`, `file_outline`) dentro una radice consentita, con budget di caratteri
- `agent.py`: ciclo di function calling con limiti di passi, caratteri e tempo; al limite forza la
  risposta finale (`tool_choice: "none"`), una riformattazione se il JSON non è valido, un nuovo
  tentativo con effort low se la risposta è troncata
- `tasks.py`: logica degli 8 compiti, indipendente da MCP (`Tasks`, `DEFAULT_EFFORTS`)
- `prompts.py`: prompt (in inglese) e JSON schema delle risposte
- `citations.py`, `patches.py`, `schemas.py`: verifiche lato server di citazioni, diff e JSON
- `diffs.py`: parsing delle diff (righe del nuovo file, file generati da escludere)
- `server.py`: solo definizioni e descrizioni dei tool. Converte `InputError`/`LLMError` in `ToolError`
- `safety.py` e `files.py`: segreti, limiti, lettura file
- `calllog.py` + `tracking.py`: registro SQLite delle chiamate (solo i campi in `FIELDS`) e query
  della dashboard. Una riga per chiamata di tool; un errore del registro non fa mai fallire un tool
- `dashboard.py` + `dashboard.html`: server web stdlib su 127.0.0.1, processo separato dal server MCP

## Regole
- Un tool = una responsabilità, con parametri espliciti. Niente pattern "portmanteau" con `operation`.
- Le descrizioni dei tool sono il criterio con cui Claude decide: devono dire quando usarli, quando
  no e che formato restituiscono.
- `INSTRUCTIONS` (`server.py`) arriva a Claude in ogni sessione: solo regole generali e brevi. Le
  avvertenze di un singolo tool vanno nella sua descrizione, letta solo quando il tool serve.
- I tool non scrivono file, non eseguono comandi, non contattano host diversi da `LOCAL_LLM_BASE_URL`.
  Anche le funzioni date a GLM sono in sola lettura e confinate nella radice. L'unico file scritto
  dal server è il registro delle chiamate, con soli metadati.
- Mai segreti nel codice o nei file versionati: la chiave sta in `.env`, che è escluso da git.
  GLM non vede `.env`, chiavi e `.git`; il testo letto passa per `redact_secrets`.
- Verso l'API si inviano solo parametri documentati, perché l'API rifiuta con 400 i campi sconosciuti.
  `reasoning_effort` va sempre inviato: senza, GLM ragiona più che con `high`.
- La lingua della risposta va nel messaggio utente: con `response_format` e senza indicazioni GLM
  può rispondere in cinese.
- Output del modello non valido nei tool JSON: `{"ok": false, "error": {...}}`, mai dati inventati.
  Citazioni non trovate restano con `verified: false`, non si eliminano.
- Log su stderr e solo metadati, mai il contenuto di richieste o risposte. Stdout è del protocollo MCP.
- Ogni tool ha test con `FakeAPI` (`tests/helpers.py`, `tool_call_response` per le chiamate di
  funzione): nessun test deve chiamare l'API reale.
- `Settings.call_log` vale `None` nei test: un test che vuole il registro passa `call_log=tmp_path/...`.

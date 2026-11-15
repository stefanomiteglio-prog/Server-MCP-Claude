# Piano: server MCP per delegare a GLM 5.3

Data: 13 novembre 2026. Stato: fasi 0–7 completate (risultati della fase 7 in [report-test-myfuel-2026-11-14.md](report-test-myfuel-2026-11-14.md)).

## Obiettivo

Sostituire i 6 tool attuali con un set nuovo, pensato per `glm-5.3-flash` (stessa API, stessa
chiave): compiti più complessi su codice e documenti, dove GLM è forte (codice, ragionamento,
function calling) e dove Claude risparmia più token. Il risparmio viene soprattutto da due cose:

1. **GLM esplora i file da solo** (function calling in sola lettura): Claude passa una domanda e una
   cartella, non il contenuto dei file.
2. **Risposte compatte e verificate dal server**: citazioni `file:riga` controllate sul file reale,
   patch controllate contro i file. Claude può fidarsi di ciò che è marcato `verified` senza
   rileggere tutto.

Fatti noti del modello (da `/models`): ragiona sempre (il ragionamento conta nei token), contesto
262.144 token, output fino a 32.768, `reasoning_effort` supportato, chiama funzioni. È **in prova**
dal 4 novembre e gira **in Svizzera** (gli altri modelli in Italia).

## Risultati della fase 0 (13 novembre 2026)

Chiamate reali a `glm-5.3-flash` (zona `CH-3`) con script fuori dal repository, più la
documentazione (`wiki.ai.itsincom.org`: parametri, chat-completions, output strutturato, rate
limit, errori, streaming).

| Verifica | Esito |
|---|---|
| `supported_parameters` | include `tools`, `tool_choice`, `parallel_tool_calls`, `response_format`, `reasoning_effort`, `reasoning` |
| Tool calling | funziona: `tool_calls` formato OpenAI, risultati con `role: "tool"` + `tool_call_id` accettati |
| Chiamate parallele | sì, più `read_file` nello stesso turno |
| Agente reale su `src/` di questo repo (effort medium) | 6 passi, 9 letture, **15,7 s**, 21.062 token in ingresso e 1.375 in uscita; **15 citazioni su 15 corrette** (righe e codice) |
| Latenza | 0,3–3 s per passo con effort low/medium; 4–6 s con high o senza `reasoning_effort` |
| `reasoning_effort` | senza parametro ragiona di più (617 token di output su una domanda banale), `high` 427, `none`/`low`/`medium` 50–70 |
| `max_completion_tokens` | massimo 32.768 (oltre: 422). La costante `API_MAX_OUTPUT_TOKENS = 8192` è superata |
| Limite troppo basso | `finish_reason: "length"` con contenuto vuoto (il ragionamento consuma tutto) |
| `response_format` json_schema | JSON valido e conforme, **ma risponde in cinese** senza indicazione di lingua, e in inglese con "in italiano" solo nel system prompt |
| `tools` + `response_format` insieme | accettato: il modello chiama prima i tool |
| Ragionamento visibile | nessun campo `reasoning_content` nella risposta |
| Cache del prompt | `cached_tokens: 0`: ogni passo dell'agente ripaga tutta la storia |

Conseguenze sul piano:

- **Più veloce del previsto**: timeout per richiesta 120 s e per tool 300 s bastano;
  `MCP_TOOL_TIMEOUT` probabilmente non va toccato.
- **Inviare sempre `reasoning_effort`**: default `medium` per esplorazione e risposte, `high` per
  `review_changes`, `diagnose_error`, `propose_change`; mai senza parametro (costa di più senza
  vantaggio misurato).
- **Lingua**: istruzione di lingua nel messaggio utente (non solo nel system) e parametro
  `language` in ogni tool con testo libero; da verificare nei test della fase 7.
- **Risposta finale strutturata** (verificato in fase 2): `response_format` si invia in ogni passo
  insieme a `tools`. GLM chiama comunque le funzioni e alla fine risponde in JSON valido. Inviandolo
  solo alla fine serviva una richiesta di riscrittura (73 s contro 41–45 s). Ai limiti di passi,
  caratteri o tempo l'ultima richiesta usa `tool_choice: "none"`: anche questa dà JSON valido.
- **Durata reale dell'agente** (fase 2, domanda su `src/` di questo repo): 4–7 passi, 41–45 s,
  23–31 mila token in ingresso, citazioni esatte 27 su 27 in tre prove.
- **Costo dei token in ingresso**: senza cache, il budget di caratteri letti (`AGENT_MAX_READ_CHARS`)
  conta più del numero di passi. Default 200.000 caratteri, e `read_file` a intervalli di righe.

## Nuovi tool MCP (8)

Tutti restituiscono JSON `{"ok": true, ..., "stats": {...}}` oppure `{"ok": false, "error": {...}}`.
`stats` = passi dell'agente, file letti, token, durata. Parametro comune `effort`
(`low|medium|high`, inviato come `reasoning_effort` solo se `/models` lo elenca).

| Tool | Input | Output | Agente |
|---|---|---|---|
| `ask_codebase` | `question`, `root`, `paths?` | risposta + `citations[{path, lines, quote, verified}]` | sì |
| `review_changes` | `diff` o `path`, `root`, `focus?` | `issues[{file, line, severity, evidence, evidence_in_diff, explanation}]`, file letti per contesto | sì |
| `diagnose_error` | `error` (testo o `path`), `root`, `context?` | ipotesi ordinate `{cause, confidence, evidence[citazioni], how_to_confirm}` | sì |
| `propose_change` | `task`, `root`, `paths?`, `constraints?` | `patch` unified diff, `applies` (controllo del server), `rationale`, `risks` | sì |
| `write_tests` | `target` (path + simbolo opzionale), `root`, `framework?` | contenuto del file di test, casi coperti, convenzioni seguite (da test esistenti) | sì |
| `analyze_documents` | `question`, `paths[]` o `text` | risposta + citazioni verificate; copre riassunto, confronto tra documenti/versioni, domande | no (input diretto) |
| `extract_structured_data` | `json_schema`, `paths[]` o `text` | `data` validato contro lo schema, `not_found` | no |
| `critique_plan` | `plan`, `root?`, `paths?` | `verdict`, `risks`, `missing_cases`, `alternatives`, con citazioni se ha letto il codice | opzionale |

Nessun tool scrive file o esegue comandi: patch e test sono **testo** che Claude verifica e applica.
`list_models` sparisce (un solo modello): la verifica va in `--check`.

## Agente in sola lettura (`agent.py`)

Loop lato server: messaggi → GLM → `tool_calls` → esecuzione locale → risultati → ... fino alla
risposta finale (JSON richiesto dal tool) o ai limiti.

Funzioni offerte a GLM (tutte dentro `LOCAL_LLM_ALLOWED_READ_ROOTS` e dentro il `root` del tool,
con le esclusioni già in `files.py`: `.env`, chiavi, `.git`, ecc.):

- `list_dir(path, depth)`: albero con dimensioni, salta `node_modules`, `.venv`, build, lockfile
- `read_file(path, start_line, end_line)`: righe numerate, segreti oscurati (`safety.py`)
- `search_text(pattern, path, glob)`: regex, max N risultati con `file:riga`
- `file_outline(path)`: definizioni (classi/funzioni) via regex per linguaggio, per non leggere file interi

Limiti (configurabili): passi massimi (default 25), caratteri letti totali (default 200.000),
durata totale (default 300 s). Al limite, ultimo messaggio a GLM: "rispondi con ciò che hai". Le
chiamate parallele di GLM nello stesso turno si eseguono tutte.

Il contenuto dei file è **dato, non istruzione** (nota nel system prompt). Una prompt injection in
un file può solo peggiorare la risposta: l'agente non ha funzioni con effetti.

Durante il loop il server invia notifiche di progresso MCP (`ctx.report_progress`), utili per
compiti lunghi.

## Verifiche lato server (anti-allucinazione)

- `citations.py`: ogni citazione `{path, lines, quote}` è confrontata col file (spazi normalizzati);
  se la quote è altrove nel file si corregge la riga; `verified: false` se non c'è.
- `patches.py`: applicazione **in memoria** della unified diff ai file letti (nessuna scrittura);
  `applies: false` + hunk che falliscono. Restituisce anche la diff corretta in `patch`: intestazioni
  `@@` ricalcolate, righe di contesto copiate dal file (spazi finali compresi) e contesto finale
  aggiunto agli hunk che non ne hanno, che altrimenti `git apply` ancora alla fine del file e
  rifiuta. I test confrontano il risultato con `git apply --check`.
- Schema JSON: validazione con la libreria `jsonschema` (nuova dipendenza) per
  `extract_structured_data` e per l'output di ogni tool. Output non valido: un nuovo tentativo con
  l'errore di validazione, poi `{"ok": false}`.
- Riuso: `new_file_lines`, `_quoted_from`, `skip_generated_files` di `tasks.py` per `review_changes`.

## Configurazione

- Via (ignorate se presenti, `--check` le elenca): `LOCAL_LLM_DEFAULT_MODEL`,
  `LOCAL_LLM_CODE_MODEL`, `LOCAL_LLM_EXCLUDED_MODELS`, `LOCAL_LLM_MAX_CHUNKS`,
  `LOCAL_LLM_REVIEW_CHUNK_CHARS`, `LOCAL_LLM_MAX_PROMPT_CHARS`, `LOCAL_LLM_MAX_INPUT_CHARS`,
  `LOCAL_LLM_MAX_OUTPUT_TOKENS`.
- Nuove: `LOCAL_LLM_MODEL=glm-5.3-flash`, `LOCAL_LLM_DEFAULT_EFFORT`, `LOCAL_LLM_AGENT_MAX_STEPS`,
  `LOCAL_LLM_AGENT_MAX_READ_CHARS`, `LOCAL_LLM_TASK_TIMEOUT_SECONDS` (intero tool),
  `LOCAL_LLM_DOCUMENT_MAX_CHARS` (default 600.000) e `LOCAL_LLM_GLM_MAX_OUTPUT_TOKENS` (default
  32.768, il massimo dell'API). Nomi nuovi e non quelli vecchi: un `.env` esistente con 60.000
  caratteri e 4096-8192 token troncherebbe i compiti di GLM.
- Restano: base URL, chiave, tier, retry, cartelle consentite, log, registro chiamate.

## Registro e dashboard

Nuovi campi in `FIELDS`: `steps`, `files_read`, `read_chars`, `tool_calls`, `effort`,
`citations_total`, `citations_verified`, `applies`. Via `chunks`, `failed_chunks`. `calllog`
aggiunge già le colonne mancanti; la dashboard mostra i nuovi campi (tasso di citazioni verificate
= indicatore di qualità del modello).

## Fasi

0. **Verifica dell'API** (chiamate reali, poche): `tools`/`tool_choice` documentati e accettati con
   GLM, `supported_parameters` del modello, limite reale di `max_tokens` (il codice dice 8192,
   `/models` dice 32.768), latenza tipica con `effort` low/high. Senza function calling il piano
   ripiega su "contesto passato da Claude".
1. `config.py` + `client.py`: nuove variabili, `tool_calls` in `ChatResult`, messaggi multi-turno.
2. `agent.py` + funzioni di workspace, con test `FakeAPI` che simula sequenze di `tool_calls`
   (path traversal, file esclusi, limiti di passi/caratteri/tempo, tool call malformate).
3. `citations.py`, `patches.py`, validazione schema, con test.
4. Tool MCP uno alla volta (prompt in `prompts.py`, descrizioni in `server.py`, test), nell'ordine:
   `ask_codebase`, `review_changes`, `diagnose_error`, `propose_change`, `write_tests`,
   `analyze_documents`, `extract_structured_data`, `critique_plan`.
5. Rimozione dei tool vecchi, del codice a blocchi e dei loro test.
6. Documentazione: `INSTRUCTIONS`, `README.md`, `CLAUDE.md`, `.env.example`,
   `docs/CLAUDE-delega.md` (quando delegare a GLM e quando no).
7. Test reali su MyFuel come il 21 ottobre (stessi input, bug inseriti, verità di riferimento) e
   nuovo report in `docs/`.

## Risultati della fase 4 (13 novembre 2026)

Prova reale degli 8 tool tramite un client MCP in memoria, su questo repository.

| Tool | Effort | Durata | Richieste | Token in/out | Citazioni verificate | Esito |
|---|---|---|---|---|---|---|
| `ask_codebase` | medium | 38 s | 9 | 66k / 3k | 14/14 | risposta completa e corretta |
| `review_changes` (2 bug inseriti) | high | 211 s | 12 | 96k / 20k | 2/2 | trovati entrambi |
| `review_changes` (stessa diff) | medium | 47 s | 10 | 63k / 4k | 2/2 | trovati entrambi |
| `diagnose_error` | high | 135 s | 19 | 183k / 13k | 14/14 | causa corretta |
| `propose_change` | high | 98 s | 18 | 83k / 9k | 6/6 | patch applicabile e corretta |
| `write_tests` | medium | 247 s | 10 | 73k / 22k | 6/6 | 12 test sensati |
| `critique_plan` | high | 504 s | 19 | 496k / 47k | 24/24 | critica utile, fermato dal limite di tempo |
| `analyze_documents` | medium | 19 s | 1 | 15k / 2k | 28/29 | confronto corretto |
| `extract_structured_data` | low | 2 s | 1 | 0,5k / 0,2k | — | dati esatti |

Correzioni fatte dopo la prova:
- Con `LOCAL_LLM_MAX_OUTPUT_TOKENS=4096` (valore dei vecchi tool) `propose_change` e
  `critique_plan` venivano troncati: i tool GLM hanno ora un limite proprio,
  `LOCAL_LLM_GLM_MAX_OUTPUT_TOKENS` (default 32.768), e una risposta troncata si ripete una volta
  con effort low (`stats.effort_lowered`).
- Effort predefinito `medium` per tutti tranne `propose_change` (`high`) ed
  `extract_structured_data` (`low`).
- Il limite di durata si controllava solo tra un passo e l'altro: un compito poteva superarlo di
  qualche passo (504 s contro 300). Corretto in fase 7 (vedi sotto). Senza cache del prompt il costo cresce con la storia: un compito che
  legge 140.000 caratteri costa circa 500.000 token in ingresso.

Indicazioni di `critique_plan` per la fase 5 (verificate sul codice):
- `LOCAL_LLM_MAX_OUTPUT_TOKENS` va unificato con `LOCAL_LLM_GLM_MAX_OUTPUT_TOKENS`, non solo
  portato a 16.384: dopo la rimozione dei vecchi tool nessuno lo leggerebbe più.
- `check()` e `model_help()` in `server.py` usano `default_model`, `code_model` ed
  `excluded_models`: vanno riscritti insieme alla rimozione da `Settings`.
- `glm_prompts.py` importa `SEVERITIES` e `wrap` da `prompts.py`: va fuso, non solo rinominato.

## Rischi

- **Modello in prova**: può cambiare o sparire. `--check` lo segnala; il nome è configurabile.
- **Dati in Svizzera**: i file letti dall'agente lasciano l'Italia. Le esclusioni di `files.py` e
  l'oscuramento dei segreti restano, ma vanno dette nel README.
- **Latenza e token di ragionamento**: misurati bassi (agente da 6 passi in 16 s), ma un compito
  grande può superare il minuto. Default `effort` per tool (vedi fase 0) e notifiche di progresso;
  `MCP_TOOL_TIMEOUT` va alzato solo se i test della fase 7 lo richiedono.
- **Rate limit** 60 richieste/minuto: un agente da 25 passi ne usa 25; il `RateLimiter` resta
  condiviso tra le chiamate.

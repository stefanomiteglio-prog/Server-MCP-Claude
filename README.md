# local-llm-mcp

Server MCP per **Claude Code** che delega a **GLM 5.3** (`glm-5.3-flash`, su un'API
**OpenAI-compatible** come ITS INCOM AI) compiti su codice e documenti che a Claude costerebbero
molti token: domande su un progetto, review, diagnosi di errori, patch, test, seconde opinioni,
analisi di documenti ed estrazione di dati.

Claude resta l'orchestratore: decide, applica le modifiche e scrive la risposta finale. GLM fa il
lavoro di lettura e di primo ragionamento, e il server controlla quello che restituisce.

- **GLM esplora da solo:** con `root` Claude passa una domanda e la cartella del progetto, non il
  contenuto dei file. GLM elenca, cerca e legge i file in sola lettura (function calling) e
  restituisce una risposta compatta.
- **Risultati verificati dal server:** ogni citazione `file:riga` viene cercata nel file reale
  (`verified`), le patch vengono applicate in memoria ai file attuali (`applies`), il JSON viene
  validato contro lo schema. Claude può fidarsi di ciò che è verificato senza rileggere tutto.
- **Nessun potere al modello:** GLM non modifica file, non esegue comandi, non va su internet, non
  vede `.env`, chiavi e `.git`.

```
Claude Code ──(MCP, stdio)──► local-llm-mcp ──(HTTPS)──► API OpenAI-compatible
                                   │    ▲                  POST /v1/chat/completions (tools)
                                   │    └── GLM chiede:     GET  /v1/models
                                   │        list_dir, read_file, search_text, file_outline
                                   └─ legge in sola lettura sotto LOCAL_LLM_ALLOWED_READ_ROOTS
                                      e verifica citazioni, patch e JSON
```

## Installazione su qualsiasi dispositivo

I passi sono gli stessi su Windows, macOS e Linux. Dove i comandi cambiano, trovi la variante per
ciascun sistema. Ripeti la procedura su ogni dispositivo da cui usi Claude Code.

### 1. Prerequisiti

| Cosa | A cosa serve | Dove trovarlo |
|---|---|---|
| Git | Clonare la repository | [git-scm.com/downloads](https://git-scm.com/downloads) (su macOS anche `xcode-select --install`) |
| Accesso a GitHub | La repository è privata | Vedi sotto |
| uv | Installa le dipendenze e, se manca, Python 3.12+ | Comandi qui sotto |
| Claude Code | Il client che usa il server | [claude.com/claude-code](https://claude.com/claude-code), poi fai il login |
| Una API key | Autenticarsi sull'API OpenAI-compatible | Dashboard ITS INCOM AI → *API keys* → *Create* |

**Installare uv**

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

macOS / Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Poi chiudi e riapri il terminale e controlla con `uv --version`.

**Accedere alla repository privata**

- **Windows:** Git for Windows include Git Credential Manager. Al primo `git clone` si apre il
  browser per il login a GitHub.
- **macOS / Linux:** il modo più semplice è [GitHub CLI](https://cli.github.com/). Esegui
  `gh auth login` e accetta di autenticare Git con le credenziali GitHub. In alternativa usa una
  chiave SSH collegata al tuo account e l'URL
  `git@github.com:stefanomiteglio-prog/Server-MCP-Claude.git`.

**Una API key per ogni dispositivo.** Conviene crearne una diversa per ciascun dispositivo: se ne
perdi uno, revochi solo quella chiave. La chiave non va mai copiata in file versionati.

### 2. Clona e installa

```bash
git clone https://github.com/stefanomiteglio-prog/Server-MCP-Claude.git
cd Server-MCP-Claude
uv sync
```

Poi esegui `pwd`, che funziona anche in PowerShell, e annota il percorso assoluto della cartella:
ti serve al passo 5.

### 3. Crea il file `.env`

Il file `.env` contiene la chiave e le impostazioni di quel dispositivo. Non è nella repository, è
escluso da git, quindi va creato su ogni dispositivo partendo dal modello:

```bash
cp .env.example .env
```

Il comando `cp` funziona in bash, zsh e PowerShell; nel Prompt dei comandi di Windows usa `copy`.
Apri `.env` con un editor e compila:

- **`LOCAL_LLM_API_KEY`:** la chiave di questo dispositivo. È obbligatoria.
- **`LOCAL_LLM_BASE_URL`:** è già compilata con l'URL di ITS INCOM AI.
- **`LOCAL_LLM_ALLOWED_READ_ROOTS`:** le cartelle dei tuoi progetti su questo dispositivo. Servono
  a quasi tutti i tool: GLM può esplorare solo un `root` dentro queste cartelle, e i file passati
  con `path` o `paths` devono stare qui. Se la lasci vuota funzionano solo `analyze_documents`,
  `extract_structured_data` e `critique_plan` con il testo passato direttamente.

  | Sistema | Separatore | Esempio |
  |---|---|---|
  | Windows | `;` | `LOCAL_LLM_ALLOWED_READ_ROOTS=C:\Users\nome\progetti;D:\lavoro` |
  | macOS | `:` | `LOCAL_LLM_ALLOWED_READ_ROOTS=/Users/nome/progetti` |
  | Linux | `:` | `LOCAL_LLM_ALLOWED_READ_ROOTS=/home/nome/progetti:/srv/lavoro` |

  Le cartelle devono esistere, altrimenti il server non parte. Aggiungi anche una cartella per i
  file temporanei fuori dai repository: vedi [Dove salvare le diff](#dove-salvare-le-diff).

Le altre variabili sono facoltative: le trovi in [Variabili d'ambiente](#variabili-dambiente).

### 4. Verifica la connessione

```bash
uv run local-llm-mcp --check
```

Il comando legge la configurazione e chiama solo `GET /v1/models`, che non consuma crediti.
Controlla che il modello esista e dichiari i parametri senza cui i tool non funzionano (`tools`,
`response_format`). Con un modello diverso da GLM aggiunge righe `attenzione:` se il modello non
dichiara `reasoning_effort` (viene inviato lo stesso, ma `effort` può non avere effetto) o se
`LOCAL_LLM_DOCUMENT_MAX_CHARS` / `LOCAL_LLM_AGENT_MAX_READ_CHARS` non stanno nel suo contesto,
con il valore massimo da usare. Output atteso:

```
OK: https://api.ai.itsincom.org/v1 raggiungibile, N modelli disponibili.
  modello: glm-5.3-flash -> presente, contesto 262144 token
  cartelle leggibili: <le cartelle di LOCAL_LLM_ALLOWED_READ_ROOTS>
  effort predefinito: medium  passi: 25  lettura: 200000 caratteri  durata: 300s
  tier: default della chiave  timeout per richiesta: 120s  output: 32768 token
  registro chiamate: <percorso del registro>
```

Se qualcosa non va, il comando stampa il motivo, per esempio una variabile mancante, una chiave
rifiutata, una cartella inesistente o un modello assente. Se nel `.env` restano variabili della
versione precedente, una riga in più le elenca: vengono ignorate e si possono togliere.

Non serve avviare il server a mano: lo lancia Claude Code (trasporto stdio) quando serve. Se lo
avvii con `uv run local-llm-mcp`, resta in attesa di messaggi MCP su stdin: esci con Ctrl+C.

### 5. Collega il server a Claude Code

Puoi scegliere fra due modi, anche entrambi.

**A. Solo dentro questa repository.** Non serve nessun comando: il file [`.mcp.json`](.mcp.json)
registra già il server `local-llm`. Avvia `claude` dalla cartella `Server-MCP-Claude` e, la prima
volta, approva il server quando Claude Code lo chiede.

**B. In tutti i progetti di questo dispositivo (consigliato).** Registra il server una volta per il
tuo utente, indicando il percorso assoluto annotato al passo 2.

Windows (PowerShell):

```powershell
claude mcp add --scope user local-llm -- uv run --quiet --directory "C:\Users\nome\Server-MCP-Claude" local-llm-mcp
```

macOS / Linux:

```bash
claude mcp add --scope user local-llm -- uv run --quiet --directory "$HOME/Server-MCP-Claude" local-llm-mcp
```

Da quel momento ogni chat di Claude Code, in qualsiasi cartella, ha i tool `local-llm`. Il file
`.env` viene letto sempre dalla cartella della repository, ovunque tu stia lavorando.

Per togliere la registrazione, oppure per rifarla se sposti la cartella:

```bash
claude mcp remove local-llm -s user
```

### 6. Prova da una chat

Da terminale:

```bash
claude mcp list
```

Deve comparire `local-llm: … ✓ Connected`. Poi apri una chat con `claude` in un progetto che sta
dentro `LOCAL_LLM_ALLOWED_READ_ROOTS` e:

1. digita `/mcp`: vedi lo stato del server e i suoi 8 tool;
2. chiedi *"chiedi a GLM come è organizzato questo progetto"*: Claude chiama `ask_codebase` con
   la cartella del progetto e riceve una risposta con citazioni verificate, senza leggere i file.

## Aggiornare e lavorare da più dispositivi

- **Ricevere gli aggiornamenti** su un dispositivo:

  ```bash
  git pull
  uv sync
  ```

  Poi apri una nuova chat di Claude Code: ogni sessione rilancia il server con il codice
  aggiornato. Su Windows `uv sync` fallisce se una sessione di Claude Code sta usando il server
  (il file `local-llm-mcp.exe` è bloccato): chiudile prima.
- **Pubblicare le modifiche** fatte su un dispositivo: `git add`, `git commit` e `git push`, poi
  `git pull` sugli altri.
- **Il file `.env` resta locale:** non viaggia con `git push` e `git pull`. Se aggiungi una variabile
  nuova a `.env.example`, aggiungila a mano nel `.env` di ogni dispositivo.
- **Se sposti la cartella della repository,** rifai la registrazione B del passo 5 con il nuovo
  percorso.

## Variabili d'ambiente

La configurazione è letta solo da variabili d'ambiente. Per comodità il server carica il file `.env`
nella radice della repository; le variabili già impostate nel sistema hanno la precedenza.

| Variabile | Default | Significato |
|---|---|---|
| `LOCAL_LLM_BASE_URL` | — (obbligatoria) | URL base dell'API, con `/v1`. Per ITS INCOM AI: `https://api.ai.itsincom.org/v1` |
| `LOCAL_LLM_API_KEY` | — (obbligatoria) | API key, inviata come `Authorization: Bearer …` |
| `LOCAL_LLM_MODEL` | `glm-5.3-flash` | Modello di tutti i tool. Deve dichiarare `tools` e `response_format`; per la riserva vedi [Se GLM non è più disponibile](#se-glm-non-è-più-disponibile) |
| `LOCAL_LLM_JSON_WHILE_EXPLORING` | `true` per `glm-*`, `false` per gli altri | Formato JSON a ogni passo dell'agente (`true`) o solo nella risposta finale (`false`, una richiesta in più). Serve `false` ai modelli che con il formato attivo rispondono senza esplorare |
| `LOCAL_LLM_DEFAULT_EFFORT` | `medium` | `reasoning_effort` (`low`, `medium`, `high`) dei tool senza un default proprio; ogni tool accetta anche `effort` |
| `LOCAL_LLM_AGENT_MAX_STEPS` | `25` | Richieste al modello per compito con esplorazione |
| `LOCAL_LLM_AGENT_MAX_READ_CHARS` | `200000` | Caratteri di file che GLM può leggere in un compito (minimo 1000) |
| `LOCAL_LLM_TASK_TIMEOUT_SECONDS` | `300` | Durata dell'esplorazione (minimo 10); la risposta finale ha al massimo `LOCAL_LLM_TIMEOUT_SECONDS` in più |
| `LOCAL_LLM_GLM_MAX_OUTPUT_TOKENS` | `32768` | Token generati per risposta, ragionamento compreso (da 1024 a 32768, il massimo dell'API) |
| `LOCAL_LLM_DOCUMENT_MAX_CHARS` | `600000` | Caratteri massimi di documenti, diff, errori, piani e file passati con `path`/`paths` |
| `LOCAL_LLM_TIER` | vuoto | Header `X-Siati-Tier` (`slow`, `medium`, `fast`, `ludicrous`). Vuoto = tier della chiave |
| `LOCAL_LLM_TIMEOUT_SECONDS` | `120` | Attesa massima per una singola risposta |
| `LOCAL_LLM_MAX_RETRIES` | `2` | Tentativi extra su 429, 502, 503 temporanei, errori di connessione |
| `LOCAL_LLM_ALLOWED_READ_ROOTS` | vuoto | Cartelle leggibili (`root`, `path`, `paths`), separate da `;` su Windows (`:` altrove). Vuoto = lettura disattivata |
| `LOCAL_LLM_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOCAL_LLM_CALL_LOG` | `<repo>/.local/calls.sqlite3` | [Registro delle chiamate](#dashboard-delle-chiamate) letto dalla dashboard (percorso assoluto). `off` lo disattiva |
| `LOCAL_LLM_CALL_LOG_DAYS` | `30` | Giorni di storico conservati nel registro: le righe più vecchie vengono cancellate all'avvio del server |
| `LOCAL_LLM_ENV_FILE` | `<repo>/.env` | Percorso alternativo del file `.env` |

> **Variabili della versione precedente.** `LOCAL_LLM_DEFAULT_MODEL`, `LOCAL_LLM_CODE_MODEL`,
> `LOCAL_LLM_EXCLUDED_MODELS`, `LOCAL_LLM_MAX_CHUNKS`, `LOCAL_LLM_REVIEW_CHUNK_CHARS`,
> `LOCAL_LLM_MAX_PROMPT_CHARS`, `LOCAL_LLM_MAX_INPUT_CHARS` e `LOCAL_LLM_MAX_OUTPUT_TOKENS`
> servivano ai tool per i modelli piccoli, ora rimossi. Se restano nel `.env` vengono ignorate e
> `--check` le elenca. I limiti nuovi hanno nomi diversi apposta: i vecchi valori (60.000
> caratteri, 4096–8192 token) troncherebbero i compiti di GLM.

## I tool

| Tool | Cosa fa | Esplora il progetto | Effort predefinito |
|---|---|---|---|
| `ask_codebase(question, root, paths, effort, language)` | Risponde a una domanda sul codice: "come funziona X", "dove si gestisce Y", "chi chiama Z" | sì | medium |
| `review_changes(root, diff \| path, focus, effort, language)` | Review di una diff leggendo anche chiamanti, definizioni e test: bug, regressioni, contratti rotti, sicurezza | sì | medium |
| `diagnose_error(root, error \| path, context, paths, effort, language)` | Segue uno stack trace o un log nel codice fino alla causa: ipotesi ordinate con prove e verifiche | sì | medium |
| `propose_change(task, root, constraints, paths, effort, language)` | Scrive la patch per una modifica, verificata in memoria e pronta per `git apply` | sì | high |
| `write_tests(target, root, symbol, framework, effort, language)` | Scrive i test di un file o di una funzione seguendo le convenzioni dei test esistenti | sì | medium |
| `critique_plan(plan, root, context, paths, effort, language)` | Seconda opinione critica su un piano o un design; con `root` lo verifica sul codice | con `root` | medium |
| `analyze_documents(question, text \| paths, effort, language)` | Domande, riassunti e confronti su uno o più documenti lunghi (fino a 20), con citazioni | no | medium |
| `extract_structured_data(json_schema, text \| path, instructions, effort)` | Estrae dati in un JSON che rispetta lo schema passato | no | low |

Ogni descrizione di tool, che Claude legge quando lo usa, dice quando usarlo, quando no e il
formato esatto della risposta.

### Regole comuni

- **Input:** `root` è il percorso **assoluto** della cartella del progetto, dentro
  `LOCAL_LLM_ALLOWED_READ_ROOTS`. `paths` (facoltativo, relativo a `root`) indica a GLM da dove
  partire. Dove c'è un'alternativa tra testo e file (`diff` o `path`, `error` o `path`, `text` o
  `path`/`paths`) si passa uno solo dei due; i file sono percorsi **assoluti** di file di testo in
  UTF-8 o UTF-16 con BOM (come lo scrive `>` in Windows PowerShell 5.1).
- **Risposta:** sempre JSON, `{"ok": true, ...}` con un campo `stats` (effort, richieste, token,
  funzioni chiamate, file e caratteri letti, eventuale `stopped`, citazioni verificate `x/y`).
- **Lingua:** i testi liberi della risposta sono in inglese, più compatto, a meno di passare
  `language` (es. `italiano`). Conviene solo se il testo va mostrato all'utente così com'è.
- **Effort:** `low`, `medium` o `high`. Più alto è più accurato, ma molto più lento e costoso (vedi
  [Tempi e costi](#tempi-e-costi)).
- **Output del modello non valido:** se GLM non produce un JSON conforme allo schema neanche dopo
  una richiesta di riformattazione, il risultato è `{"ok": false, "error": {"code", "message",
  ...}}`, senza dati inventati.
- **Errori di input, di rete o dell'API:** il tool fallisce con un messaggio leggibile, per esempio
  `API key rifiutata (401)…`, `Timeout: nessuna risposta entro 120 secondi…` o
  `Modello non trovato (404)… Controlla LOCAL_LLM_MODEL`.
- **Progresso:** durante l'esplorazione il server invia a ogni passo una notifica di avanzamento
  (passo sul massimo e funzioni chiamate, per esempio `GLM: read_file, search_text`).

### L'agente in sola lettura

I tool con `root` fanno lavorare GLM come un piccolo agente. A ogni passo GLM può chiamare, anche
più volte in parallelo:

| Funzione | Cosa fa |
|---|---|
| `list_dir(path)` | Elenca file e cartelle (nasconde `node_modules`, `.venv`, `build` e simili) |
| `read_file(path, start_line, end_line)` | Legge un file a intervalli di righe numerate, al massimo 400 righe per chiamata |
| `search_text(pattern, path, glob)` | Cerca un testo o una regex nei file, al massimo 60 risultati |
| `file_outline(path)` | Elenca classi, funzioni e metodi di un file con il numero di riga |

Tutto resta dentro `root`: niente percorsi assoluti, niente `..`, niente link simbolici che
escono, niente file sensibili (vedi [Sicurezza](#sicurezza)). I segreti nel testo letto vengono
oscurati prima di arrivare a GLM.

Tre limiti fermano un compito: passi (`LOCAL_LLM_AGENT_MAX_STEPS`), caratteri letti
(`LOCAL_LLM_AGENT_MAX_READ_CHARS`) e durata (`LOCAL_LLM_TASK_TIMEOUT_SECONDS`). Al limite il
server chiede a GLM di rispondere subito con quello che ha letto, e `stats.stopped` dice quale
limite è scattato. Ogni richiesta ha come timeout il tempo che resta, e un passo troppo lento
ferma l'esplorazione invece di far fallire il tool: un compito dura al massimo
`LOCAL_LLM_TASK_TIMEOUT_SECONDS` più `LOCAL_LLM_TIMEOUT_SECONDS` per la risposta finale. Se una risposta viene troncata dal limite di output, il server la ripete una volta
con effort `low` (`stats.effort_lowered`).

L'API non ha cache del prompt: ogni passo ripaga tutta la storia. Per questo il budget di caratteri
letti conta più del numero di passi: un compito che legge 140.000 caratteri costa circa 500.000
token in ingresso.

### Verifiche del server

- **Citazioni** (`ask_codebase`, `diagnose_error`, `propose_change`, `write_tests`,
  `critique_plan`): ogni `{path, line, quote}` viene cercata nel file reale, ignorando gli spazi.
  `verified: true` vuol dire che il testo citato esiste; se sta a un'altra riga, `line` viene
  corretta e compare `line_corrected: true`. `verified: false` è una citazione
  inventata o sbagliata: resta nel risultato, ma va ricontrollata.
- **Citazioni nei documenti** (`analyze_documents`): la frase citata viene cercata nel documento
  indicato e, se trovata, riceve il numero di riga.
- **Review** (`review_changes`): `evidence_found` dice dove il server ha trovato la citazione di un
  problema: `"diff"`, `"file"` o `null` (probabile falso positivo). Il numero di riga viene
  corretto dagli header `@@` quando la riga citata è univoca. Lockfile e file generati
  (`package-lock.json`, `go.sum`, `*.lock`, `*.min.js`…) non vengono inviati e sono elencati in
  `skipped_files`.
- **Patch** (`propose_change`): la diff viene applicata in memoria ai file attuali. Se si applica,
  `applies: true` e `patch` è la versione corretta dal server (intestazioni `@@` ricontate,
  contesto copiato esattamente dai file), pronta per `git apply`. Se non si applica, il server
  chiede a GLM una correzione; se fallisce anche quella, restituisce la diff del modello con i
  problemi file per file in `files[].problems`.
- **Test** (`write_tests`): `test_path_exists` e `mode` (`new_file` o `append`) dicono se il file
  di test esiste già e se il contenuto va aggiunto.
- **JSON** (`extract_structured_data` e tutti gli altri): la risposta viene validata contro lo
  schema (JSON Schema 2020-12). Per l'estrazione lo schema lo passa Claude, e uno schema non valido
  viene rifiutato prima di inviare richieste.

### Dove salvare le diff

`review_changes` accetta la diff come testo o, meglio, come file con `path`. Il server legge solo
dentro `LOCAL_LLM_ALLOWED_READ_ROOTS`, e una diff salvata dentro il repository rischia di finire
nel commit. Conviene aggiungere alle cartelle consentite una cartella per i file temporanei fuori
dai repository, per esempio `C:\Users\nome\AppData\Local\Temp\claude` (dove Claude Code tiene i
propri file temporanei). La descrizione del parametro `path` elenca le cartelle consentite, e il
messaggio di errore per un percorso fuori da queste le ripete.

### Tempi e costi

Prove reali del 13 novembre 2026 su questo repository (dettagli in
[docs/piano-glm.md](docs/piano-glm.md); prove su un progetto Flutter reale, con bug inseriti e
verità di riferimento, in [docs/report-test-myfuel-2026-11-14.md](docs/report-test-myfuel-2026-11-14.md)):

| Tool | Effort | Durata | Token in / out | Citazioni verificate |
|---|---|---|---|---|
| `ask_codebase` | medium | 38 s | 66k / 3k | 14/14 |
| `review_changes` (2 bug inseriti, trovati) | medium | 47 s | 63k / 4k | 2/2 |
| `review_changes` (stessa diff) | high | 211 s | 96k / 20k | 2/2 |
| `diagnose_error` | high | 135 s | 183k / 13k | 14/14 |
| `propose_change` (patch applicabile) | high | 98 s | 83k / 9k | 6/6 |
| `write_tests` | medium | 247 s | 73k / 22k | 6/6 |
| `critique_plan` | high | 504 s | 496k / 47k | 24/24 |
| `analyze_documents` | medium | 19 s | 15k / 2k | 28/29 |
| `extract_structured_data` | low | 2 s | 0,5k / 0,2k | — |

Un compito con esplorazione dura da mezzo minuto a qualche minuto: non conviene delegare ciò che
Claude fa con una lettura o una ricerca. `high` costa da 2 a 5 volte `medium` in tempo e token: si
usa per le patch e quando un risultato `medium` non basta.

### Esempi d'uso da Claude Code

Basta chiedere in linguaggio naturale: Claude sceglie il tool in base alle descrizioni.

- *"Chiedi a GLM come viene calcolato il prezzo medio in questo progetto."*
- *"Salva `git diff` in un file temporaneo e fai fare una review a GLM, poi verifica tu i problemi
  segnalati."*
- *"Questo test fallisce con questo stack trace: fai diagnosticare la causa a GLM."*
- *"Fai scrivere a GLM la patch per aggiungere il parametro `language` a `summarize`, poi
  applicala ed esegui i test."*
- *"Fai scrivere a GLM i test per `src/app/pricing.py`."*
- *"Prima di implementare, chiedi a GLM una seconda opinione su questo piano, verificandolo sul
  codice."*
- *"Confronta con GLM le due versioni del contratto in `<cartella>/v1.txt` e `<cartella>/v2.txt`."*

Per forzare un tool specifico puoi nominarlo: *"usa `mcp__local-llm__ask_codebase` per…"*.

Esempio di chiamata e risposta di `extract_structured_data`:

```json
// input
{"text": "Fattura n. 2026-0417 del 14 settembre 2026. Righe: consulenza, licenza. Totale CHF 5026.65.",
 "json_schema": {"type": "object",
                 "properties": {"numero": {"type": "string"},
                                "data": {"type": "string", "description": "YYYY-MM-DD"},
                                "totale": {"type": "number"},
                                "righe": {"type": "array", "items": {"type": "string"}},
                                "cliente": {"type": ["string", "null"]}},
                 "required": ["numero", "data", "totale", "righe", "cliente"]}}
// output
{"ok": true,
 "data": {"numero": "2026-0417", "data": "2026-09-14", "totale": 5026.65,
          "righe": ["consulenza", "licenza"], "cliente": null},
 "stats": {"effort": "low", "requests": 1, ...}}
```

## Se GLM non è più disponibile

`glm-5.3-flash` è in prova. Se `--check` dice `NON presente nell'elenco`, imposta nel `.env`:

```
LOCAL_LLM_MODEL=qwen3-coder-next
```

È la riserva scelta con le prove reali del 14/11/2026 ([report](docs/report-riserva-2026-11-14.md)):
circa un minuto per compito e risposte corrette, ma un po' meno accurata di GLM nella review, e
con patch da ricontrollare. Con un modello che non è `glm-*` l'agente chiede il JSON solo alla
fine (`LOCAL_LLM_JSON_WHILE_EXPLORING`), perché altrimenti alcuni modelli rispondono senza leggere
il progetto. Riavvia poi la sessione di Claude Code.

## Dashboard delle chiamate

Ogni chiamata di tool viene salvata in un registro SQLite con gli stessi metadati della riga di log
`tool_call`: tool, modello, esito, durata, effort, caratteri, token, passi dell'agente, funzioni
chiamate, file e caratteri letti, limite raggiunto, citazioni verificate, patch applicabile,
segreti oscurati, `request_id`, più l'ora e il PID della sessione. **Mai il contenuto** di
richieste o risposte: il registro salva solo un elenco chiuso di campi. Tutte le sessioni di
Claude Code dello stesso dispositivo scrivono nello stesso file, quindi la dashboard le mostra
insieme.

Per aprirla, dalla cartella della repository:

```bash
uv run local-llm-dashboard
```

Il comando avvia un piccolo server web su `http://127.0.0.1:8765/` e apre il browser. Opzioni:
`--port 9000` per un'altra porta, `--no-browser` per non aprire il browser. Esci con Ctrl+C. Non
serve la API key e non viene contattata l'API: la dashboard legge solo il registro.

La pagina si aggiorna da sola ogni 5 secondi e mostra:

- **Riepilogo:** numero di chiamate, percentuale riuscite (con errori, output non validi, compiti
  fermati da un limite e risposte ripetute con effort low, ↻), citazioni verificate sul totale,
  token in ingresso e in uscita, durata mediana, p95 e massima.
- **Chiamate nel tempo:** un grafico a barre diviso per esito.
- **Per tool e per modello:** chiamate, fallite, durata media e massima, token, citazioni
  verificate.
- **Esiti non riusciti più frequenti,** per esempio `error:503` o `invalid_output:schema`.
- **Ultime chiamate:** le 100 più recenti, con effort, passi, file letti, citazioni verificate,
  patch applicabile (✓/✗), limite raggiunto e il `request_id` per cercarle nei log del provider.

Filtri: periodo (1 ora, 6 ore, 24 ore, 7 giorni, 30 giorni), tool, modello ed esito. Le chiamate
che falliscono già per un input non valido (per esempio un file fuori dalle cartelle consentite)
non arrivano al modello e non compaiono nel registro. Le righe registrate dalla versione
precedente restano visibili finché scadono.

Sicurezza della dashboard: ascolta solo su `127.0.0.1`, risponde solo a richieste con host
`127.0.0.1` o `localhost` (protezione dal DNS rebinding), non carica risorse esterne e apre il
registro in sola lettura. Se il registro non è scrivibile, il server MCP lo disattiva con un
avviso nei log e i tool continuano a funzionare.

## Regole di delega per Claude

Claude riceve le indicazioni in tre livelli, dal più economico in token:

1. **Istruzioni del server**, in ogni sessione: solo le regole generali (cosa delegare, usare
   `root` e `path`, fidarsi delle citazioni verificate, applicare e verificare patch e test). Sono
   brevi apposta.
2. **Descrizioni dei tool**, lette quando Claude usa un tool: quando usarlo, quando no, formato del
   risultato e limiti.
3. **`CLAUDE.md`**, anche questo in ogni sessione: solo quello che il server non può sapere, cioè
   le soglie per delegare, l'effort da usare e dove salvare le diff.

Il modello per il terzo livello è in [docs/CLAUDE-delega.md](docs/CLAUDE-delega.md): poche righe,
da copiare nel `CLAUDE.md` del progetto o in `~/.claude/CLAUDE.md`.

## Sicurezza

- **Nessuna azione:** il server non scrive file di progetto, non esegue comandi e non contatta
  altri host oltre a `LOCAL_LLM_BASE_URL`. Patch e test vengono restituiti a Claude, non salvati.
  L'unico file che scrive è il [registro delle chiamate](#dashboard-delle-chiamate), con soli
  metadati (`LOCAL_LLM_CALL_LOG=off` lo disattiva).
- **File in sola lettura:** si possono leggere solo i percorsi dentro
  `LOCAL_LLM_ALLOWED_READ_ROOTS`, dopo aver risolto i link simbolici; GLM resta inoltre confinato
  nel `root` del compito. Sono sempre esclusi `.env*` (tranne `.env.example`), chiavi e certificati
  (`.pem`, `.key`, `id_rsa`…), file di credenziali, `.tfstate` e le cartelle `.git`, `.ssh`, `.aws`
  e simili. I file devono essere testo (UTF-8, o UTF-16 con BOM), non binari.
- **Segreti oscurati:** prima dell'invio, chiavi API (`sk-…`), token GitHub, GitLab, Slack, AWS e
  Google, JWT, token Bearer, chiavi private, password negli URL e assegnazioni come
  `password = "…"` diventano `[REDACTED:TIPO]`, sia nel testo passato da Claude sia nei file letti
  da GLM. Il risultato riporta quanti valori sono stati oscurati. Il rilevamento è *best-effort*:
  non inviare di proposito testi riservati.
- **Limiti:** dimensione dell'input, passi, caratteri letti, durata, token di output e timeout
  sono configurabili. Le richieste di chat non superano le 60 al minuto: un compito da 25 passi ne
  usa 25. Si ritentano solo gli errori temporanei (429 rispettando `Retry-After`, 502, 503
  `model_unavailable` o `zone_unavailable`, errori di connessione), mai i timeout.
- **Log:** su stderr, solo metadati, mai il contenuto. Per esempio:

  ```
  ... INFO local_llm_mcp tool_call {"tool": "ask_codebase", "model": "glm-5.3-flash", "effort": "medium", "input_chars": 120, "steps": 7, "tool_calls": 15, "files_read": 9, "read_chars": 61234, "citations_total": 14, "citations_verified": 14, "prompt_tokens": 66012, "completion_tokens": 3120, "outcome": "ok", "duration_ms": 38211, ...}
  ```

> I testi inviati e i file letti da GLM lasciano comunque la tua macchina. `glm-5.3-flash` è
> elaborato in **Svizzera** (gli altri modelli di ITS INCOM AI in Italia) ed è un modello **in
> prova**: può cambiare o essere ritirato, e `--check` lo segnala. Il testo delle richieste viene
> conservato solo se attivi "keep messages" sulla chiave. Dettagli nella pagina *Sovereignty* della
> documentazione del provider.

## Test

```bash
uv run pytest              # nessuna chiamata reale: l'HTTP è simulato con httpx.MockTransport
uv run ruff check src tests
```

## Struttura

```
src/local_llm_mcp/
  config.py      variabili d'ambiente (+ .env) e validazione
  client.py      client HTTP: auth, timeout, retry, rate limit, tool calls, errori leggibili
  workspace.py   le funzioni in sola lettura offerte a GLM, confinate nel root
  agent.py       ciclo di function calling con limiti di passi, lettura e tempo
  tasks.py       logica degli 8 compiti, indipendente da MCP
  prompts.py     prompt di sistema e JSON schema delle risposte
  citations.py   verifica delle citazioni nei file e nei documenti
  patches.py     verifica e correzione delle patch in memoria
  schemas.py     response_format e validazione JSON Schema
  diffs.py       parsing delle diff e file generati
  safety.py      limiti di dimensione e oscuramento dei segreti
  files.py       lettura di file in sola lettura con allowlist
  server.py      tool MCP, descrizioni, istruzioni per Claude, comando --check
  calllog.py / tracking.py   registro SQLite delle chiamate (solo metadati) e query della dashboard
  dashboard.py / dashboard.html   dashboard web locale (local-llm-dashboard)
tests/           tool, agente, verifiche, client, config, sicurezza, registro e dashboard
```

Per aggiungere un tool: prompt e schema in `prompts.py`, logica in `tasks.py`, una funzione
`@mcp.tool` in `server.py` con una descrizione che dica *quando* usarlo, e un test con
`FakeAPI` (e `tool_call_response` per le chiamate di funzione) in `tests/`.

## Risoluzione problemi

| Sintomo | Causa probabile |
|---|---|
| `uv`, `git` o `claude`: comando non trovato | Chiudi e riapri il terminale dopo l'installazione; se persiste, il programma non è nel `PATH` |
| `git clone` chiede credenziali o risponde `Repository not found` | La repository è privata: accedi con un account GitHub che ha accesso (passo 1) |
| `claude mcp list` mostra `Failed to connect` | Esegui `uv run local-llm-mcp --check` nella cartella della repository: stampa l'errore di configurazione |
| `Failed to connect` dopo aver spostato o rinominato la cartella | Il percorso in `--directory` non esiste più: rifai la registrazione B del passo 5 |
| `--check`: `la cartella '…' non esiste` | `LOCAL_LLM_ALLOWED_READ_ROOTS` contiene percorsi di un altro dispositivo: correggili nel `.env` locale |
| `--check`: `NON presente nell'elenco` o `parametri NON dichiarati` | Il modello in `LOCAL_LLM_MODEL` non esiste più o non supporta tool calling e output strutturato: vedi [Se GLM non è più disponibile](#se-glm-non-è-più-disponibile) |
| `--check`: `attenzione: … oltre il contesto del modello` | Il modello ha un contesto più piccolo di GLM: abbassa le variabili indicate al valore suggerito |
| `--check`: `variabili non più usate` | Variabili della versione precedente nel `.env`: vengono ignorate, puoi toglierle |
| I tool non compaiono in chat, o compaiono quelli vecchi | Server non approvato (opzione A) o sessione aperta prima dell'aggiornamento: apri una nuova chat e controlla `/mcp` |
| `uv sync` fallisce su Windows con un file in uso | Una sessione di Claude Code usa il server: chiudile e riprova |
| `API key rifiutata (401)` | Chiave errata, revocata o scaduta in `LOCAL_LLM_API_KEY` |
| `Modello non trovato (404)` | Nome sbagliato in `LOCAL_LLM_MODEL`: verifica con `--check` |
| `… non è caricato … (503)` | Il modello è attivato su richiesta: attivalo dalla dashboard del provider o imposta un altro `LOCAL_LLM_MODEL` |
| `Lettura … disattivata` | `LOCAL_LLM_ALLOWED_READ_ROOTS` è vuota |
| `Percorso fuori dalle cartelle consentite` | Il messaggio elenca le cartelle consentite: usa un `root` o un file dentro una di queste (per le diff, non nel repository su cui lavori) oppure aggiungi la cartella a `LOCAL_LLM_ALLOWED_READ_ROOTS` |
| `stats.stopped` nella risposta | Il compito ha raggiunto un limite (passi, caratteri letti o durata) e GLM ha risposto con quello che aveva letto: restringi la domanda, passa `paths` o alza il limite |
| `Timeout: nessuna risposta entro …` | Una singola risposta è troppo lenta, di solito con effort `high`: usa `medium` o alza `LOCAL_LLM_TIMEOUT_SECONDS` |
| `{"ok": false, "error": {"code": "invalid_output:…"}}` | GLM non ha prodotto un JSON valido neanche dopo la riformattazione: riprova con effort più alto o fai il compito senza delega |
| `applies: false` in `propose_change` | La patch non si applica ai file attuali: leggi `files[].problems`, riprova con più dettagli in `task` o scrivi tu la modifica |
| Citazioni con `verified: false` | GLM ha citato testo che non c'è nel file: considera quella parte non provata |
| `evidence_found: null` nella review | Il problema non cita righe esistenti: è probabilmente un falso positivo |
| La dashboard dice "Nessuna chiamata registrata" | Nessun tool è stato ancora chiamato, oppure il server MCP è partito prima dell'aggiornamento: apri una nuova chat di Claude Code |
| `local-llm-dashboard`: comando non trovato | Esegui `uv sync` con tutte le sessioni di Claude Code chiuse (su Windows il server in uso blocca l'installazione) |

## Licenza

MIT (vedi [LICENSE](LICENSE)). Il progetto nasce da un fork di
[sandraschi/local-llm-mcp](https://github.com/sandraschi/local-llm-mcp), poi riscritto da zero: il
codice originale non è incluso e resta consultabile in quel repository.

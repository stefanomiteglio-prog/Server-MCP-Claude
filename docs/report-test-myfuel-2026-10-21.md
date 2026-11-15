# Test reali su MyFuel — 21 ottobre 2026

Test end-to-end del server `local-llm` (codice al commit `7e53b81`) chiamato da Claude Code sui
file reali del progetto MyFuel (`D:\progetti\apps\MyFuel`, commit `e0f3aa6`). Ogni risultato è
stato confrontato con la verità ricavata dal codice, da `git` o da uno script. Durate e token
vengono dal registro delle chiamate (`.local/calls.sqlite3`, righe 181–226).

## Sintesi

- **46 chiamate registrate, 6 tool, 5 modelli** (più 7 chiamate rifiutate dal server per input
  non valido, senza contattare il modello, e 2 bloccate da Claude Code). Riassunti, estrazioni puntuali, classificazioni e
  `ask_local_model` sono affidabili con il modello predefinito (`gemma-4-26b`): nessun fatto
  inventato, tutti gli hash di commit citati esistono, prompt injection ignorata, segreti oscurati.
- **I conteggi non vanno chiesti ai modelli.** Su 31 dipendenze di `pubspec.yaml` gemma dice 33 e
  qwen3.6 26; su 75 rifornimenti di `test_data.json` gemma dice 64 e qwen3.6 70. Solo
  `gpt-oss-120b` conta giusto (31). Chiedendo **la lista** invece del numero, gemma e qwen3.6
  restituiscono tutti i 31 nomi esatti. Questo smentisce in parte la regola attuale del
  `CLAUDE.md` globale ("qwen3.6 unico veloce con estrazioni perfette").
- **La review con gemma è precisa ma non completa:** 6/6 bug inseriti trovati su una diff piccola,
  5/6 quando la stessa diff è mescolata a 46 KB di codice reale, zero falsi positivi su 4 diff
  reali. `qwen3.8-27b` trova 6/6 anche nella diff lunga, con 1 falso positivo, ma impiega 72 s.
- **Il server ha 7 difetti correggibili** (sezione [Problemi trovati](#problemi-trovati-nel-server)).
  I più gravi: una review a blocchi perde tutti i risultati se un solo blocco va in timeout, i
  lockfile consumano l'85% di una review, i file UTF-16 (quelli scritti da PowerShell 5.1)
  vengono rifiutati come "binari".

## Metodo

### Input usati

I file derivati stanno in `.local/myfuel-tests/` (escluso da git), così i test si possono
ripetere.

| Input | Dimensione | Cosa contiene | Verità di riferimento |
|---|---|---|---|
| `lib/services/sync_manager.dart` | 25 KB | Sincronizzazione SQLite ↔ Firestore | Lettura del codice (fasi 0–3 di `_syncAllInternal`) |
| `lib/utils/fuel_calculator.dart` | 15 KB | Calcoli di consumo | Nessun pagamento con carta (focus assente) |
| `lib/l10n/app_localizations.dart` | 70 KB | Stringhe generate, 2 blocchi | `app_en.arb` |
| `lib_300k.txt` | 300 KB | I primi 300 KB di `lib/**/*.dart` concatenati, 5 blocchi | Struttura delle cartelle |
| `gitlog.txt` | 25 KB | `git log` completo (69 commit) con i messaggi | `git log -1 <hash>` per ogni hash citato |
| `pubspec.yaml` | 5 KB | Dipendenze | 31 dipendenze, 5 dev_dependencies |
| `test_data.json` | 21 KB | Export di prova | Script Python: 75 rifornimenti, 3 veicoli |
| `seeded.diff` | 2 KB | 6 bug inseriti a mano in `trip_calculator.dart` e `consumption_rating.dart` | I 6 bug (tabella sotto) |
| `last_commit.diff` | 46 KB | Commit reale `e0f3aa6` (11 file) | Verifica manuale dei rilievi |
| `combined.diff` | 48 KB | `last_commit.diff` + `seeded.diff` | 6 bug + verifica manuale |
| `rules_commit.diff` | 55 KB | Commit reale `d194b3d` (regole Firestore + `package-lock.json`) | Verifica manuale |
| `rating_commit.diff` | 6 KB | Commit reale `2f32a0c` | Verifica manuale |
| `analyze.txt` di MyFuel | 17 KB | Output di `flutter analyze` salvato da PowerShell (UTF-16 LE) | — |

### Bug inseriti in `seeded.diff`

| # | File e riga (nuova) | Bug |
|---|---|---|
| B1 | `consumption_rating.dart:27` | Controllo `>= 0.85` spostato prima di `> 0.96`: `normal` irraggiungibile |
| B2 | `trip_calculator.dart:94` | Tolto `kml <= 0` dalla guardia: divisione per zero |
| B3 | `trip_calculator.dart:104` | `.ceil()` → `.floor()`: soste di rifornimento sottostimate |
| B4 | `trip_calculator.dart:111` | Pedaggi non più moltiplicati per le tratte, né limitati a ≥ 0 |
| B5 | `trip_calculator.dart:112` | `costPerKm` invertito (`kml / pricePerLiter`) |
| B6 | `trip_calculator.dart:154` | `isAfter` → `isBefore`: prende il prezzo più vecchio |

## Risultati per tool

### `review_code_diff`

**Diff con i bug inseriti (2 KB, un blocco)**

| Modello | Bug trovati | Falsi positivi | Righe esatte | Durata | Note |
|---|---|---|---|---|---|
| gemma-4-26b | **6/6** | 0 | 3/6 (le altre sbagliate di 2–3) | 5,8 s | Il migliore per precisione |
| qwen3-coder-30b | 6/6 (B4 e B5 in un solo rilievo) | 0 | 4/5 | 6,1 s | Spiegazione di B1 sbagliata: descrive l'ordine vecchio come nuovo; severità tutte `critical`/`high` |
| qwen3.6-35b | 5/6 (manca B2) | 0 | 5/5 | 6,2 s | Righe le più accurate |
| qwen3.8-27b | **6/6** | 0 | 4/6 | 12,9 s | Spiegazioni migliori (unità di misura, esempi numerici) |

**Stessi bug dentro 46 KB di codice reale (`combined.diff`, 7 blocchi)**

| Modello | Bug trovati | Falsi positivi sul codice reale | Durata | Esito |
|---|---|---|---|---|
| gemma-4-26b | 5/6 (manca B2) | **0** | 5,1 s | ok |
| qwen3.6-35b | 5/6 (manca B2) | **11**, 6 dei quali `critical` | 46 s | `partial:truncated`: un blocco non rivisto |
| qwen3.8-27b | **6/6** | 1 (`evidence_in_diff: false`) + 1 discutibile | 72 s | ok, 1 blocco ripetuto senza ragionamento |
| gpt-oss-120b | — | — | 120 s | **errore**: timeout nel blocco 4/7, tutto perso |

I falsi positivi di qwen3.6 sono quasi tutti "variabile non definita" (`_previewOpen`, `_scale`,
`_press`, `_cardKey`, `_openPreview`) in `trip_list_tile.dart`, che il server ha diviso in due
blocchi (3 e 4): le dichiarazioni stavano nel blocco 3, il `build` nel blocco 4. Gli altri:
`currentRangeKm` "duplicato" in `trips_tab.dart` (è in due chiamate annidate diverse),
`TripListTile.durationLabel` "rimosso" (esiste, `trip_list_tile.dart:54`). Un rilievo ha una
`explanation` di circa 5.000 caratteri, un flusso di ragionamento ("Wait, let's look at line 106
again…") finito nel JSON.

Il rilievo "discutibile" di qwen3.8 (se `showTripPreview` lanciasse un'eccezione `_previewOpen`
resterebbe `true`) è corretto in teoria, ma `showTripPreview` fa solo un `Navigator.push`: non è un
bug pratico.

**Diff reali senza bug inseriti (gemma)**

| Diff | Blocchi | Rilievi | Corretto? |
|---|---|---|---|
| `last_commit.diff` (46 KB) | 7 | 0 | Sì, nessun bug noto |
| `rules_commit.diff` (55 KB, focus sicurezza) | 7 | 0 | Sì, ma 5 blocchi su 7 erano `package-lock.json` |
| `rules_ps.diff` (solo `firestore.rules`, focus sicurezza) | 1 | 0 | Sì: la modifica chiude il passaggio di documenti tra utenti; la regola `/mail` tolta non è usata dall'app |
| `rating_commit.diff` (6 KB) | 1 | 0 | Sì |

### `summarize_text`

| Test | Modello | Esito | Durata |
|---|---|---|---|
| `sync_manager.dart`, `language: italiano` | gemma | Corretto. Generico sull'ordine delle fasi ("push, pull e riconciliazione") | 4,2 s |
| Stesso file | gpt-oss-120b | Corretto e più completo: fasi nell'ordine giusto (pull veicoli → push → pull rifornimenti → viaggi), code di sync, `registerOneOffSync` | 11,4 s |
| Stesso file, senza `language` | gemma | In inglese, come documentato | 4,1 s |
| `fuel_calculator.dart`, focus "pagamenti con carta" | gemma | "Il testo non contiene informazioni…": corretto | 1,5 s |
| `app_localizations.dart` (70 KB, 2 blocchi) | gemma | Corretto: lingue, aree funzionali, Premium | 16,2 s |
| 300 KB di `lib/` (5 blocchi), focus architettura | gemma | Corretto e utile: DBHelper/DTO/tabelle, Riverpod, repository, SyncManager | 19,7 s |
| `git log`, focus "correzioni su dati, sync, sicurezza" | gemma | 11 hash citati, **tutti esistenti** e descritti correttamente; inizia con "Ecco un riassunto…" nonostante il prompt vieti i preamboli | 13,5 s |
| Testo con prompt injection, password e token Bearer | gemma | Injection ignorata, 2 segreti oscurati e segnalati | 1,2 s |

### `extract_structured_data`

| Test | gemma-4-26b | qwen3.6-35b | gpt-oss-120b |
|---|---|---|---|
| `pubspec.yaml`: 8 valori da copiare (nome, versione, sdk, vincoli, colore, dev_dependencies) + 1 assente | 8/8, assente = null | 8/8, null | 8/8, null |
| `pubspec.yaml`: **numero** di dipendenze (vero: 31) | **33** ✗ | **26** ✗ | 31 ✓ (19 s) |
| `pubspec.yaml`: **lista** delle dipendenze | 31/31 ✓ | 31/31 ✓ | — |
| `test_data.json`: 8 valori, anche cercati per id a metà file + 1 assente | 8/8, null | 8/8, null | — |
| `test_data.json`: **numero** di rifornimenti (vero: 75) | **64** ✗ | **70** ✗ | — |
| `app_localizations.dart` (2 blocchi): lingue, 3 stringhe in punti diversi + 1 inesistente | 4/4, null | — | — |
| Testo libero in italiano ("42,5 litri", "1,849 €/l", "123.456 km", "14/10/2026") | Tutto normalizzato correttamente, carburante assente = null | — | — |

Durate: 2–6 s per gemma e qwen3.6, 19 s per gpt-oss-120b.

### `classify_text`

- **Livello dei file** (ui / logica / dati / configurazione) con `path`: 4/4 corretti
  (`fuel_calculator` → logica, `sync_manager` → dati, `firestore.rules` → configurazione,
  `trip_preview` → ui), 1,5–2 s ciascuno.
- **Messaggi di commit senza prefisso** (feat / fix / perf / chore / release): 7/8 uguali al
  prefisso dell'autore. L'ottavo ("l'app non include più Firebase Analytics", `fix`) diventa
  `chore`: è una scelta difendibile. Motivi brevi, in italiano, con l'evidenza dal testo.

### `ask_local_model`

- Traduzione di 6 stringhe dell'app con segnaposto: corrette, segnaposto intatti; vicine a
  `app_it.arb` ("Mostra dettagli" contro "Mostra i dettagli").
- Messaggio di commit nello stile del progetto: `fix: il calcolo dei pedaggi tiene conto sia
  dell'andata che del ritorno`, corretto e nello stile.
- Spiegazione di `ConsumptionRating.of` per 4 valori e `kml = 0`: tutta corretta.

### Errori e input non validi

| Caso | Messaggio | Valutazione |
|---|---|---|
| Percorso fuori da `D:\progetti` | Elenca le cartelle consentite | Chiaro |
| Percorso relativo | `'path' deve essere un percorso assoluto` | Chiaro |
| `text` e `path` insieme | `Passa 'text' oppure 'path', non entrambi` | Chiaro |
| Modello escluso (apertus) | Dice quale usare al suo posto | Chiaro |
| Modello inesistente | 404 con `request_id` e invito a `list_models` | Chiaro |
| File da 940 KB | `'text' troppo lungo … limite 600,000`, prima di qualsiasi richiesta | Corretto, ma dice `'text'` anche se è stato passato `path` |
| `classify_text` su 70 KB | Rifiutato prima dell'invio | Corretto |
| `analyze.txt` (UTF-16 LE con BOM) | `Il file sembra binario` | **Sbagliato**: è testo |
| `.env` e PNG | Bloccati da Claude Code (classificatore della modalità auto) prima di arrivare al server | Non verificabile dal vivo; coperti dai test unitari (`tests/test_safety.py`) |

## Problemi trovati nel server

In ordine di priorità. Per ognuno: evidenza, causa e correzione proposta.

### 1. Un timeout in un blocco fa perdere tutta la review

- **Evidenza:** `review_code_diff` con `gpt-oss-120b` su `combined.diff` → `Blocco 4/7: Timeout`
  dopo 120 s. Gli altri 6 blocchi (5.480 token di input, 3.942 di output già pagati) sono stati
  annullati o scartati.
- **Causa:** `_map_chunks` (`tasks.py:611`) trasforma ogni `LLMError` in un errore dell'intero
  tool. Per riassunto ed estrazione è giusto (un risultato parziale sarebbe fuorviante), ma la
  review gestisce già i blocchi falliti con `not_reviewed`, solo non per gli errori di API.
- **Correzione:** in `review_diff` un `LLMError` di un blocco (timeout, 5xx dopo i tentativi)
  diventa una voce di `not_reviewed` con `code: "timeout"` o `"http_<status>"`, come l'output
  non valido; il tool fallisce solo se falliscono tutti i blocchi. Inoltre in `client.py:198` il
  timeout va creato con `error_type="timeout"`: oggi nel registro compare come `error:llm`.

### 2. Lockfile e file generati consumano la review

- **Evidenza:** in `rules_commit.diff` 5 blocchi su 7 (≈ 47 KB su 55, 26.091 token di prompt)
  sono `tool/firestore_rules_test/package-lock.json`.
- **Correzione:** `split_diff` salta per nome i file che non si rivedono (`package-lock.json`,
  `yarn.lock`, `pnpm-lock.yaml`, `pubspec.lock`, `uv.lock`, `poetry.lock`, `Cargo.lock`,
  `composer.lock`, `Gemfile.lock`, `go.sum`, `*.min.js`, `*.g.dart`, `*.freezed.dart`) e li
  elenca nel risultato in `"skipped_files"`, così Claude sa che non sono stati visti. Lo stesso
  consiglio va nel modello di `CLAUDE.md` (pathspec `':!*.lock'` in `git diff`).

### 3. Righe approssimative anche quando la citazione è esatta

- **Evidenza:** con `seeded.diff` gemma sbaglia 3 righe su 6 di 2–3 (115 invece di 112, 114
  invece di 111, 106 invece di 104), qwen3.8 2 su 6, sempre con `evidence` copiata esattamente.
- **Correzione:** il server conosce la diff: quando la prima riga di `evidence` compare una sola
  volta tra le righe nuove (contesto e `+`) del file indicato, `line` diventa il numero esatto
  ricavato dagli header `@@`. Altrimenti resta quello del modello. Lo stesso controllo, fatto solo
  sulle righe nuove, eviterebbe anche il caso di qwen3-coder che "cita" righe rimosse e aggiunte
  nell'ordine vecchio e ottiene comunque `evidence_in_diff: true`.

### 4. `explanation` senza limite di lunghezza

- **Evidenza:** qwen3.6 ha scritto ≈ 5.000 caratteri di ragionamento in un'`explanation`;
  un altro blocco è finito troncato a 4.096 token (`partial:truncated`). Il prompt chiede "at most
  two sentences" ma nulla lo impone.
- **Correzione:** `_valid_issue` (`tasks.py:976`) tronca `explanation` a circa 400 caratteri
  (come già fa con `evidence` a 500) e aggiunge `…`. Da valutare con una prova diretta se l'API
  rispetta `maxLength` nello schema JSON: se sì, va messo anche lì.

### 5. Falsi positivi sui file divisi in più blocchi

- **Evidenza:** 6 degli 11 falsi positivi di qwen3.6 sono identificatori "non definiti" in
  `trip_list_tile.dart`, diviso tra i blocchi 3 e 4.
- **Causa:** `review_user` (`prompts.py:198`) dice "split by file", ma un file lungo è diviso
  tra gli hunk e il modello non lo sa.
- **Correzione:** quando un blocco contiene solo una parte di un file, il prompt lo dice
  esplicitamente ("the diff of FILE continues in other parts: identifiers may be declared there,
  do not report them as undefined"). In alternativa `split_diff` può tenere insieme un file fino
  a 2× il limite del blocco.

### 6. File UTF-16 rifiutati come binari

- **Evidenza:** `analyze.txt` di MyFuel (output di `flutter analyze` salvato con `>` in Windows
  PowerShell 5.1, codifica UTF-16 LE con BOM) → `Il file sembra binario`. È lo stesso formato che
  produce `git diff > file` in PowerShell 5.1, cioè proprio quello che le istruzioni del server
  chiedono a Claude di fare.
- **Causa:** `files.py:107` rifiuta qualsiasi file con byte `0x00` prima di guardare il BOM.
- **Correzione:** se i dati iniziano con `FF FE` o `FE FF` si decodificano come UTF-16; se il
  file non è decodificabile il messaggio dice "non è testo UTF-8 o UTF-16".

### 7. Messaggi e dettagli minori

- Con `path` il messaggio di file troppo lungo dice `'text' troppo lungo`: meglio
  `Il file è troppo lungo` (`safety.py:95`, etichetta passata da `_load`).
- Il riassunto del `git log` inizia con "Ecco un riassunto…" nonostante il prompt: si può
  rafforzare `SUMMARY_SYSTEM` ("Start directly with the content") o togliere una prima riga che
  termina con `:` e annuncia il riassunto.
- La descrizione di `extract_structured_data` (`server.py:242`) dovrebbe dire di non chiedere
  conteggi, somme o calcoli: "chiedi la lista e conta tu".

## Correzioni applicate (22 ottobre)

Tutti e 7 i problemi sono stati corretti, con test unitari (255 test, tutti superati). Poi gli
stessi input sono stati rivisti con il codice nuovo contro l'API reale, tramite `Tasks` e la
configurazione del server.

| # | Correzione | Prova reale con il codice nuovo |
|---|---|---|
| 1 | Un timeout o un errore dell'API in un blocco della review finisce in `not_reviewed` (codice `timeout`, `http_<status>`…); il tool fallisce solo se falliscono tutti i blocchi. Il timeout compare nel registro come `error:timeout` invece di `error:llm` | gpt-oss-120b su `combined.diff`: 2 blocchi su 7 in timeout finiscono in `not_reviewed`, gli altri 5 restano (6/6 bug inseriti trovati, ma 13 rilievi falsi o dubbi sul codice reale). Esito nel registro `partial:timeout`, 195 s |
| 2 | Lockfile e file generati (`package-lock.json`, `*.lock`, `*.g.dart`, `*.min.js`…) non vengono inviati e sono elencati in `skipped_files` | `rules_commit.diff`: 1 blocco invece di 7, `package-lock.json` in `skipped_files` |
| 3 | `line` è ricavata dalla citazione quando questa individua una sola riga del nuovo file | `seeded.diff` e `combined.diff` con gemma: tutte le righe esatte (prima 3 su 6) |
| 4 | `explanation` tagliata a 400 caratteri | qwen3.6 su `combined.diff`: le spiegazioni lunghe finiscono con `…`, nessun blocco troncato |
| 5 | Il prompt avvisa il modello quando un file è diviso tra più blocchi | **Non basta per qwen3.6**, che segnala ancora 7 identificatori "non definiti" in `trip_list_tile.dart`. gemma non dava questi falsi positivi già prima. Resta la regola: niente qwen3.6 per la review |
| 6 | File UTF-16 con BOM letti come testo; gli a capo `\r\n` diventano `\n` | `analyze.txt` di MyFuel riassunto correttamente |
| 7 | `'path' troppo lungo` quando l'input è un file; niente conteggi nella descrizione di `extract_structured_data`; prima riga che annuncia il riassunto ("Ecco un riepilogo…:") tolta dal server | Messaggio corretto; riassunto del `git log` senza preambolo |

Il prompt da solo non ha tolto il preambolo (gemma l'ha riscritto anche con il divieto esplicito):
per questo il server toglie la prima riga quando comincia con "Ecco", "Di seguito", "Here is" e
simili e finisce con `:`.

Altre osservazioni dalla seconda serie di prove:

- Con le stesse diff gemma trova ancora 5 bug su 6 in `combined.diff`, ma questa volta manca B1
  invece di B2: su una diff lunga il bug mancato cambia da un'esecuzione all'altra.
- Nel riassunto del `git log` gemma ha scritto `dd0564` per il commit `dd06564`: gli hash citati
  vanno sempre verificati, come dice la descrizione del tool.

### Meno token a ogni sessione

Claude legge a ogni sessione le istruzioni del server e il `CLAUDE.md`; le descrizioni dei tool
solo quando li usa. Le indicazioni sono state spostate di conseguenza:

| Testo | Prima | Dopo |
|---|---|---|
| Istruzioni del server (`INSTRUCTIONS`) | 1.467 caratteri | 820 caratteri (−44%) |
| Sezione da copiare in `CLAUDE.md` ([CLAUDE-delega.md](CLAUDE-delega.md)) | 2.214 caratteri | 476 caratteri (−78%) |

Le avvertenze sui singoli tool (niente conteggi, `skipped_files`, righe ricavate dalla citazione)
sono ora nelle descrizioni dei tool. Il `CLAUDE.md` contiene solo quello che il server non può
sapere: le soglie per delegare, la scelta dei modelli e la cartella delle diff.

## Scelta dei modelli: cosa cambia

| Compito | Prima (`CLAUDE.md` globale) | Dopo questi test |
|---|---|---|
| Riassunti, classificazioni, `ask` | gemma | gemma: confermato |
| Estrazioni di valori | qwen3.6 per i numeri | gemma o qwen3.6 indifferentemente: 100% su valori copiati e cercati |
| Conteggi e somme | qwen3.6 | **Mai al modello**: chiedi la lista e conta tu; se proprio serve, gpt-oss-120b |
| Secondo parere su un riassunto | gpt-oss-120b | Confermato (più completo, 11 s su 25 KB) |
| Code review | gemma | gemma come prima passata (0 falsi positivi); per diff importanti una seconda passata con qwen3.8-27b (6/6, ≈ 1 min). Non qwen3.6 (11 falsi positivi), non gpt-oss-120b (timeout) |

## Limiti di questi test

- Una sola esecuzione per caso: le percentuali sono indicative, non statistiche.
- I bug inseriti sono logici e locali; non sono stati provati bug che attraversano più file.
- `.env` e PNG non sono arrivati al server perché bloccati da Claude Code; la protezione del
  server è coperta solo dai test unitari.
- `ministral-3-14b` non è stato provato.

## Ripetere i test

Gli input sono in `.local/myfuel-tests/`. Per rigenerare `seeded.diff` bastano le sostituzioni
della tabella [Bug inseriti](#bug-inseriti-in-seededdiff) su una copia dei due file, poi
`git diff --no-index`. Le chiamate di questa sessione sono le righe 181–226 del registro:

```bash
sqlite3 .local/calls.sqlite3 "select tool, model, outcome, duration_ms, chunks from calls where id between 181 and 226"
```

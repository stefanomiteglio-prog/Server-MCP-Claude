# Test reali su MyFuel con GLM — 13-14 novembre 2026

Fase 7 del [piano](piano-glm.md): gli 8 tool per `glm-5.3-flash` chiamati per davvero, tramite un
client MCP in memoria con la configurazione del server, sul progetto MyFuel (`C:\_dev\MyFuel`,
commit `55e9eed`). Ogni risultato è stato confrontato con il codice, con `git`, con uno script o
eseguendo i test di Flutter. Stessi casi del [report del 21 ottobre](report-test-myfuel-2026-10-21.md)
dove possibile (bug inseriti, commit `e0f3aa6`, `pubspec.yaml`, `test_data.json`, `git log`).

## Sintesi

- **Qualità alta su tutti i tool.** Nessun fatto inventato nelle risposte verificate a mano;
  91 citazioni su 95 verificate dal server, e delle 4 mancanti 2 erano testo vero rifiutato per
  un difetto del server (corretto, vedi sotto: ora 93 su 95).
- **Review: 6/6 bug inseriti, alle righe esatte, nessun falso positivo** sulla diff piccola; 6/6
  anche dentro 46 KB di codice reale, con un solo rilievo `low` discutibile. Il 21 ottobre il
  modello piccolo migliore (gemma) ne trovava 5/6 nella diff lunga.
- **Patch e diagnosi giuste al primo colpo:** la patch di `propose_change` si applica con
  `git apply`, `flutter analyze` è pulito e i 16 test passano, compreso quello nuovo; la diagnosi
  trova causa, riga e correzione di un test fallito in 30 s.
- **Due difetti del server trovati e corretti:**
  1. il limite di durata non fermava le richieste in corso: una review è durata **25 minuti** ed è
     poi fallita, perdendo tutto il lavoro;
  2. le citazioni di commenti con `//` al posto di `///` risultavano "non verificate".
- **Costi:** le domande e la diagnosi costano 30-70 mila token e meno di un minuto; review di diff
  lunghe, test e patch con effort high arrivano a 300-470 mila token e 5-8 minuti.
- **Punto debole:** `write_tests` scrive test plausibili ma non eseguiti: 4 aspettative su 13
  sbagliate.

## Input

File derivati in `.local/myfuel-tests/` (escluso da git), rigenerabili con lo script descritto in
[Ripetere i test](#ripetere-i-test).

| Input | Cosa contiene | Verità di riferimento |
|---|---|---|
| `base/` | `git archive HEAD` di MyFuel | — |
| `seeded/` | Come `base/`, con i 6 bug inseriti del 21 ottobre (B1-B6) | Tabella dei bug nel [report precedente](report-test-myfuel-2026-10-21.md#bug-inseriti-in-seededdiff) |
| `seeded.diff` | La diff dei 6 bug (2 KB) | 6 bug, righe note |
| `last_commit.diff` | Commit reale `e0f3aa6` (46 KB, 11 file) | Nessun bug noto |
| `combined.diff` | `last_commit.diff` + `seeded.diff` | 6 bug |
| `diag_error.txt` | Output reale di `flutter test` su `seeded/`: test di `latestPrice` fallito | B6 |
| `gitlog.txt` | `git log` completo (72 commit) | `git log -1 <hash>` |
| `pubspec.yaml`, `test_data.json` | Da `base/` | Script Python: 31 + 5 dipendenze, 3 veicoli, valori di `entry-2-10` |

Su `seeded/` i test esistenti di MyFuel falliscono tutti e 6 come previsto, uno per bug.

## Risultati

Effort predefinito di ogni tool, salvo `propose_change` (high). Limiti: 25 passi, 200.000
caratteri letti, 300 s di esplorazione. Timeout per richiesta 300 s (il valore del `.env` di prova).

| Tool | Caso | Durata | Richieste | Token in / out | File letti | Citazioni verificate | Limite | Esito |
|---|---|---|---|---|---|---|---|---|
| `ask_codebase` | Fasi di `syncAll` e quando un record assente dal cloud si elimina | 33 s | 9 | 46k / 3k | 2 | 18/18 | — | ✓ corretto e completo |
| `ask_codebase` | Collezioni Firestore, campo proprietario, regole | 27 s | 11 | 72k / 2k | 3 | 13/13 | — | ✓ corretto, più un commento datato segnalato |
| `review_changes` | `seeded.diff` | 93 s | 9 | 72k / 9k | 8 | 6/6 | — | ✓ 6/6 bug, righe esatte, 0 falsi positivi |
| `review_changes` | `combined.diff` (prima della correzione) | 1490 s | — | — | 7 | — | — | ✗ timeout, tutto perso |
| `review_changes` | `combined.diff` (dopo) | 339 s | 19 | 465k / 30k | 10 | 7/7 | tempo | ✓ 6/6 bug, 1 rilievo `low` discutibile |
| `review_changes` | `last_commit.diff` | 453 s | 17 | 343k / 39k | 9 | — | tempo | ✓ 0 rilievi, nessun falso positivo |
| `diagnose_error` | Test di `latestPrice` fallito | 30 s | 6 | 43k / 2k | 3 | 5/5 | — | ✓ causa, riga e correzione giuste |
| `propose_change` | Ignorare prezzi oltre 5 €/l in `latestPrice`, con test | 471 s | 13 | 70k / 44k | 5 | 9/9 | tempo | ✓ si applica, analyze pulito, 16/16 test |
| `write_tests` | Test per `lib/utils/app_format.dart` (senza test) | 277 s | 26 | 322k / 28k | 14 | 14/14 | passi | ~ 13 casi, 9 giusti (vedi sotto) |
| `critique_plan` | Piano di "ottimizzazione" del sync con 4 errori voluti | 177 s | 11 | 119k / 18k | 3 | 11/15 → 13/15 | — | ✓ trovati tutti e 4 |
| `analyze_documents` | Commit su perdita di dati, sync e sicurezza in `gitlog.txt` | 15 s | 1 | 12k / 1k | — | 8/8 | — | ✓ 6 commit, hash e date esatti |
| `extract_structured_data` | `pubspec.yaml`: nome, versione, liste delle dipendenze | 3 s | 1 | 1,5k / 0,3k | — | — | — | ✓ 31/31 e 5/5, nell'ordine |
| `extract_structured_data` | `test_data.json`: veicoli, `entry-2-10`, campo assente | 3 s | 1 | 8k / 0,2k | — | — | — | ✓ tutto esatto, assente = `null` |

### Dettagli

**`ask_codebase`.** La prima risposta descrive le fasi nell'ordine giusto (fotografia degli id,
veicoli, push, rifornimenti, viaggi), il ruolo del flag `baselineDone` e la differenza con i
viaggi, che si eliminano sempre: tutto confermato sul codice. La seconda elenca le tre collezioni,
le regole riga per riga e i test dell'emulatore, e nota che un commento in
`firestore_service.dart` ("le regole pubblicate non prevedono ancora i viaggi") è superato da
`firestore.rules`: è vero. In entrambe GLM dichiara in `open_points` cosa non ha verificato.

**`review_changes`.** Sulla diff piccola i 6 rilievi corrispondono ai 6 bug, alle righe esatte
(27, 94, 104, 111, 112, 154). Nella diff lunga B1 è indicato alla riga 26 invece che 27 (la
citazione è quella giusta) e c'è un rilievo `low` su `trip_preview.dart:111` (pop e push del
Navigator nello stesso frame): GLM stesso dice di non averlo verificato, ed è un pattern normale
in Flutter. Sul commit reale nessun rilievo, dopo aver controllato chiamanti, import e refactoring.
Le due review lunghe si sono fermate al limite di tempo: la risposta finale arriva comunque, ma
costano 340-470 mila token.

**`propose_change`.** Aggiunge la costante `maxPlausiblePrice`, la usa nel filtro di
`latestPrice` e aggiunge un test nel gruppo giusto con l'helper `_entry` del file. Verifica:
`git apply` su una copia, `flutter analyze` senza problemi, `flutter test` 16/16.

**`write_tests`.** File nuovo con 13 casi nello stile dei test esistenti. Eseguito così com'è: 4
passano. 7 falliscono perché `intl` vuole `initializeDateFormatting` nei test: GLM l'aveva
previsto in `assumptions`, con la correzione da applicare. Con quella correzione passano 9 su 13.
Restano 4 aspettative sbagliate: 2 su `decimal`, dove `decimalPatternDigits` tiene i decimali
fissi (`1.234,50`, non `1.234,5`, e GLM si contraddice con `5,0000` nello stesso test), e 2 date
in cui GLM ha seguito il commento di `app_format.dart` (`29/9/2026`) mentre `intl` dà
`29/09/2026`. I test generati vanno sempre eseguiti, come dice la descrizione del tool.

**`critique_plan`.** Il piano conteneva 4 errori voluti. GLM li trova tutti: la fotografia degli
id va presa prima del pull (premessa sbagliata), il pull in parallelo al push crea una corsa
critica, unire la fase 0 alla fase 2 rompe la deduplicazione dei veicoli prima del push, e segnare
il primo allineamento "anche se ok è false" porta alla perdita di dati al sync successivo dopo un
sync offline. Aggiunge un'osservazione utile: il costo vero è `fetchMyEntries` che riscarica tutti
i rifornimenti a ogni sync. Verdetto `needs_changes`.

**`analyze_documents`.** Sei commit: `d194b3d`, `406ddae`, `e43403e`, `1067882`, `1f41225`,
`573511a`, tutti esistenti, con le date giuste; `e43403e` è un `feat` e GLM l'ha trovato dal corpo
del messaggio. Il 21 ottobre gemma aveva scritto un hash sbagliato (`dd0564`).

## Problemi trovati e correzioni

### 1. Il limite di durata non fermava le richieste in corso

- **Evidenza:** `review_changes` su `combined.diff` è durata 1490 s con un limite di 300 s, poi è
  fallita con `Timeout: nessuna risposta entro 300 secondi`, buttando 7 file letti.
- **Causa:** la durata si controllava solo tra un passo e l'altro. Ogni richiesta poteva durare
  fino a `LOCAL_LLM_TIMEOUT_SECONDS` (300 s nel `.env` di prova), e dopo il limite venivano la
  risposta finale, la riformattazione e il nuovo tentativo con effort low, ciascuno fino a 300 s.
  Un timeout durante l'esplorazione faceva fallire tutto il tool.
- **Correzione** (`agent.py`, `client.py`): ogni richiesta ha come timeout il tempo che resta;
  l'esplorazione finisce entro `LOCAL_LLM_TASK_TIMEOUT_SECONDS` e la risposta finale ha al massimo
  `LOCAL_LLM_TIMEOUT_SECONDS` in più. Un passo che va in timeout ferma l'esplorazione
  (`stopped: "timeout"`) e GLM risponde con quello che ha letto. Con la correzione la stessa review
  si chiude in 339 s con un risultato completo. 4 test nuovi in `tests/test_agent.py`.

### 2. Citazioni di commenti con il marcatore sbagliato

- **Evidenza:** in `critique_plan` 2 delle 4 citazioni non verificate erano testo vero di
  commenti Dart `///` citati con `//`.
- **Correzione** (`citations.py`): il confronto ignora il marcatore di commento iniziale (`//`,
  `///`, `#`, `*`, `--`) sia nella citazione sia nel file; il testo deve comunque esistere. Sulle
  citazioni reali di `critique_plan`: 13/15 invece di 11/15; le 2 restanti sono giustamente
  rifiutate (una virgola nel posto sbagliato e una frase di GLM, non una citazione).

### Altro

- Il messaggio di risposta troncata citava ancora `LOCAL_LLM_MAX_OUTPUT_TOKENS`: ora
  `LOCAL_LLM_GLM_MAX_OUTPUT_TOKENS`.
- Il messaggio di timeout suggeriva "un modello più veloce": ora suggerisce un compito più piccolo
  o un effort più basso.

## Indicazioni d'uso

| Compito | Indicazione |
|---|---|
| Domande sul codice, diagnosi, documenti, estrazioni | Delegare sempre: veloci (3-35 s), economici, corretti |
| Review di diff piccole e medie | `review_changes` con effort predefinito: affidabile e senza falsi positivi |
| Review di diff oltre ~40 KB | Funziona, ma costa 5-8 minuti e 300-500 mila token: passare `focus` o dividere la diff per area |
| Patch | `propose_change` (effort high): rileggerla, applicarla, eseguire i test |
| Test | `write_tests`: un buon punto di partenza, da eseguire sempre; leggere `assumptions` |
| Piani | `critique_plan` con `root`: trova errori reali sul codice |

Timeout per richiesta: 120 s (il default) bastano; con 300 s un tool arriva a 10 minuti invece
di 7.

## Limiti di questi test

- Una sola esecuzione per caso: le percentuali sono indicative.
- I bug inseriti sono logici e locali, come il 21 ottobre; il piano di `critique_plan` è stato
  scritto apposta con errori noti.
- I tempi dipendono dal carico dell'API: `glm-5.3-flash` è in prova e gira in Svizzera.

## Ripetere i test

Lo script di preparazione crea `base/` e `seeded/` con `git archive HEAD` di MyFuel (MyFuel non
viene modificato), applica i 6 bug, scrive le diff e il `git log`. Le chiamate sono nel registro
di prova `.local/myfuel-tests/live_calls.sqlite3`:

```bash
sqlite3 .local/myfuel-tests/live_calls.sqlite3 "select tool, outcome, duration_ms, steps, stopped, citations_verified, citations_total from calls"
```

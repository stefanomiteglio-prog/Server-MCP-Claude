# Modello di riserva per GLM — prove reali del 14 novembre 2026

Obiettivo ([piano](piano-modelli-2026-11-14.md)): scegliere il modello da mettere in
`LOCAL_LLM_MODEL` se `glm-5.3-flash`, ancora "on trial", venisse ritirato. Candidati:
`qwen3-coder-next` (contesto 262k, il più economico in ingresso) e `qwen3.8-27b` (ragiona come GLM,
contesto 131k).

Metodo: `Tasks` chiamato direttamente con la configurazione del `.env`, modello cambiato, timeout
per richiesta 300 s (come nel [report del 14/11](report-test-myfuel-2026-11-14.md)),
`LOCAL_LLM_DOCUMENT_MAX_CHARS` 290.000. Progetto: MyFuel al commit `55e9eed` (`base/`) e la stessa
copia con i 6 bug inseriti (`seeded/`), in `.local/myfuel-tests/`. Registro delle chiamate:
`.local/myfuel-tests/fallback_calls.sqlite3`. Una sola esecuzione per caso.

## Primo giro: i Qwen non esploravano

Con il codice di prima, i due Qwen hanno risposto a ogni compito **senza chiamare nessuna
funzione** (0 file letti). `qwen3-coder-next` ha inventato file inesistenti
(`src/sync/refuelSync.js`), `qwen3.8-27b` ha dato una risposta vuota o una patch rifiutata ("non ho
ancora esplorato il progetto").

Causa, verificata con richieste singole: l'agente invia `response_format` insieme a `tools` a ogni
passo. GLM e devstral chiamano comunque le funzioni; `qwen3-coder-next` e `qwen3.8-27b` invece
scrivono subito il JSON. Con `tool_choice: "required"` le funzioni le chiamano.

Correzione: `LOCAL_LLM_JSON_WHILE_EXPLORING`. Se non è impostata vale `true` per i modelli
`glm-*` (comportamento invariato) e `false` per gli altri. Con `false` l'agente esplora senza
`response_format` e chiede il JSON con una richiesta finale (`tool_choice: "none"`).

## Secondo giro, con la correzione

| Caso | GLM | qwen3-coder-next | qwen3.8-27b |
|---|---|---|---|
| `ask_codebase`: fasi di `syncAll` ed eliminazioni | 26 s, 14/14 citazioni, corretto | 67 s, 11/11, corretto (manca la "fotografia" iniziale degli id) | 506 s (limite di tempo), 22/22, il più completo |
| `review_changes`: `seeded.diff` (6 bug) | 44 s, **6/6**, 0 falsi positivi | 59 s, **5/6** (manca B3, pedaggi), 0 falsi positivi | 473 s (limite di tempo), **5/6** (manca B5, `floor`), 0 falsi positivi |
| `diagnose_error`: test di `latestPrice` fallito | 54 s, causa giusta | 57 s, causa giusta, 4/4 | 304 s, causa giusta, 15/15 |
| `propose_change` (effort high) | 541 s, si applica, 16/16 test, 1 avviso di stile | 74 s, codice giusto ma il hunk del test **non si applica** (contesto sbagliato, segnalato dal server) | **errore**: timeout dopo 600 s |

GLM con `propose_change` ed effort high va in timeout con il timeout per richiesta di 120 s del
`.env` (primo giro): le sue singole risposte superano i 2 minuti.

## Conclusione

- **Riserva: `qwen3-coder-next`.** Veloce (circa un minuto per compito), nessun fatto inventato
  dopo la correzione, citazioni tutte verificate. Più debole di GLM: un bug su sei mancato nella
  review e patch da ricontrollare prima di applicarle (il server lo segnala con `applies: false`).
  `reasoning_effort` è accettato ma non dichiarato: `effort` probabilmente non cambia nulla.
- **Non `qwen3.8-27b`**: qualità vicina a GLM, ma 5-10 minuti per compito e la patch non arriva.
- GLM resta il modello predefinito.

Per passare alla riserva basta `LOCAL_LLM_MODEL=qwen3-coder-next` nel `.env`;
`LOCAL_LLM_JSON_WHILE_EXPLORING` si imposta da sola. `--check` ricorda che `reasoning_effort` non è
dichiarato.

## Limiti

- Un'esecuzione per caso: i 5/6 possono cambiare da un'esecuzione all'altra, come era successo con
  gemma il 21 ottobre.
- Non provati `write_tests`, `critique_plan`, `analyze_documents`, `extract_structured_data` con la
  riserva (gli ultimi due non usano l'agente e non cambiano con la correzione).

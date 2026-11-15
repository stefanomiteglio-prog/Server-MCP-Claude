# Piano: nuovi modelli dell'API (14 novembre 2026)

Stato: fase 1 completata il 14/11 (risultati in [report-riserva-2026-11-14.md](report-riserva-2026-11-14.md): riserva `qwen3-coder-next`); immagini e audio scartati perché non necessari.

## Cosa offre oggi l'API (`GET /models`, 21 modelli)

`glm-5.3-flash` è ancora presente, con gli stessi limiti (contesto 262.144, output 32.768) ma
sempre **"on trial"**: il server funziona senza modifiche (`--check` OK).

| Modello | Contesto | Tools | `reasoning_effort` dichiarato | Immagini | Prezzo in/out (CHF/1M) | Note del fornitore |
|---|---|---|---|---|---|---|
| glm-5.3-flash | 262k | sì | sì | no | 0,345 / 0,6 | in prova, Svizzera |
| qwen3-coder-next | 262k | sì | no | no | **0,092** / 0,4 | coder, meglio su modifiche multi-file |
| qwen3.8-27b | 131k | sì | sì | no | 0,345 / 0,6 | ragiona, lento |
| gpt-oss-120b | 131k | sì | sì | no | 0,345 / 0,6 | timeout nella review del 21/10 |
| devstral-small-2-24b | 131k | sì | no | sì* | 0,17 / 0,6 | coder Mistral |
| flint-40b | **65k** | sì | no | no | 0,115 / 0,44 | coder agentico, contesto piccolo |
| gemma-4-26b / gemma-4-31b | 262k | sì | no | **sì** | 0,345 / 0,6–0,8 | veloce / più accurato |
| qwen3.6-35b, qwen3-coder-30b | 262k | sì | no | 3.6 sì | 0,345 / 0,6 | già scartati il 21/10 |
| ministral-3-14b / 3-8b | 262k | sì | no | 14b sì | 0,35 / 0,17 | generici |
| mistral-small-3.2-24b, apertus-70b | 32k / 16k | — | no | no | — | contesto troppo piccolo |
| bge-m3, bge-reranker-v2-m3 | — | — | — | — | — | embedding e rerank |
| whisper-1, whisper-1-hd | — | — | — | — | — | trascrizione audio |
| z-image-turbo, wan-2.2-5b-fast, tts-1 | — | — | — | — | — | genera immagini, video, voce |

\* la descrizione lo dice, `input_modalities` no.

Prova reale (14/11, una richiesta con una funzione `read_file` e `reasoning_effort: "low"`):
glm, qwen3-coder-next, devstral, flint, qwen3.8-27b e gemma-4-31b rispondono tutti 200 con una
`tool_call` corretta in 0,6–2 s. Quindi `reasoning_effort` non viene rifiutato nemmeno dai modelli
che non lo dichiarano.

## Valutazione: nuovi tool?

Criterio: un tool nuovo deve dare a Claude una **capacità che non ha** o un **risparmio di token
misurabile**, e non deve violare le regole del server (niente scrittura di file).

| Idea | Verdetto | Motivo |
|---|---|---|
| Un tool per ogni modello coder (qwen3-coder-next, devstral, flint) | **No** | Fanno le stesse cose di GLM: sarebbero doppioni dei tool attuali, Claude dovrebbe scegliere tra tool equivalenti |
| Ricerca semantica con bge-m3 + reranker | **No** | Serve un indice persistente da costruire e aggiornare (il server scrive solo il registro). GLM esplora già con `search_text`, con 91–93/95 citazioni corrette |
| Generazione di immagini, video, voce | **No** | Producono file (vietato dalle regole) e non servono a lavorare su codice |
| Trascrizione audio (whisper) | **No** | È l'unica capacità che Claude non ha, ma non serve per questo uso |
| Immagini (gemma-4) | **No** | Claude legge già immagini da solo; il risparmio ci sarebbe solo con molte immagini o PDF scansionati |

**Conclusione: nessun tool nuovo per i modelli di chat.** Gli 8 tool restano.

Costo in contesto: in Claude Code i tool MCP sono caricati in differita (solo il nome finché non
servono), quindi un tool aggiunto costa poco in ogni sessione, ma va comunque evitato se non serve.
`INSTRUCTIONS` e la sezione di `CLAUDE.md` non devono crescere in nessun caso.

## Cosa si fa: modello di riserva per quando GLM esce dalla prova

Oggi, se `glm-5.3-flash` sparisce dall'elenco, tutti gli 8 tool falliscono.

1. `--check`: `reasoning_effort` non dichiarato diventa un avviso, non un errore (la prova sopra
   mostra che l'API lo accetta). `tools` e `response_format` restano obbligatori.
2. `LOCAL_LLM_DOCUMENT_MAX_CHARS` controllato contro `context_length` del modello in `--check`:
   600.000 caratteri non stanno nei 131k di qwen3.8/devstral, né nei 65k di flint.
3. Nessun passaggio automatico a un altro modello: basta cambiare `LOCAL_LLM_MODEL` nel `.env`
   (un fallback automatico nasconderebbe un calo di qualità).
4. Test reali con i casi già pronti in `.local/myfuel-tests/` (diff con 6 bug inseriti,
   `combined.diff`, `ask_codebase`, `propose_change`) su **qwen3-coder-next** (262k, il più
   economico in ingresso, che è il costo principale senza cache) e **qwen3.8-27b** (ragiona come
   GLM). Report in `docs/`, e il vincitore scritto nel README come riserva.
5. Emerso dalle prove: con `tools` e `response_format` nella stessa richiesta i due Qwen
   rispondono senza esplorare. Aggiunta `LOCAL_LLM_JSON_WHILE_EXPLORING` (default `true` solo per
   `glm-*`): con `false` il JSON si chiede solo nella richiesta finale.

## Da non fare

- Modelli diversi per tool (era la vecchia architettura, tolta in `8f370ef`): i test del 14/11
  mostrano GLM migliore dei modelli piccoli su review e hash, e un solo modello tiene semplici
  configurazione e descrizioni.
- Tornare a `list_models` come tool: la verifica resta in `--check`.

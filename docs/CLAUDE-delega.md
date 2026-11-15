# Modello per CLAUDE.md: delega a GLM

Copia la sezione qui sotto nel `CLAUDE.md` del progetto, oppure in `~/.claude/CLAUDE.md` per
tutti i progetti, e sostituisci `<CARTELLA_DIFF>` con una cartella dentro
`LOCAL_LLM_ALLOWED_READ_ROOTS` ma fuori dal repository (per esempio
la cartella `.local` dentro la copia del server, già esclusa da git).

Il `CLAUDE.md` viene letto per intero a ogni sessione, quindi la sezione contiene solo quello che
Claude non sa già. Il resto arriva dal server: le regole generali (passare `root` e `path`,
fidarsi delle citazioni verificate, applicare e verificare patch e test, non inviare segreti) sono
nelle istruzioni del server, i dettagli di ogni tool (quando usarlo, formato, `evidence_found`,
`applies`) nelle descrizioni dei tool, che Claude legge solo quando li usa. Non ripeterli qui.
Le soglie vengono dalle [prove reali](report-test-myfuel-2026-11-14.md): un
compito con esplorazione dura da 20 secondi a qualche minuto e `high` costa da 2 a 5 volte
`medium`.

```markdown
## Delega a GLM (`local-llm`)
- Domande che richiederebbero di leggere più di 3-4 file: `ask_codebase` invece di leggerli.
  Errori il cui percorso attraversa più file: `diagnose_error`. Documenti oltre 300 righe:
  `analyze_documents` con `paths`, senza leggerli.
- Prima di un commit con una diff oltre 100 righe: `review_changes`, con la diff salvata in
  `<CARTELLA_DIFF>\review.diff`; verifica tu ogni problema prima di correggerlo.
- Piani non banali: `critique_plan` con `root` prima di implementarli.
- Effort: ometti `effort` (default per tool). `high` solo se il risultato `medium` non basta.
- Non delegare ciò che fai con una lettura o una ricerca, né modifiche di poche righe.
```

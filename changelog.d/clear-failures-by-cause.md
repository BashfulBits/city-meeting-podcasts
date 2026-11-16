Make the manual terminal-failure clearing workflow safe to use on every LLM lane (tag, agenda,
locator, moments). Every run now prints the matching markers grouped by cause; `--last-error-contains`
(workflow input `last_error_contains`) clears only a cause a shipped fix addresses; each cleared
marker is archived under `state/llm_failure_marker_audits/<date>/` first, under the record lock;
and `--apply` refuses more than `--max-clear` (default 300) markers. The workflow runs on
`ubuntu-24.04`.

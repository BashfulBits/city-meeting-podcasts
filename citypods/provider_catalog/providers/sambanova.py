"""SambaNova Cloud (Free tier; config/provider_limits.yml documents its 20 rpm Free-tier ceiling).

Account-level free evidence, like Groq: the configured routes are `free: true` on this account's
Free tier, and the canary decides per model (live 2026-09-24: MiniMax-M2.7 served; 429 "currently
experiencing high demand" is capacity noise, inconclusive). The discovery backtest (review/48)
found observation-only mode could never re-find the two SambaNova models already configured.
"""

from citypods.provider_catalog.rules import MODEL_DOES_NOT_EXIST, ProviderRules, account_level

RULES = ProviderRules(
    name="sambanova",
    free_evidence=account_level(),
    signals=(MODEL_DOES_NOT_EXIST,),
)

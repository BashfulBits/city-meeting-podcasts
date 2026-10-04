"""Provider catalog reconciliation (review/48): keep free LLM routes current with providers.

``rules`` defines the provider-plugin contract, ``providers/`` holds one plugin per provider,
``registry`` discovers them, and ``classify`` turns a canary response into a verdict.
"""

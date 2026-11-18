"""Reusable GPU launch planning for batched model inference jobs.

``profiles`` holds GPU classes, model profiles and measured calibrations, ``planner`` picks the
cheapest GPU that fits a model and token volume, ``kernel_cache`` makes one-time compile work
reusable across executions, and ``warmup`` derives the shapes that populate that cache. None of
these modules call a cloud API; ``citypods.judging.decision_batch_cloud`` does the launching.
"""

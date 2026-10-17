"""The judge stack (review/49, review/53): typed judgments of producer outputs by JEV plus siblings.

``tasks`` holds the task-spec registry, ``tag_task`` and ``moment_task`` register the two tasks,
``backends`` turns packets into dispatch jobs and parses replies, ``packing`` groups subjects into
packets, ``ledger`` records judgments append-only on each candidate, and ``runner`` drives one
judge pass over a city's episodes for :class:`citypods.stages.JudgeStage`.
"""

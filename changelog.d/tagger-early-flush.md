Submit collected tag jobs to the dispatch Worker as soon as 100 have gathered or the oldest has
waited 30 seconds, instead of only at the state checkpoint (minutes apart) and the end of the run.
Pin the LLM producer and sweep workflows to `ubuntu-24.04`: long tag and moments runs on runner
image `ubuntu26/20261111.82` repeatedly lost runner communication and their unsent jobs.

Scheduled GitHub Actions workflows now use staggered, nonzero UTC minute offsets while preserving
their existing cadence. The city-request intake retry cron keeps its 15-minute interval at an offset
from common quarter-hour ticks. This reduces synchronized starts; GitHub may still delay scheduled
triggers during platform load.

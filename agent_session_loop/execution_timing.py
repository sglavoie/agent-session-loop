"""Public execution-timing API.

The collector is divided into evidence models, ingestion, reconciliation, and
collection so each accounting concern stays independently readable.
"""

from .execution_timing_collect import collect_execution_timing
from .execution_timing_models import *

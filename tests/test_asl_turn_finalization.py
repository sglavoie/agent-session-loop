"""Turn-finalization tests, split by finalization responsibility."""

from .asl_turn_finalization_cases_artifacts import PreservedReviewArtifactTests
from .asl_turn_finalization_cases_assurance import DegradedAssuranceTests
from .asl_turn_finalization_cases_correction import HandoffCorrectionAttemptTests
from .asl_turn_finalization_cases_diagnostics import CommandFailureDetailTests
from .asl_turn_finalization_cases_mechanical import MechanicalHandoffDefectTests
from .asl_turn_finalization_cases_projection import ReviewArtifactProjectionTests
from .asl_turn_finalization_cases_review_gate import BroadReviewGateTests
from .asl_turn_finalization_cases_shape import TurnFinalizationShapeTests
from .asl_turn_finalization_cases_terminal import TerminalAdjudicationTests
from .asl_turn_finalization_cases_validators import (
    GeneratedReviewValidatorDiagnosticTests,
)

__all__ = [name for name in globals() if name.endswith("Tests")]

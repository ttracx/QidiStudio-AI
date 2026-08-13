"""Unit tests for deterministic Print Health safety-critical logic."""
from __future__ import annotations

import unittest

from print_health.core import PrintHealthSettings, fuse_assessments
from print_health.models import (
    Action,
    BoundingBox,
    Detection,
    ProviderAssessment,
    Severity,
)
from print_health.remediation import diagnose_changes


class PrintHealthFusionTests(unittest.TestCase):
    def settings(self) -> PrintHealthSettings:
        return PrintHealthSettings(
            min_consensus_providers=2,
            warning_threshold=0.45,
            critical_threshold=0.76,
            healthy_interval_s=15.0,
            suspect_interval_s=5.0,
            warning_interval_s=2.5,
            critical_interval_s=1.0,
        )

    def assessment(
        self,
        provider: str,
        severity: Severity,
        confidence: float,
        defect: str | None = None,
        quality: int = 90,
    ) -> ProviderAssessment:
        detections = []
        if defect:
            detections.append(
                Detection(
                    defect=defect,
                    confidence=confidence,
                    severity=severity,
                    bbox=BoundingBox(100, 100, 700, 700),
                )
            )
        return ProviderAssessment(
            provider=provider,
            model="test",
            quality_score=quality,
            confidence=confidence,
            severity=severity,
            detections=detections,
            recommended_action=Action.PAUSE if severity is Severity.CRITICAL else Action.CONTINUE,
        )

    def test_two_high_confidence_critical_votes_pause_and_sample_fast(self):
        fused = fuse_assessments(
            [
                self.assessment("openai", Severity.CRITICAL, 0.91, "spaghetti", 20),
                self.assessment("ollama-local", Severity.CRITICAL, 0.87, "spaghetti", 25),
            ],
            [],
            self.settings(),
        )
        self.assertEqual(fused.severity, Severity.CRITICAL)
        self.assertEqual(fused.recommended_action, Action.PAUSE)
        self.assertEqual(fused.sample_interval_s, 1.0)
        self.assertGreater(fused.detections[0].confidence, 0.95)

    def test_single_critical_provider_does_not_trigger_destructive_consensus(self):
        fused = fuse_assessments(
            [self.assessment("openai", Severity.CRITICAL, 0.95, "blob", 30)],
            ["ollama-local: unavailable"],
            self.settings(),
        )
        self.assertNotEqual(fused.recommended_action, Action.PAUSE)
        self.assertIn("insufficient provider consensus", fused.rationale)

    def test_suspected_defect_increases_sampling_before_warning(self):
        fused = fuse_assessments(
            [
                self.assessment("openai", Severity.OK, 0.55, "stringing", 88),
                self.assessment("ollama-local", Severity.OK, 0.60, None, 94),
            ],
            [],
            self.settings(),
        )
        self.assertIn(fused.sample_interval_s, {5.0, 15.0})
        self.assertGreaterEqual(fused.quality_score, 80)


class RemediationTests(unittest.TestCase):
    def test_under_extrusion_clog_changes_are_bounded(self):
        assessment = fuse_assessments(
            [
                ProviderAssessment(
                    provider="openai",
                    model="test",
                    quality_score=30,
                    confidence=0.9,
                    severity=Severity.CRITICAL,
                    detections=[Detection("clog", 0.9, Severity.CRITICAL)],
                    recommended_action=Action.PAUSE,
                ),
                ProviderAssessment(
                    provider="ollama-local",
                    model="test",
                    quality_score=35,
                    confidence=0.85,
                    severity=Severity.CRITICAL,
                    detections=[Detection("under_extrusion", 0.86, Severity.CRITICAL)],
                    recommended_action=Action.PAUSE,
                ),
            ],
            [],
            PrintHealthSettings(min_consensus_providers=2),
        )
        changes = diagnose_changes(assessment)
        self.assertLessEqual(changes.speed_multiplier, 0.85)
        self.assertGreater(changes.flow_multiplier, 1.0)
        self.assertLessEqual(changes.nozzle_temp_delta_c, 15)
        self.assertEqual(changes.rotation_z_deg, 0)

    def test_spaghetti_remediation_does_not_create_unbounded_temperature(self):
        fused = fuse_assessments(
            [
                ProviderAssessment(
                    provider="a", model="m", quality_score=10, confidence=0.95,
                    severity=Severity.CRITICAL,
                    detections=[Detection("spaghetti", 0.95, Severity.CRITICAL)],
                    recommended_action=Action.PAUSE,
                ),
                ProviderAssessment(
                    provider="b", model="m", quality_score=10, confidence=0.95,
                    severity=Severity.CRITICAL,
                    detections=[Detection("detachment", 0.95, Severity.CRITICAL)],
                    recommended_action=Action.PAUSE,
                ),
            ], [], PrintHealthSettings(min_consensus_providers=2)
        )
        changes = diagnose_changes(fused)
        self.assertLessEqual(changes.brim_add_mm, 10.0)
        self.assertGreaterEqual(changes.first_layer_speed_multiplier, 0.5)
        self.assertLessEqual(changes.first_layer_speed_multiplier, 1.0)


if __name__ == "__main__":
    unittest.main()

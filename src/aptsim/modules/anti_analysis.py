"""Synthetic sandbox-detection research.

This module never inspects the real host. It evaluates a rule set against
*configured mock indicators* so analysts can study what sandbox-aware malware
telemetry looks like, score it and build detections -- without the framework
being able to recognise (or evade) an actual analysis environment.

``enforcement`` is hard-locked to ``log_only`` here as well as in config
validation: even if the configuration claims otherwise, no behaviour changes.
"""

from __future__ import annotations

from typing import Any

from aptsim.core.errors import ConfigError

from .base import SimModule

PREDICATES = {"lte", "gte", "eq", "neq", "contains_any", "non_empty", "empty"}


def evaluate(indicator: Any, predicate: str, expected: Any = None) -> bool:
    if predicate == "lte":
        return float(indicator) <= float(expected)
    if predicate == "gte":
        return float(indicator) >= float(expected)
    if predicate == "eq":
        return indicator == expected
    if predicate == "neq":
        return indicator != expected
    if predicate == "contains_any":
        text = str(indicator).lower()
        needles = expected if isinstance(expected, list) else [expected]
        return any(str(needle).lower() in text for needle in needles)
    if predicate == "non_empty":
        return bool(indicator)
    if predicate == "empty":
        return not indicator
    raise ConfigError(f"unsupported anti-analysis predicate: {predicate!r}")


class AntiAnalysisModule(SimModule):
    name = "anti_analysis"
    description = "Rule-based scoring of synthetic environment indicators (log only)"

    async def run(self, ctx) -> None:  # type: ignore[no-untyped-def]
        config = ctx.section("anti_analysis")
        if config.get("enforcement") != "log_only":
            raise ConfigError(
                "anti_analysis.enforcement must remain 'log_only'; behaviour-changing evasion is unsupported"
            )

        rules: list[dict[str, Any]] = list(config.get("rules", []))
        threshold = float(config.get("threshold", 55))
        for rule in rules:
            if rule.get("predicate") not in PREDICATES:
                raise ConfigError(f"rule {rule.get('id')} uses unknown predicate {rule.get('predicate')}")

        await ctx.emit(
            "anti_analysis",
            "assessment.begin",
            self.name,
            data={
                "rules": len(rules),
                "threshold": threshold,
                "enforcement": "log_only",
                "source": "synthetic-config",
            },
        )

        assessments: list[dict[str, Any]] = []
        for scenario in config.get("scenarios", []):
            scenario_id = str(scenario.get("id"))
            indicators: dict[str, Any] = dict(scenario.get("indicators", {}))
            triggered: list[dict[str, Any]] = []
            score = 0.0

            await ctx.emit(
                "anti_analysis",
                "scenario.begin",
                self.name,
                data={"scenario": scenario_id, "indicators": indicators, "synthetic": True},
            )

            for rule in rules:
                indicator_name = str(rule.get("indicator"))
                if indicator_name not in indicators:
                    continue
                hit = evaluate(indicators[indicator_name], str(rule.get("predicate")), rule.get("value"))
                entry = {
                    "rule_id": rule.get("id"),
                    "indicator": indicator_name,
                    "weight": float(rule.get("weight", 0)),
                    "hit": hit,
                }
                if hit:
                    score += float(rule.get("weight", 0))
                    triggered.append(entry)
                await ctx.emit(
                    "anti_analysis",
                    "rule.evaluated",
                    self.name,
                    severity="debug",
                    data={"scenario": scenario_id, **entry, "description": rule.get("description")},
                )

            verdict = "sandbox_like" if score >= threshold else "benign_like"
            assessment = {
                "scenario": scenario_id,
                "score": score,
                "threshold": threshold,
                "verdict": verdict,
                "triggered": triggered,
                "synthetic": True,
            }
            assessments.append(assessment)
            await ctx.emit(
                "anti_analysis",
                "assessment.completed",
                self.name,
                data={
                    "scenario": scenario_id,
                    "score": score,
                    "verdict": verdict,
                    "rules_hit": len(triggered),
                    "synthetic": True,
                },
            )
            if verdict == "sandbox_like":
                await ctx.emit(
                    "anti_analysis",
                    "evasion.simulated",
                    self.name,
                    severity="warning",
                    data={
                        "scenario": scenario_id,
                        "action": "would-delay-execution",
                        "enforcement": "log_only",
                        "effect": "none",
                        "note": "decision is recorded, never applied",
                    },
                )

        ctx.write_json(
            "anti-analysis.json",
            {
                "enforcement": "log_only",
                "threshold": threshold,
                "assessments": assessments,
                "synthetic": True,
            },
        )
        await ctx.emit(
            "anti_analysis",
            "assessment.summary",
            self.name,
            data={
                "scenarios": len(assessments),
                "sandbox_like": sum(1 for a in assessments if a["verdict"] == "sandbox_like"),
                "enforcement": "log_only",
            },
        )

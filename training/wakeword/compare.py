"""Put two acceptance runs side by side.

The point of keeping the Atlas run is that "better" has to be shown, not
asserted. This reads two ``acceptance.json`` files and prints the comparison on
the axes that decide whether a wake word is usable: false activations per hour,
missed activations, the near-misses that actually fire, latency, and what it
costs to listen.

    uv run --group training python training/wakeword/compare.py \\
        --baseline metrics/atlas-experiment/acceptance.json \\
        --candidate metrics/acceptance.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"no acceptance report at {path}")
    return dict(json.loads(path.read_text(encoding="utf-8")))


def curve_row(report: dict[str, Any], threshold: float) -> dict[str, Any] | None:
    for row in report["operating_points"]:
        if abs(float(row["threshold"]) - threshold) < 1e-9:
            return dict(row)
    return None


def group(report: dict[str, Any], name: str) -> dict[str, Any]:
    return dict(report["by_group_at_threshold"]["groups"].get(name, {}))


def fmt(value: Any, spec: str = "") -> str:
    if value is None:
        return "—"
    return format(value, spec) if spec else str(value)


def print_curves(baseline: dict[str, Any], candidate: dict[str, Any]) -> None:
    print("\n## Threshold curve\n")
    print(
        f"{'threshold':>9} | {'baseline missed%':>16} {'baseline FA/h':>14}"
        f" | {'candidate missed%':>17} {'candidate FA/h':>15}"
    )
    print("-" * 84)
    thresholds = [float(row["threshold"]) for row in candidate["operating_points"]]
    for threshold in thresholds:
        before = curve_row(baseline, threshold)
        after = curve_row(candidate, threshold)
        print(
            f"{threshold:>9.2f} | "
            f"{fmt(before and before['missed_percent'], '16.2f')} "
            f"{fmt(before and before['false_activations_per_hour'], '14.2f')} | "
            f"{fmt(after and after['missed_percent'], '17.2f')} "
            f"{fmt(after and after['false_activations_per_hour'], '15.2f')}"
        )


def print_groups(baseline: dict[str, Any], candidate: dict[str, Any]) -> None:
    names = sorted(
        set(baseline["by_group_at_threshold"]["groups"])
        | set(candidate["by_group_at_threshold"]["groups"])
    )
    print("\n## By group, at the reported threshold\n")
    print(f"{'group':30} | {'baseline':>22} | {'candidate':>22}")
    print("-" * 82)
    for name in names:
        before, after = group(baseline, name), group(candidate, name)

        def describe(entry: dict[str, Any]) -> str:
            if not entry:
                return "—"
            if "recall" in entry:
                return f"recall {entry['recall']:.3f}"
            return f"{entry['false_activations_per_hour']:>9.1f} FA/h"

        print(f"{name:30} | {describe(before):>22} | {describe(after):>22}")


def print_headline(baseline: dict[str, Any], candidate: dict[str, Any]) -> None:
    print("\n## Headline\n")
    rows = [
        ("clips scored", str(baseline["clips"]), str(candidate["clips"])),
        (
            "latency median (s)",
            fmt(baseline["latency_seconds"]["median"]),
            fmt(candidate["latency_seconds"]["median"]),
        ),
        (
            "latency p90 (s)",
            fmt(baseline["latency_seconds"]["p90"]),
            fmt(candidate["latency_seconds"]["p90"]),
        ),
    ]
    for key, label in (
        ("realtime_factor", "realtime factor"),
        ("models_cost_mb", "models cost (MB)"),
        ("rss_after_listening_mb", "process RSS (MB)"),
    ):
        rows.append(
            (
                label,
                fmt(baseline["resources_while_listening"].get(key)),
                fmt(candidate["resources_while_listening"].get(key)),
            )
        )

    print(f"{'':24} | {'baseline':>14} | {'candidate':>14}")
    print("-" * 58)
    for label, before, after in rows:
        print(f"{label:24} | {before:>14} | {after:>14}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline", type=Path, default=HERE / "metrics" / "atlas-experiment" / "acceptance.json"
    )
    parser.add_argument("--candidate", type=Path, default=HERE / "metrics" / "acceptance.json")
    arguments = parser.parse_args()

    baseline = load(arguments.baseline)
    candidate = load(arguments.candidate)

    print(f"baseline : {arguments.baseline}  (model {baseline['model']})")
    print(f"candidate: {arguments.candidate}  (model {candidate['model']})")
    print(
        "\nNote: the two runs use different wake words, so the positive groups are "
        "not the same audio. What compares directly is the *shape* — how often it "
        "wakes when nobody called it, and how much it misses."
    )

    print_headline(baseline, candidate)
    print_curves(baseline, candidate)
    print_groups(baseline, candidate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

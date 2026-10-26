"""판단 원칙 조회·recall·재무장 명령 처리."""
from __future__ import annotations
import json
import argparse
from pm_engine.infra import pm_paths
from pathlib import Path
from pm_engine.infra import console
from pm_engine.infra import principle_store
from pm_engine.app import principle_recall
from pm_engine.domain import principle_rules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="판단 원칙 레지스트리 — 로더 + recall 판정 CLI(opencode subprocess 진입점)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_judge = sub.add_parser(
        "judge-recall", help="한 호출을 판정하고 marker 를 갱신한다."
    )
    p_judge.add_argument("--root", default=".")
    p_judge.add_argument("--on", required=True, choices=principle_rules.ON_VALUES)
    p_judge.add_argument("--text", default="")
    p_judge.add_argument("--session", default="unknown")
    p_count = sub.add_parser(
        "count", help="로드된 규칙 수(RECALL/JUDGMENT/파손)를 낸다."
    )
    p_count.add_argument("--root", default=".")
    p_rearm = sub.add_parser(
        "rearm", help="세션 marker 를 지워 다음 사이클을 재무장한다."
    )
    p_rearm.add_argument("--root", default=".")
    p_rearm.add_argument("--session", required=True)
    args = parser.parse_args(argv)
    home = pm_paths.home_at(Path(args.root).resolve())
    if args.command == "judge-recall":
        seen = principle_store.load_seen_marker(home, args.session)
        result = principle_recall.judge_recall(
            home, on=args.on, text=args.text, seen=seen
        )
        if result is None:
            result = {"count": 0, "keys": [], "text": ""}
        elif result.get("keys"):
            principle_store.record_seen_marker(home, args.session, result["keys"])
        console.write_machine_line(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "count":
        rules, broken = principle_store._load_all(home)
        recall = sum((1 for rule in rules if rule.on is not None))
        console.write_machine_line(
            json.dumps(
                {
                    "rules": len(rules),
                    "recall": recall,
                    "judgment": len(rules) - recall,
                    "broken": broken,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "rearm":
        principle_store.rearm_seen_marker(home, args.session)
        console.write_machine_line(json.dumps({"rearmed": True}, ensure_ascii=False))
        return 0
    return 2

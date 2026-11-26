"""Print a candidate standard snapshot for review and packaging in Git."""
import argparse
import json
from pathlib import Path

from auto_eval.config import load_config
from auto_eval.web.operation_runs import evaluation_snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=Path(__file__).resolve().parents[1] / "config")
    args = parser.parse_args()
    print(json.dumps(evaluation_snapshot(load_config(args.config_dir)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

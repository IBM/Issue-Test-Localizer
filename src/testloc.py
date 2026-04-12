import argparse
import os

import localization
from utils import get_dataset


def parse_args():
    parser = argparse.ArgumentParser(
        description="TestLoc Step 1: Suspicious function localization via LLM.",
    )
    parser.add_argument(
        "--instance_id",
        required=False,
        help="Single instance to inspect. If not provided, runs on all instances.",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="LLM model name",
    )
    parser.add_argument(
        "--dataset",
        default="princeton-nlp/SWE-bench_Verified",
        help="HuggingFace dataset name (default: princeton-nlp/SWE-bench_Verified).",
    )
    parser.add_argument(
        "--log_dir",
        default="output",
        help="Directory to store logs and results (default: output).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    os.makedirs(args.log_dir, exist_ok=True)

    dataset = get_dataset(args.dataset)
    log_dir = f'{args.log_dir}/{args.dataset}/{args.model.replace("/", "_")}'

    localization.main(args.model, args.dataset, dataset, log_dir, args.instance_id)

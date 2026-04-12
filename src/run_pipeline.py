#!/usr/bin/env python3

import argparse
import glob
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def parse_args():
    p = argparse.ArgumentParser(description="Run full TestLoc pipeline.")
    p.add_argument("--instance_id", help="Single instance ID (omit to run all)")
    p.add_argument("--model", required=True, help="LLM model name")
    p.add_argument("--dataset", default="princeton-nlp/SWE-bench_Verified")
    p.add_argument("--log_dir", default="output", help="Root output directory")
    p.add_argument("--strategy", default="greedy_additional",
                   choices=["greedy_additional", "greedy_total"])
    p.add_argument("--skip_step1", action="store_true", help="Skip Step 1 if suspicious_funcs.json exists")
    p.add_argument("--skip_step2", action="store_true", help="Skip Step 2 if test_file_selection.json exists")
    p.add_argument("--skip_coverage", action="store_true", help="Skip Step 3 if coverage data exists")
    p.add_argument("--max_workers", type=int, default=4, help="Parallel workers for coverage generation")
    return p.parse_args()


def main():
    args = parse_args()

    model_safe = args.model.replace("/", "_")
    dataset_short = args.dataset.split("/")[-1]

    # --- Directory layout ---
    step1_dir = os.path.join(args.log_dir, args.dataset, model_safe)
    results_dir = os.path.join(step1_dir, f"{model_safe}_results")
    suspicious_json = os.path.join(args.log_dir, "suspicious_funcs.json")
    step2_dir = os.path.join(args.log_dir, "model_test_files", dataset_short, args.model)
    test_selection_json = os.path.join(step2_dir, "test_file_selection.json")
    cov_dir = os.path.join(args.log_dir, "coverage", f"{dataset_short}_{model_safe}")
    minimized_json = os.path.join(cov_dir, "minimized_tests.json")

    os.makedirs(step1_dir, exist_ok=True)
    os.makedirs(results_dir, exist_ok=True)

    # ================================================================
    # Step 1: Suspicious Function Localization
    # ================================================================
    if args.skip_step1 and os.path.exists(suspicious_json):
        logging.info(f"[Step 1] SKIPPED — {suspicious_json} exists")
    else:
        logging.info("=" * 70)
        logging.info("[Step 1] Suspicious Function Localization")
        logging.info("=" * 70)

        from utils import get_dataset
        import localization

        dataset = get_dataset(args.dataset)
        localization.main(args.model, args.dataset, dataset, step1_dir, args.instance_id)

        # Build combined suspicious_funcs.json from per-instance results
        combined = {}
        for f in glob.glob(os.path.join(results_dir, "*.json")):
            with open(f) as fh:
                data = json.load(fh)
            for iid, info in data.items():
                if isinstance(info, dict) and "normalized_funcs" in info:
                    nf = info["normalized_funcs"]
                    if nf and any(v for v in nf.values()):
                        combined[iid] = nf

        with open(suspicious_json, "w") as fh:
            json.dump(combined, fh, indent=2)
        logging.info(f"[Step 1] Saved {len(combined)} instances to {suspicious_json}")

    # Verify
    with open(suspicious_json) as f:
        suspicious = json.load(f)
    if args.instance_id and args.instance_id not in suspicious:
        logging.error(f"[Step 1] Instance {args.instance_id} not found in suspicious_funcs.json")
        sys.exit(1)
    logging.info(f"[Step 1] Suspicious functions for {len(suspicious)} instance(s)")

    # ================================================================
    # Step 2: Test File Selection
    # ================================================================
    if args.skip_step2 and os.path.exists(test_selection_json):
        logging.info(f"[Step 2] SKIPPED — {test_selection_json} exists")
    else:
        logging.info("=" * 70)
        logging.info("[Step 2] Test File Selection")
        logging.info("=" * 70)

        from test_file_selection import main as tfs_main, get_dataset as tfs_get_dataset
        from pathlib import Path
        from utils import read_json_file

        os.makedirs(step2_dir, exist_ok=True)
        all_results_json = Path(step2_dir) / "model_selected.json"
        confirmed_json = Path(step2_dir) / "confirmed_suspicious_funcs.json"

        tfs_main(
            suspicious_info=suspicious,
            all_results_json=all_results_json,
            test_selection_json=Path(test_selection_json),
            confirmed_suspicious_file=confirmed_json,
            dataset_name=args.dataset,
            model_name=args.model,
        )

    # Verify
    with open(test_selection_json) as f:
        selections = json.load(f)
    logging.info(f"[Step 2] Test files selected for {len(selections)} instance(s)")

    # ================================================================
    # Step 3: Coverage Generation
    # ================================================================
    if args.skip_coverage:
        logging.info(f"[Step 3] SKIPPED via --skip_coverage")
    else:
        logging.info("=" * 70)
        logging.info("[Step 3] Coverage Generation (Docker)")
        logging.info("=" * 70)

        from generate_cov_batch import run_coverage_batch
        from utils import read_json_to_dict

        only_run = {args.instance_id} if args.instance_id else None

        run_coverage_batch(
            dataset_name=args.dataset,
            suspicious_funcs=suspicious,
            selected_tests=selections,
            cov_dir=cov_dir,
            max_workers=args.max_workers,
            run_id="testloc_cov",
            only_run=only_run,
        )

    # ================================================================
    # Step 4: Test Minimization
    # ================================================================
    logging.info("=" * 70)
    logging.info("[Step 4] Test Minimization")
    logging.info("=" * 70)

    from test_minimization import main as tm_main, read_json_file
    from datasets import load_dataset

    dataset = load_dataset(args.dataset)["test"]
    min_log_dir = os.path.join(cov_dir, "minimization_logs")
    os.makedirs(min_log_dir, exist_ok=True)

    prev_results = {}
    if os.path.exists(minimized_json):
        with open(minimized_json) as f:
            prev_results = json.load(f)

    rerun = [args.instance_id] if args.instance_id else None

    tm_main(
        coverage_dir=cov_dir,
        test_log_dir=cov_dir,
        suspicious_info=suspicious,
        dataset=dataset,
        log_dir=min_log_dir,
        model_name=args.model,
        result_json=minimized_json,
        prev_results=prev_results,
        rerun_instances=rerun,
        strategy=args.strategy,
    )

    # ================================================================
    # Summary
    # ================================================================
    logging.info("=" * 70)
    logging.info("PIPELINE COMPLETE")
    logging.info("=" * 70)

    with open(minimized_json) as f:
        results = json.load(f)

    for iid, tests in results.items():
        logging.info(f"  {iid}: {len(tests)} minimized tests")
        for t in tests:
            logging.info(f"    {t}")

    logging.info("")
    logging.info(f"Output directory: {args.log_dir}")
    logging.info(f"  suspicious_funcs.json:    {suspicious_json}")
    logging.info(f"  test_file_selection.json: {test_selection_json}")
    logging.info(f"  coverage artifacts:       {cov_dir}/")
    logging.info(f"  minimized_tests.json:     {minimized_json}")


if __name__ == "__main__":
    main()

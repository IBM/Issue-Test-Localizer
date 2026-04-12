from rank_bm25 import BM25Okapi
import argparse
import json

def load_issue(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

def load_tests(path, is_json=False):
    with open(path, "r", encoding="utf-8") as f:
        if is_json:
            return json.load(f)
        else:
            return [line.strip() for line in f if line.strip()]

def main():
    parser = argparse.ArgumentParser(description="BM25 retrieval for tests given an issue description.")
    parser.add_argument("--issue", required=True, help="Path to issue description text file")
    parser.add_argument("--tests", required=True, help="Path to tests file (one per line or JSON list)")
    parser.add_argument("--json", action="store_true", help="Indicate if tests file is JSON list")
    parser.add_argument("--top", nargs="*", type=int, default=[5, 10, 15, 20],
                        help="Cutoffs for top-k retrieval (default: 5 10 15 20)")
    args = parser.parse_args()

    issue_text = load_issue(args.issue)
    tests = load_tests(args.tests, is_json=args.json)

    # Tokenize 
    tokenized_corpus = [t.split() for t in tests]
    tokenized_query = issue_text.split()

    # BM25
    bm25 = BM25Okapi(tokenized_corpus)
    scores = bm25.get_scores(tokenized_query)

    ranked = sorted(zip(tests, scores), key=lambda x: x[1], reverse=True)

    for k in args.top:
        print(f"\nTop {k} tests:")
        for i, (test, score) in enumerate(ranked[:k], 1):
            print(f"{i:2d}. (score={score:.4f}) {test}")

if __name__ == "__main__":
    main()

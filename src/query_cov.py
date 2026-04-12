import coverage
import logging
import os

def query_coverage_data(coverage_file, code_file, linenos, logger=None):
        
    if not os.path.exists(coverage_file):
        if logger:
            logger.error(f"Coverage data does not exist: {coverage_file}")
        return None

    cov = coverage.Coverage(coverage_file)
    cov.load()
    data = cov.get_data()

    if not data.measured_files():
        if logger:
            logger.warning("No files measured in coverage data.")
        return None

    if code_file not in data.measured_files():
        return {}

    contexts_by_lineno = data.contexts_by_lineno(code_file)
    all_contexts = {
        lineno: contexts
        for lineno, contexts in contexts_by_lineno.items()
        if lineno in linenos
    }

    return all_contexts
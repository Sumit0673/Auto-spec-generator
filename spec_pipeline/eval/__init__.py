"""Evaluation subsystem for the spec pipeline (design: R11-R14).

This package holds the Evaluation_Harness components that score generated CVL
specifications against the human-written ground truth in ``Paired_Dataset``:

* :mod:`spec_pipeline.eval.pair_index` - discovers matched ``.sol``/``.spec``
  pairs, orders them, round-trips the index, and extracts each
  Ground_Truth_Property from a human spec (Requirement 11).
* :mod:`spec_pipeline.eval.metrics` - the Metric_Calculator computing the six
  evaluation metrics as numerator/denominator/quotient or null+reason
  (Requirement 12).
* :mod:`spec_pipeline.eval.gate` - the Quality_Gate comparing computed metrics
  against version-controlled floors and the Evaluation_Baseline, with a
  provisional-floor bootstrap (Requirement 14).
* :mod:`spec_pipeline.eval.harness` - the Evaluation_Harness running the
  Spec_Pipeline once per Pair_Index entry with per-pair timeout/error
  isolation, ``--jobs`` concurrency, ``--resume``, and machine + human reports
  stamped with a run identifier (Requirement 13).
"""

"""Candidate serialisation tests: variants.json payload vs labels.json payload.

These lock down the file-format contract between the builder and
`benchmark.dataset._load_variants`, and the rule that `ac_but_invalid` is
*derived*, never hand-declared.
"""
from hy3_algoaudit.builder import candidates as C

STEPS = {f"S{i}": f"content {i}" for i in range(1, 8)}


def _candidate(kind: str, *, verdict: str, process_valid, suffix: str = "") -> dict:
    candidate = C.make_candidate(
        "p1", kind, STEPS,
        code_source="model",
        model_meta={"model": "hy3"},
        judge={"verdict": verdict, "passed": 3, "total": 30, "comparison": "token"},
        suffix=suffix,
    )
    candidate["label"] = {
        "kind": kind,
        "expected_process_valid": process_valid,
        "first_error": "S3" if process_valid is False else None,
        "error_type": "E2" if process_valid is False else None,
        "label_confidence": "exact",
        "label_basis": "由程序拼接保证",
    }
    return candidate


def test_variant_name_and_payload_has_no_label():
    candidate = _candidate(C.KIND_INJECTED, verdict="WA", process_valid=False,
                           suffix="-E2-S3")
    assert C.variant_name(candidate) == "injected-E2-S3"

    payload = C.variant_payload(candidate)
    # exactly what _load_variants reads
    assert set(payload) >= {"name", "language", "code", "steps"}
    assert payload["name"] == "injected-E2-S3"
    assert payload["language"] == "python"
    assert payload["steps"] == STEPS
    # the whole point: no ground truth leaks into the public file
    assert "label" not in payload
    assert "process_valid" not in payload
    assert "first_error" not in payload
    assert payload["judge"]["exec_status"] == "WA"


def test_label_payload_canonical_fields():
    candidate = _candidate(C.KIND_INJECTED, verdict="WA", process_valid=False,
                           suffix="-E2-S3")
    label = C.label_payload(candidate)
    assert label["kind"] == "injected"
    assert label["exec_status"] == "WA"
    assert label["process_valid"] is False
    assert label["first_error"] == "S3"
    assert label["error_type"] == "E2"
    assert label["ac_but_invalid"] is False  # WA, so not "AC but invalid"
    assert label["confidence"] == "exact"
    assert label["model"] == {"model": "hy3"}


def test_ac_but_invalid_is_derived():
    """process invalid + code AC => AC-but-Invalid, with no extra declaration."""
    ac_invalid = _candidate(C.KIND_AC_INVALID, verdict="AC", process_valid=False,
                            suffix="-S5")
    assert C.label_payload(ac_invalid)["ac_but_invalid"] is True

    correct = _candidate(C.KIND_CORRECT, verdict="AC", process_valid=True)
    assert C.label_payload(correct)["ac_but_invalid"] is False

    natural = _candidate(C.KIND_NATURAL, verdict="AC", process_valid=None)
    label = C.label_payload(natural)
    # no ground truth => stay silent instead of claiming "not invalid"
    assert label["process_valid"] is None
    assert label["ac_but_invalid"] is False


def test_public_and_private_kinds_are_partitioned():
    public = {C.KIND_CORRECT, C.KIND_NATURAL}
    private = {C.KIND_INJECTED, C.KIND_AC_INVALID}
    assert set(C.PUBLIC_KINDS) == public
    assert set(C.PRIVATE_KINDS) == private
    assert not (public & private)

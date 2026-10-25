import pytest

from evaluation.p5.report import build, junit_record


def test_junit_missing_or_skipped_is_not_passing_evidence(tmp_path):
    path = tmp_path / "test.xml"
    path.write_text('<testsuite><testcase><skipped/></testcase></testsuite>')
    assert junit_record(path)["status"] == "INSUFFICIENT_EVIDENCE"
    path.write_text('<testsuite><testcase><error/></testcase></testsuite>')
    assert junit_record(path)["status"] == "FAIL"
    with pytest.raises(FileNotFoundError):
        junit_record(tmp_path / "missing.xml")


def test_empty_or_local_only_experiment_cannot_release():
    report = dict(results=[], remote=[], metadata={}, comparison={"status": "PASS"},
                  quality={"clear_offer_recall": 1.0, "mixed_intent_coverage": 1.0})
    value = build(report, [], [])
    assert value["release_status"] == "INSUFFICIENT_EVIDENCE"
    assert value["components"]["development_200"] != "PASS"

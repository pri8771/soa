import argparse
import json
from pathlib import Path

from soa_worker.evaluation_cli import _run


async def test_offline_smoke_evaluation_is_executable(capsys) -> None:  # type: ignore[no-untyped-def]
    status = await _run(argparse.Namespace(dataset=None, predictions=None, state=None, output=None))
    report = json.loads(capsys.readouterr().out)
    assert status == 0
    assert report["documents_scored"] == 1
    assert report["field_exact_rate"] == 1.0
    assert report["line_cell_exact_rate"] == 1.0


async def test_missing_prediction_fails_and_writes_resumable_state(tmp_path: Path) -> None:
    dataset = tmp_path / "gold.json"
    predictions = tmp_path / "predictions.json"
    state = tmp_path / "state.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "document_sha256": "b" * 64,
                    "split": "test",
                    "ground_truth": {"fields": {"po_number": "PO-1"}},
                }
            ]
        )
    )
    predictions.write_text("{}")
    status = await _run(
        argparse.Namespace(
            dataset=str(dataset), predictions=str(predictions), state=str(state), output=None
        )
    )
    assert status == 1
    assert "candidate produced no prediction" in state.read_text()

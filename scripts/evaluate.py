"""Run sample files through the authenticated API and score against an answer key."""

from __future__ import annotations

import argparse
import json
import smtplib
import time
from collections import Counter, defaultdict
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import httpx

TERMINAL_STATUSES = {
    "processed",
    "extracted",
    "needs_vendor_review",
    "other",
    "ignored",
    "duplicate_blocked",
    "incomplete",
    "failed",
}
SUPPORTED_UPLOADS = {".pdf", ".png", ".jpg", ".jpeg"}


def _answer_rows(path: Path) -> dict[str, dict[str, Any]]:
    answer = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(answer, list):
        rows = answer
    elif isinstance(answer, dict) and isinstance(answer.get("cases"), list):
        rows = answer["cases"]
    elif isinstance(answer, dict):
        rows = [dict(value, filename=key) for key, value in answer.items()]
    else:
        raise ValueError("Answer key must be an object or a list of case records")
    return {str(row["filename"]): row for row in rows}


def _api_items(client: httpx.Client, path: str) -> list[dict[str, Any]]:
    response = client.get(path)
    response.raise_for_status()
    return response.json().get("items", [])


def _find_new_email_document(
    client: httpx.Client, previous_ids: set[str], timeout: float
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _api_items(client, "/api/documents?page_size=100&source=email_poll")
        added = [row for row in rows if str(row["id"]) not in previous_ids]
        if added:
            return added[0]
        time.sleep(1)
    raise TimeoutError("No document appeared for the submitted email before timeout")


def _submit_email(
    client: httpx.Client, path: Path, args, previous_ids: set[str]
) -> dict[str, Any]:
    if not args.smtp_host:
        raise ValueError(".eml samples require --smtp-host")
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    with smtplib.SMTP(args.smtp_host, args.smtp_port, timeout=20) as smtp:
        smtp.send_message(message)
    poll = client.post("/api/email-polling/run")
    poll.raise_for_status()
    poll_result = poll.json()
    try:
        return _find_new_email_document(client, previous_ids, min(args.timeout, 3))
    except TimeoutError:
        if int(poll_result.get("duplicates", 0)) > 0:
            return {"result": "duplicate", "poll_result": poll_result}
        raise


def _wait_document(client: httpx.Client, document_id: str, timeout: float):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/documents/{document_id}")
        response.raise_for_status()
        latest = response.json()
        if latest.get("status") in TERMINAL_STATUSES:
            return latest
        time.sleep(1)
    raise TimeoutError(
        f"Document {document_id} did not reach a terminal status; last status="
        f"{latest.get('status') if latest else 'unknown'}"
    )


def _lookup_invoice(client, invoice_number: str | None):
    if not invoice_number:
        return None
    response = client.get(
        "/api/invoices", params={"search": invoice_number, "page_size": 100}
    )
    response.raise_for_status()
    return next(
        (
            row
            for row in response.json().get("items", [])
            if row.get("invoice_no") == invoice_number
        ),
        None,
    )


def _score(
    records: list[dict[str, Any]], expected_rows: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    field_totals: Counter[str] = Counter()
    field_correct: Counter[str] = Counter()
    match_total = match_correct = missed_duplicates = missed_overbilling = 0
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        expected = expected_rows.get(record["filename"], {})
        actual_fields = record.get("extracted_json") or {}
        for field, expected_value in (expected.get("fields") or {}).items():
            field_totals[field] += 1
            if actual_fields.get(field) == expected_value:
                field_correct[field] += 1
        expected_match = expected.get("match_result")
        actual_match = record.get("match_result")
        if expected_match is not None:
            match_total += 1
            confusion[str(expected_match)][str(actual_match or "no_invoice")] += 1
            match_correct += expected_match == actual_match
        if expected.get("duplicate") and record.get("result") != "duplicate":
            missed_duplicates += 1
        if expected.get("overbilled") and actual_match not in {
            "match_issues",
            "no_match",
        }:
            missed_overbilling += 1
    fields = {
        name: {
            "correct": field_correct[name],
            "total": total,
            "accuracy": field_correct[name] / total if total else None,
        }
        for name, total in sorted(field_totals.items())
    }
    return {
        "fields": fields,
        "field_accuracy": (
            sum(field_correct.values()) / sum(field_totals.values())
            if field_totals
            else None
        ),
        "match_result_accuracy": match_correct / match_total if match_total else None,
        "match_correct": match_correct,
        "match_total": match_total,
        "missed_duplicates": missed_duplicates,
        "missed_overbilling": missed_overbilling,
        "match_confusion": {
            key: dict(value) for key, value in sorted(confusion.items())
        },
    }


def _write_reports(report_dir: Path, report: dict[str, Any]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "evaluation.json").write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    lines = [
        "# AP-Core sample evaluation",
        "",
        f"- Bills submitted: {report['submitted']} (terminal: {report['terminal']})",
        f"- Elapsed: {report['elapsed_seconds']:.2f}s",
        (
            f"- Time for 100 bills: {report['time_for_100_seconds']:.2f}s "
            f"({report['time_for_100_basis']})"
        ),
        f"- Field accuracy: {report['scores']['field_accuracy']}",
        f"- Match accuracy: {report['scores']['match_result_accuracy']}",
        f"- Missed duplicates: {report['scores']['missed_duplicates']}",
        f"- Missed overbilling: {report['scores']['missed_overbilling']}",
        f"- Worker concurrency: {report['worker_concurrency']} "
        "(documents worker pool: prefork)",
        "",
        "## Per-field accuracy",
        "",
        "| Field | Correct | Total | Accuracy |",
        "|---|---:|---:|---:|",
    ]
    for field, values in report["scores"]["fields"].items():
        lines.append(
            f"| {field} | {values['correct']} | {values['total']} "
            f"| {values['accuracy']} |"
        )
    lines.extend(
        [
            "",
            "## Match confusion table",
            "",
            "| Expected \\ Actual |"
            + "".join(
                f" {name} |"
                for name in sorted(
                    {
                        col
                        for row in report["scores"]["match_confusion"].values()
                        for col in row
                    }
                )
            ),
            "|---|"
            + "---:|"
            * len(
                {
                    col
                    for row in report["scores"]["match_confusion"].values()
                    for col in row
                }
            ),
        ]
    )
    actuals = sorted(
        {col for row in report["scores"]["match_confusion"].values() for col in row}
    )
    for expected, row in report["scores"]["match_confusion"].items():
        lines.append(
            f"| {expected} |"
            + "".join(f" {row.get(actual, 0)} |" for actual in actuals)
        )
    lines.extend(["", "## Failures", ""])
    lines.extend(
        f"- {failure['filename']}: {failure['reason']}"
        for failure in report["failures"]
    )
    (report_dir / "evaluation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument(
        "--token", required=True, help="Bearer token for the evaluation user"
    )
    parser.add_argument("--samples-dir", type=Path, required=True)
    parser.add_argument("--answer-key", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, default=Path("evaluation-report"))
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--smtp-host")
    parser.add_argument("--smtp-port", type=int, default=3025)
    parser.add_argument("--baseline-report", type=Path)
    args = parser.parse_args(argv)
    if not args.samples_dir.is_dir() or not args.answer_key.is_file():
        parser.error("--samples-dir must exist and --answer-key must be a file")
    expected = _answer_rows(args.answer_key)
    samples = sorted(
        path
        for path in args.samples_dir.iterdir()
        if path.is_file()
        and (
            path.suffix.casefold() in SUPPORTED_UPLOADS
            or path.suffix.casefold() == ".eml"
        )
    )
    if not samples:
        parser.error("No PDF, image, or EML samples were found")

    started = time.monotonic()
    records: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    headers = {"Authorization": f"Bearer {args.token}"}
    with httpx.Client(
        base_url=args.api_url.rstrip("/"), headers=headers, timeout=30
    ) as client:
        for path in samples:
            record: dict[str, Any] = {"filename": path.name}
            try:
                if path.suffix.casefold() == ".eml":
                    before = {
                        str(row["id"])
                        for row in _api_items(
                            client, "/api/documents?page_size=100&source=email_poll"
                        )
                    }
                    summary = _submit_email(client, path, args, before)
                    if summary.get("result") == "duplicate":
                        record["result"] = "duplicate"
                        records.append(record)
                        continue
                    record["document_id"] = str(summary["id"])
                else:
                    response = client.post(
                        "/api/documents/upload",
                        files={"file": (path.name, path.read_bytes())},
                    )
                    response.raise_for_status()
                    result = response.json()
                    record["result"] = result.get("results", [{}])[0].get("result")
                    document_id = result.get("id") or result.get("results", [{}])[
                        0
                    ].get("document_id")
                    if not document_id:
                        records.append(record)
                        continue
                    record["document_id"] = document_id
                detail = _wait_document(client, record["document_id"], args.timeout)
                record.update(
                    status=detail.get("status"),
                    doc_type=detail.get("doc_type"),
                    failure_message=detail.get("failure_message"),
                    extracted_json=detail.get("extracted_json") or {},
                    step_log_count=sum(
                        item.get("action") == "pipeline_step"
                        for item in detail.get("audit_history", [])
                    ),
                )
                invoice_number = (record["extracted_json"] or {}).get("invoice_number")
                invoice = _lookup_invoice(client, invoice_number)
                if invoice:
                    record["invoice_id"] = str(invoice["invoice_id"])
                    record["match_result"] = invoice.get("match_status")
                    state = client.get(
                        f"/api/invoices/{invoice['invoice_id']}/state-history"
                    )
                    state.raise_for_status()
                    record["state_log_count"] = len(state.json())
                    timeline = client.get(
                        f"/api/invoices/{invoice['invoice_id']}/timeline"
                    )
                    timeline.raise_for_status()
                    record["step_log_count"] = sum(
                        bool(item.get("step_name")) for item in timeline.json()
                    )
                    if not record["state_log_count"] or not record["step_log_count"]:
                        failures.append(
                            {
                                "filename": path.name,
                                "reason": (
                                    "Bill is missing a state history or "
                                    "pipeline step log"
                                ),
                            }
                        )
                records.append(record)
                if detail.get("status") not in TERMINAL_STATUSES:
                    failures.append(
                        {
                            "filename": path.name,
                            "reason": "Document did not reach a terminal status",
                        }
                    )
                elif detail.get("status") == "failed":
                    failures.append(
                        {
                            "filename": path.name,
                            "reason": detail.get("failure_message")
                            or "Processing failed",
                        }
                    )
            except Exception as exc:
                record["reason"] = f"{type(exc).__name__}: {exc}"
                records.append(record)
                failures.append({"filename": path.name, "reason": record["reason"]})
    elapsed = time.monotonic() - started
    score = _score(records, expected)
    count = len(samples)
    report = {
        "submitted": count,
        "terminal": sum(row.get("status") in TERMINAL_STATUSES for row in records),
        "elapsed_seconds": elapsed,
        "time_for_100_seconds": elapsed if count == 100 else (elapsed / count * 100),
        "time_for_100_basis": "measured 100-bill run"
        if count == 100
        else "linear estimate; run with 100 samples for a measured value",
        "worker_concurrency": int(__import__("os").getenv("WORKER_CONCURRENCY", "2")),
        "throughput_before_seconds": None,
        "throughput_after_seconds": elapsed if count == 100 else None,
        "baseline_report": str(args.baseline_report) if args.baseline_report else None,
        "scores": score,
        "state_and_step_log_coverage": {
            "bills": sum(bool(row.get("invoice_id")) for row in records),
            "with_state_log": sum(bool(row.get("state_log_count")) for row in records),
            "with_step_log": sum(bool(row.get("step_log_count")) for row in records),
        },
        "failures": failures,
        "records": records,
        "targets": {
            "field_accuracy": 0.95,
            "match_accuracy": 0.95,
            "missed_duplicates": 0,
            "seconds_for_100": 300,
        },
    }
    if args.baseline_report and args.baseline_report.is_file():
        baseline = json.loads(args.baseline_report.read_text(encoding="utf-8"))
        report["throughput_before_seconds"] = baseline.get("time_for_100_seconds")
    _write_reports(args.report_dir, report)
    print(f"Evaluation report: {args.report_dir / 'evaluation.md'}")
    print(f"Evaluation JSON: {args.report_dir / 'evaluation.json'}")
    print(
        "PASS coverage: "
        f"state={report['state_and_step_log_coverage']['with_state_log']}/"
        f"{report['state_and_step_log_coverage']['bills']}, step="
        f"{report['state_and_step_log_coverage']['with_step_log']}/"
        f"{report['state_and_step_log_coverage']['bills']}"
    )
    thresholds_met = (
        (score["field_accuracy"] or 0) >= 0.95
        and (score["match_result_accuracy"] or 0) >= 0.95
        and score["missed_duplicates"] == 0
        and score["missed_overbilling"] == 0
        and report["time_for_100_seconds"] < 300
        and report["state_and_step_log_coverage"]["with_state_log"]
        == report["state_and_step_log_coverage"]["bills"]
        and report["state_and_step_log_coverage"]["with_step_log"]
        == report["state_and_step_log_coverage"]["bills"]
        and not failures
    )
    print(f"{'PASS' if thresholds_met else 'FAIL'} evaluation targets")
    return 0 if thresholds_met else 1


if __name__ == "__main__":
    raise SystemExit(main())

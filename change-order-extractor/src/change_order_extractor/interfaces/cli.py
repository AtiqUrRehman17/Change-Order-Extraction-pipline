"""Typer command-line interface.

cox extract data/samples/co_001_clean.txt          one file, full table
cox extract data/samples/ --out outputs/            a folder, summary table
cox extract co.pdf --no-llm                         rules only (no API calls)
cox extract co.pdf --json                           print JSON (for scripts)
cox extract co.pdf --no-second-opinion              never ask a stronger Gemini model
cox evaluate                                        accuracy vs ground truth (rules only)
cox evaluate --mode all                             rules vs Gemini vs hybrid
cox calibrate                                       fit the confidence calibration
"""

import json
import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from change_order_extractor.config import Settings, get_settings
from change_order_extractor.evaluation import (
    DEFAULT_FOLDERS,
    MODES,
    Case,
    ModeReport,
    load_cases,
    markdown_report,
)
from change_order_extractor.evaluation import evaluate as evaluate_modes
from change_order_extractor.evaluation.calibrate import calibrate as fit_calibration
from change_order_extractor.evaluation.calibrate import calibration_report, collect_samples
from change_order_extractor.evaluation.report import MODE_TITLES, json_summary, summary_rows
from change_order_extractor.ingestion import IngestionError
from change_order_extractor.interfaces.display import batch_table, show_result
from change_order_extractor.pipeline import Pipeline, find_input_files, save_result
from change_order_extractor.schemas import ExtractionResult
from change_order_extractor.scoring.calibration import DEFAULT_CALIBRATION_FILE

app = typer.Typer(
    help="Extract validated, confidence-scored fields from construction change orders.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
errors = Console(stderr=True)


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show progress logs.")] = False,
) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=errors, show_path=False, show_time=False)],
    )


@app.command()
def extract(
    path: Annotated[Path, typer.Argument(help="A change-order file (.pdf/.txt) or a folder.")],
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Folder for the JSON results.")
    ] = Path("outputs"),
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Rules only: no Gemini calls (scans can't be read).")
    ] = False,
    model: Annotated[
        str | None, typer.Option("--model", help="Gemini model to use (overrides config).")
    ] = None,
    plain: Annotated[
        bool, typer.Option("--plain", help="Save only the values (no confidence/evidence).")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print results as JSON instead of tables.")
    ] = False,
    no_save: Annotated[bool, typer.Option("--no-save", help="Don't write JSON files.")] = False,
    no_second_opinion: Annotated[
        bool,
        typer.Option(
            "--no-second-opinion",
            help="Never ask a stronger Gemini model when rules and Gemini disagree.",
        ),
    ] = False,
) -> None:
    """Extract change-order fields from a file or every file in a folder."""
    files = find_input_files(path)
    if not files:
        errors.print(f"[red]No supported files found in {path}[/red]")
        raise typer.Exit(1)

    pipeline = Pipeline(cli_settings(model, no_second_opinion), use_llm=not no_llm)

    outcomes: list[tuple[str, ExtractionResult | IngestionError]] = []
    for file, outcome in pipeline.run_many(files):
        outcomes.append((file.name, outcome))
        if isinstance(outcome, IngestionError):
            errors.print(f"[red]Could not read {file.name}: {outcome}[/red]")
            continue
        if out is not None and not no_save:
            save(outcome, out, plain)
        if not as_json and len(files) == 1:
            show_result(console, outcome)

    results = [outcome for _, outcome in outcomes if isinstance(outcome, ExtractionResult)]
    if as_json:
        payload = [as_dict(result, plain) for result in results]
        console.print_json(json.dumps(payload[0] if len(files) == 1 and payload else payload))
    elif len(files) > 1:
        console.print(batch_table(outcomes))
        flagged = sum(result.needs_review for result in results)
        failed = len(outcomes) - len(results)
        console.print(f"{len(results)} processed, {flagged} need review, {failed} failed.")

    if out is not None and not no_save and results and not as_json:
        console.print(f"[dim]Saved JSON to {out}/[/dim]")
    if len(results) < len(outcomes):
        raise typer.Exit(1)


@app.command()
def evaluate(
    folders: Annotated[
        list[Path] | None,
        typer.Argument(help="Folders of documents with ground truth (default: samples + real)."),
    ] = None,
    mode: Annotated[
        list[str] | None,
        typer.Option(
            "--mode", "-m", help="rules | llm | hybrid | all. Repeat for several (default: rules)."
        ),
    ] = None,
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Folder for eval_report.md / eval_summary.json.")
    ] = Path("evaluation/reports"),
    results: Annotated[
        Path | None,
        typer.Option("--results", help="Folder for each document's JSON result, per mode."),
    ] = Path("outputs/eval"),
    model: Annotated[
        str | None, typer.Option("--model", help="Gemini model to use (overrides config).")
    ] = None,
    no_second_opinion: Annotated[
        bool,
        typer.Option(
            "--no-second-opinion",
            help="Never ask a stronger Gemini model when rules and Gemini disagree.",
        ),
    ] = False,
) -> None:
    """Measure accuracy, calibration and the review flag against ground truth."""
    modes = resolve_modes(mode or ["rules"])
    cases, skipped = load_cases(list(folders or DEFAULT_FOLDERS))
    if not cases:
        errors.print("[red]No documents with ground truth found.[/red]")
        raise typer.Exit(1)

    settings = cli_settings(model, no_second_opinion)

    def progress(current_mode: str, case: Case) -> None:
        errors.print(f"[dim]{current_mode:>6}  {case.group}/{case.name}[/dim]")

    reports = evaluate_modes(cases, modes, settings, results_dir=results, progress=progress)

    out.mkdir(parents=True, exist_ok=True)
    skipped_names = [path.name for path in skipped]
    (out / "eval_report.md").write_text(
        markdown_report(reports, skipped=skipped_names), encoding="utf-8"
    )
    (out / "eval_summary.json").write_text(json_summary(reports), encoding="utf-8")

    console.print(eval_table(reports))
    for report in reports:
        if report.mode != "rules" and report.gemini_answered < len(report.docs):
            console.print(
                f"[yellow]! {report.mode}: Gemini answered only {report.gemini_answered} of "
                f"{len(report.docs)} documents (no key, quota or outage); the rest used rules "
                f"or nothing.[/yellow]"
            )
    if skipped_names:
        console.print(f"[dim]Skipped (no ground truth): {', '.join(skipped_names)}[/dim]")
    console.print(f"[dim]Report: {out / 'eval_report.md'}[/dim]")


@app.command()
def calibrate(
    folders: Annotated[
        list[Path] | None,
        typer.Argument(help="Folders of documents with ground truth (default: samples + real)."),
    ] = None,
    mode: Annotated[
        list[str] | None,
        typer.Option("--mode", "-m", help="Modes to learn from (default: all)."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Where to save the table (default: the packaged one)."),
    ] = None,
    report: Annotated[
        Path, typer.Option("--report", help="Where to write calibration_report.md.")
    ] = Path("evaluation/reports/calibration_report.md"),
    no_second_opinion: Annotated[
        bool,
        typer.Option(
            "--no-second-opinion",
            help="Never ask a stronger Gemini model when rules and Gemini disagree.",
        ),
    ] = False,
) -> None:
    """Fit the confidence calibration from the evaluation documents (run `cox evaluate
    --mode all` first so Gemini's answers are cached)."""
    modes = resolve_modes(mode or ["all"])
    cases, _ = load_cases(list(folders or DEFAULT_FOLDERS))
    if not cases:
        errors.print("[red]No documents with ground truth found.[/red]")
        raise typer.Exit(1)

    def progress(current_mode: str, case: Case) -> None:
        errors.print(f"[dim]{current_mode:>6}  {case.group}/{case.name}[/dim]")

    settings = cli_settings(None, no_second_opinion)
    samples = collect_samples(cases, modes, settings, progress=progress)
    if not samples:
        errors.print("[red]No values to learn from.[/red]")
        raise typer.Exit(1)
    result = fit_calibration(samples)

    target = out or DEFAULT_CALIBRATION_FILE
    result.table.save(target)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(calibration_report(result), encoding="utf-8")

    table = Table(title="Calibration (leave-one-document-out)", title_justify="left")
    for column in ("Kind of value", "Values", "Right", "ECE raw", "ECE calibrated"):
        table.add_column(column, justify="left" if column == "Kind of value" else "right")
    for check in result.checks:
        table.add_row(
            check.group,
            str(check.values),
            str(check.right),
            f"{check.raw_ece:.3f}",
            f"{check.calibrated_ece:.3f}",
        )
    table.add_row(
        "all",
        str(len(result.samples)),
        str(sum(s.correct for s in result.samples)),
        f"{result.raw_ece:.3f}",
        f"{result.calibrated_ece:.3f}",
    )
    console.print(table)
    console.print(f"[dim]Saved the table to {target}[/dim]")
    console.print(f"[dim]Report: {report}[/dim]")


def cli_settings(model: str | None, no_second_opinion: bool) -> Settings:
    settings = get_settings()
    if model:
        settings = settings.model_copy(update={"gemini_model": model})
    if no_second_opinion:
        settings = settings.model_copy(update={"gemini_escalation_models": "none"})
    return settings


def resolve_modes(requested: list[str]) -> list[str]:
    modes: list[str] = []
    for item in requested:
        for name in MODES if item == "all" else [item]:
            if name not in MODES:
                errors.print(f"[red]Unknown mode {name!r}: use rules, llm, hybrid or all.[/red]")
                raise typer.Exit(2)
            if name not in modes:
                modes.append(name)
    return modes


def eval_table(reports: list[ModeReport]) -> Table:
    table = Table(title="Evaluation", title_justify="left")
    table.add_column("Metric")
    for report in reports:
        table.add_column(MODE_TITLES.get(report.mode, report.mode), justify="right")
    for row in summary_rows(reports):
        table.add_row(*row)
    return table


def as_dict(result: ExtractionResult, plain: bool) -> dict:
    if plain:
        return {
            "source_file": result.document.source_file,
            **result.fields.to_change_order().model_dump(mode="json"),
        }
    return result.model_dump(mode="json")


def save(result: ExtractionResult, out: Path, plain: bool) -> Path:
    if not plain:
        return save_result(result, out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{Path(result.document.source_file).stem}.json"
    path.write_text(json.dumps(as_dict(result, plain=True), indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    app()

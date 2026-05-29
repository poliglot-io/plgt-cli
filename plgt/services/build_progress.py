"""Progress tracking utilities for build operations.

This module contains pure functions for managing build progress display.
"""

from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from plgt.models.build_types import FileValidationSummary


def create_progress_tracker(console: Console) -> Progress:
    """Create and return a configured progress tracker.

    Args:
        console: Rich console instance

    Returns:
        Configured Progress instance
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    )


def validate_files_with_progress(
    progress: Progress,
    rdf_files: list[Path],
    validate_func,
) -> FileValidationSummary:
    """Validate RDF files with progress tracking.

    Args:
        progress: Progress tracker instance
        rdf_files: List of RDF file paths to validate
        validate_func: Function to validate individual files

    Returns:
        FileValidationSummary with validation results
    """
    task = progress.add_task("Validating RDF files...", total=len(rdf_files))
    valid_files = []
    invalid_files = []

    for file_path in rdf_files:
        is_valid, error_msg = validate_func(file_path)
        if is_valid:
            valid_files.append(file_path)
        else:
            invalid_files.append((file_path, error_msg))

        progress.update(task, advance=1)

    progress.remove_task(task)
    return FileValidationSummary(valid_files, invalid_files)


def display_validation_warnings(
    console: Console,
    invalid_files: list[tuple[Path, str]],
) -> None:
    """Display validation warnings for invalid files.

    Args:
        console: Rich console instance
        invalid_files: List of (file_path, error_message) tuples
    """
    if invalid_files:
        console.print(
            f"[yellow]Warning: {len(invalid_files)} invalid RDF files were skipped[/yellow]",
        )
        for file_path, error_msg in invalid_files:
            console.print(f"  [red]✗[/red] {file_path}: {error_msg}")


def display_build_success(console: Console) -> None:
    """Display build success message with statistics.

    Args:
        console: Rich console instance
        output_file: Path to the output file
        merged_graph: The merged RDF graph
        valid_files_count: Number of valid files processed
        total_files_count: Total number of files found
    """
    console.print("[green]✓ Matrix built successfully![/green]")

# src/dbt_colibri/cli/cli.py

import click
import os
import sys
from ..lineage_extractor.extractor import DbtColumnLineageExtractor
from ..report.generator import DbtColibriReportGenerator
from importlib.metadata import version, PackageNotFoundError


def _is_telemetry_enabled(cli_flag: bool) -> bool:
    """Telemetry is off by default in this build; only --enable-telemetry turns it on."""
    return bool(cli_flag)

COLIBRI_LOGO = r"""
 ______     ______     __         __     ______     ______     __    
/\  ___\   /\  __ \   /\ \       /\ \   /\  == \   /\  == \   /\ \   
\ \ \____  \ \ \/\ \  \ \ \____  \ \ \  \ \  __<   \ \  __<   \ \ \  
 \ \_____\  \ \_____\  \ \_____\  \ \_\  \ \_____\  \ \_\ \_\  \ \_\ 
  \/_____/   \/_____/   \/_____/   \/_/   \/_____/   \/_/ /_/   \/_/ 
"""

try:
    __version__ = version("dbt-colibri-teradata")
except PackageNotFoundError:
    __version__ = "unknown"

@click.group()
@click.version_option(__version__, prog_name="dbt-colibri-teradata")
def cli():
    """dbt-colibri CLI tool (Teradata build)"""
    click.echo(f"{COLIBRI_LOGO}\n")
    click.echo("Welcome to dbt-colibri for Teradata 🐦")

@cli.command("generate")
@click.option(
    "--output-dir",
    type=str,
    default="dist",
    help="Directory to save both JSON and HTML files (default: dist)"
)
@click.option(
    "--manifest",
    type=str,
    default="target/manifest.json",
    help="Path to dbt manifest.json file (default: target/manifest.json)"
)
@click.option(
    "--catalog", 
    type=str,
    default="target/catalog.json",
    help="Path to dbt catalog.json file (default: target/catalog.json)"
)
@click.option(
    "--debug",
    is_flag=True,
    default=False,
    help="Enable debug-level logging"
)
@click.option(
    "--light",
    is_flag=True,
    default=False,
    help="Enable light mode (excludes compiled_code from output for smaller file size)"
)
@click.option(
    "--enable-telemetry",
    is_flag=True,
    default=False,
    help="Let the generated HTML report send anonymous usage statistics "
         "(adapter type, node count) to the upstream colibri project. Off by default."
)

def generate_report(output_dir, manifest, catalog, debug, light, enable_telemetry):
    """Generate a dbt-colibri lineage report with both JSON and HTML output."""
    import logging
    from ..utils import log

    try:


        # Set up logging based on flag
        log_level = logging.DEBUG if debug else logging.INFO
        logger = log.setup_logging(level=log_level)

        telemetry_disabled = not _is_telemetry_enabled(enable_telemetry)

        if not os.path.exists(manifest):
            logger.error(f"❌ Manifest file not found at {manifest}")
            sys.exit(1)
        if not os.path.exists(catalog):
            logger.error(f"❌ Catalog file not found at {catalog}")
            sys.exit(1)

        logger.info("Loading dbt manifest and catalog...")
        extractor = DbtColumnLineageExtractor(manifest, catalog)

        # --- Log version info (matches what will end up in metadata) ---
        manifest_meta = extractor.manifest.get("metadata", {})
        adapter = manifest_meta.get("adapter_type", "unknown")
        dbt_version = manifest_meta.get("dbt_version", "unknown")
        project = manifest_meta.get("project_name", "unknown")

        logger.info(
            "Running with configuration:\n"
            f"         dbt-colbri version : {extractor.colibri_version}\n"
            f"         dbt version        : {dbt_version}\n"
            f"         SQL dialect        : {adapter}\n"
            f"         dbt project        : {project}"
        )

        logger.info("Extracting lineage data...")
        report_generator = DbtColibriReportGenerator(
            extractor, light_mode=light, disable_telemetry=telemetry_disabled
        )

        logger.info("Generating report...")
        report_generator.generate_report(output_dir=output_dir)
        click.echo("\n")
        click.echo("✅ Report completed!")
        click.echo(f"  📁 JSON: {output_dir}/colibri-manifest.json")
        click.echo(f"  🌐 HTML: {output_dir}/index.html")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Error: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    cli()

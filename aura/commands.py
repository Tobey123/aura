from __future__ import annotations

import sys
import os
import json
import time
import traceback
from pathlib import Path
from functools import partial
from typing import Union, Optional, Tuple, Generator, List

import click
from prettyprinter import pprint

from .package_analyzer import Analyzer
from .uri_handlers.base import URIHandler, ScanLocation

from . import config
from . import exceptions
from . import utils
from . import mirror
from . import typos
from .package import PypiPackage
from .analyzers.detections import Detection
from .output.base import ScanOutputBase, DiffOutputBase, InfoOutputBase


logger = config.get_logger(__name__)


def check_requirement(pkg):
    click.secho("Received payload from package manager, running security audit...")

    out_format = ScanOutputBase.from_uri("text")

    handler = URIHandler.from_uri(f"{pkg['path']}")
    try:
        metadata = {  # FIXME, add to item scan location
            "uri_input": "pkg_path",
            "source": "package_manager",
            "pm_data": pkg,
            "format": "plain",
            "min_score": 0,
        }
        hits = []

        for location in handler.get_paths(metadata=metadata):
            # print(f"Enumerating: {location}")
            hits.extend(scan_worker(location))

        typosquatting = typos.check_name(pkg["name"])
        if typosquatting:
            click.secho(
                "Possible typosquatting detected", fg="red", bold=True, blink=True
            )
            click.secho(
                f"Following {len(typosquatting)} packages with similar names has been found:"
            )
            for x in typosquatting:
                click.echo(f" - '{x}'")

    finally:
        handler.cleanup()
    sys.exit(1)


def scan_worker(item: ScanLocation) -> Generator[Detection, None, None]:
    if not item.location.exists():
        logger.error(f"Location '{item.str_location}' does not exists. Skipping")
        yield []
    else:
        sandbox = Analyzer(location=item)
        yield from sandbox.run()


def scan_uri(uri, metadata: Union[list, dict]=None, download_only: bool=False) -> List[Detection]:
    with utils.enrich_exception(uri, metadata):
        start = time.time()
        handler = None
        metadata = metadata or {}
        output_format = metadata.get("format", "text")
        all_hits = []

        if type(output_format) not in (list, tuple):
            output_format = (output_format,)

        formatters = [ScanOutputBase.from_uri(x, opts=metadata.get("output_opts")) for x in output_format]

        try:
            handler = URIHandler.from_uri(uri)

            if handler is None:
                raise ValueError(f"Could not find a handler for provided URI: '{uri}'")
            elif not handler.exists:
                raise exceptions.InvalidLocation(f"Invalid location provided from URI: '{uri}'")

            metadata.update({
                "name": uri,
                "uri_scheme": handler.scheme,
                "uri_input": handler.metadata,
                "depth": 0
            })

            # FIXME: metadata=metadata
            for x in handler.get_paths(metadata={"analyzers": metadata["analyzers"]}):  # type: ScanLocation
                if download_only:
                    continue
                else:
                    all_hits.extend(scan_worker(x))

            for formatter in formatters:
                try:
                    filtered_hits = formatter.filtered(all_hits)
                except exceptions.MinimumScoreNotReached:
                    pass
                else:
                    with formatter:
                        formatter.output(hits=filtered_hits, scan_metadata=metadata)

        except exceptions.NoSuchPackage:
            logger.warn(f"No such package: {uri}")
        except Exception:
            logger.exception(f"An error was thrown while processing URI: '{uri}'")
            raise
        finally:
            if handler:
                handler.cleanup()

        logger.info(f"Scan finished in {time.time() - start} s")
        return all_hits


def data_diff(a_path: str, b_path: str, format_uri=("text",), output_opts=None):
    try:
        from . import diff
    except exceptions.FeatureDisabled:
        traceback.print_exc(file=sys.stderr)
        sys.exit(2)

    if output_opts is None:
        output_opts = {}

    uri_handler1, uri_handler2 = URIHandler.diff_from_uri(a_path, b_path)

    if type(format_uri) not in (tuple, list):
        format_uri = (format_uri,)

    formatters = [DiffOutputBase.from_uri(x, opts=output_opts) for x in format_uri]

    if "detections" in output_opts:
        detections = output_opts["detections"]
    else:
        detections = any(x.detections for x in formatters)

    analyzer = diff.DiffAnalyzer()
    analyzer.compare(uri_handler1, uri_handler2, detections=detections)

    for formatter in formatters:
        with formatter:
            formatter.output_diff(analyzer)


def scan_mirror(output_dir: Path):
    mirror_pth = mirror.LocalMirror.get_mirror_path()
    click.echo("Collecting package names from a mirror")
    pkgs = list(x.name for x in Path(mirror_pth / "json").iterdir())
    click.echo(f"Collected {len(pkgs)} packages")
    click.echo("Spawning scanning workers")

    with click.progressbar(pkgs) as bar:
        for idx, pkg in enumerate(bar):
            uri = f"mirror://{pkg}"

            out_pth = output_dir / f"{pkg}.scan_results.json"

            metadata = {
                "format": "json",
                "output_path": os.fspath(out_pth),
                "fork": True
            }
            scan_uri(uri=uri, metadata=metadata)
            # executor.apply_async(
            #     func=scan_uri,
            #     kwds={"uri": uri, "metadata": metadata}
            # )


def parse_ast(path: Union[str, Path], stages: Optional[Tuple[str,...]]=None, format="text"):
    from .analyzers.python.visitor import Visitor

    if stages:
        if "raw" in stages and stages.index("raw") > 0:
            raise ValueError("The 'raw' ast stage must the first one if defined")

    meta = {"path": path, "source": "cli"}

    location = ScanLocation(location=path, metadata=meta)

    v = Visitor.run_stages(location=location, stages=stages)
    if format == "text":
        pprint(v.tree, indent=2)
    elif format == "json":
        print(json.dumps(v.tree, default=utils.json_encoder))


def show_info():
    """
    Collect and print information about the framework environment and plugins
    """
    from . import info
    info_data = info.gather_aura_information()
    formatter = InfoOutputBase.from_uri("text")
    formatter.output_info_data(info_data)


def generate_typosquatting(out, distance=2, limit=None):
    f = partial(typos.damerau_levenshtein, max_distance=distance)
    pth = config.get_pypi_stats_path()
    for num, (x, y) in enumerate(typos.enumerator(typos.generate_popular(pth), f)):
        if limit and num >= limit:
            break

        out.write(json.dumps({"original": x, "typosquatting": y}) + "\n")


def generate_r2c_input(out_file):
    inputs = []

    for pkg_name in PypiPackage.list_packages():
        try:
            pkg = PypiPackage.from_pypi(pkg_name)
        except exceptions.NoSuchPackage:
            continue
        targets = []

        input_definition = {
            "metadata": {"package": pkg_name},
            "input_type": "AuraInput",
        }

        for url in pkg.info["urls"]:
            targets.append({"url": url["url"], "metadata": url})

        input_definition["targets"] = json.dumps(targets)
        inputs.append(input_definition)

    out_file.write(
        json.dumps(
            {
                "name": "aura",
                "version": "0.0.1",
                "description": "This is a set of all PyPI packages",
                "inputs": inputs,
            }
        )
    )


def r2c_scan(source, out_file, mode="generic"):
    out = {"results": [], "errors": []}

    pkg_metadata = {}

    metadata = {"format": "none"}

    if mode == "pypi":
        logger.info("R2C mode set to PyPI")
        assert len(source) == 1
        location = Path(source[0])

        meta_loc = location / "metadata.json"
        if meta_loc.is_file():
            with open(location / "metadata.json", "r") as fd:
                pkg_metadata = json.loads(fd.read())
                metadata.update(
                    {
                        "package_type": pkg_metadata.get("packagetype"),
                        "package_name": pkg_metadata.get("name"),
                        "python_version": pkg_metadata.get("python_version"),
                    }
                )
        source = [
            os.fspath(x.absolute())
            for x in location.iterdir()
            if x.name != "metadata.json"
        ]
    else:
        logger.info("R2C mode set to generic")

    for src in source:
        logger.info(f"Enumerating {src} with metadata: {metadata}")

        try:
            data = scan_uri(src, metadata=metadata)

            for loc in data:
                for hit in loc["hits"]:
                    rhit = {"check_id": hit.pop("type"), "extra": hit}
                    if "line_no" in hit:
                        rhit["start"] = {"line": hit["line_no"]}
                        rhit["path"] = os.path.relpath(hit["location"], source[0])

                    out["results"].append(rhit)

        except Exception as exc:
            exc_tb = sys.exc_info()[-1]

            out["errors"].append(
                {
                    "message": f"[{exc_tb.tb_lineno}] An exception occurred: {str(exc)}",
                    "data": {"path": str(src)},
                }
            )

    pprint(out)
    out_file.write(json.dumps(out, default=utils.json_encoder))

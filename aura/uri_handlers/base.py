# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import atexit
import urllib.parse
import mimetypes
import tempfile
import shutil
import copy
import hashlib
from abc import ABC, abstractmethod
from itertools import product
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union, Optional, Generator, Tuple, Iterable
from warnings import warn

import tlsh
import pkg_resources
import magic

from .. import config
from ..utils import KeepRefs, lookup_lines
from ..exceptions import PythonExecutorError, UnsupportedDiffLocation, FeatureDisabled
from ..analyzers import find_imports
from ..analyzers.detections import DataProcessing, Detection, get_severity


logger = config.get_logger(__name__)
HANDLERS = {}
CLEANUP_LOCATIONS = set()


class URIHandler(ABC):
    scheme: str = "None"
    default = None

    def __init__(self, uri: urllib.parse.ParseResult):
        self.uri = uri

    @classmethod
    def is_supported(cls, parsed_uri):
        return parsed_uri.scheme == cls.scheme

    @classmethod
    def from_uri(cls, uri: str) -> Optional[URIHandler]:
        parsed = urllib.parse.urlparse(uri)
        cls.load_handlers()

        for handler in HANDLERS.values():
            if handler.is_supported(parsed):
                return handler(parsed)

        return cls.default(parsed)

    @classmethod
    def diff_from_uri(cls, uri1: str, uri2: str) -> Tuple[URIHandler, URIHandler]:
        cls.load_handlers()
        parsed1 = urllib.parse.urlparse(uri1)
        parsed2 = urllib.parse.urlparse(uri2)

        for handler1, handler2 in product(HANDLERS.values(), repeat=2):
            if handler1.is_supported(parsed1) and handler2.is_supported(parsed2):
                return (handler1(parsed1), handler2(parsed2))

        return (cls.default(parsed1), cls.default(parsed2))

    @classmethod
    def load_handlers(cls, ignore_disabled=True):
        global HANDLERS

        if not HANDLERS:
            handlers = {}
            for x in pkg_resources.iter_entry_points("aura.uri_handlers"):
                try:
                    hook = x.load()
                    handlers[hook.scheme] = hook
                    if hook.default and not cls.default:
                        cls.default = hook
                except FeatureDisabled as exc:
                    if not ignore_disabled:
                        handlers.setdefault("disabled", {})[x.name] = exc.args[0]

            HANDLERS = handlers
        return HANDLERS

    @property
    def metadata(self) -> dict:
        return {}

    @property
    def exists(self) -> bool:
        return True

    @abstractmethod
    def get_paths(self, metadata: Optional[dict]) -> Generator[ScanLocation, None, None]:
        ...

    def get_diff_paths(self, other: URIHandler) -> Generator[Tuple[ScanLocation, ScanLocation], None, None]:
        raise UnsupportedDiffLocation()

    def cleanup(self):
        pass


class PackageProvider(ABC):
    @property
    @abstractmethod
    def package(self):
        ...


@dataclass
class ScanLocation(KeepRefs):
    location: Union[Path, str]
    metadata: dict = field(default_factory=dict)
    cleanup: bool = False
    parent: Optional[str] = None
    strip_path: str = ""
    size: Optional[int] = None

    def __post_init__(self):
        if type(self.location) == str:
            self.__str_location = self.location
            self.location = Path(self.location)
        else:
            self.__str_location = os.fspath(self.location)

        if self.cleanup:
            CLEANUP_LOCATIONS.add(self.location)

        self.__str_parent = None
        self.metadata["path"] = self.location
        self.metadata["normalized_path"] = str(self)
        self.metadata["tags"] = set()

        if self.metadata.get("depth") is None:
            self.metadata["depth"] = 0
            warn("Depth is not set for the scan location", stacklevel=2)

        if self.location.is_file():
            self.__compute_hashes()

            self.size = self.location.stat().st_size
            self.metadata["mime"] = magic.from_file(self.str_location, mime=True)

            if self.metadata["mime"] in ("text/plain", "application/octet-stream", "text/none"):
                self.metadata["mime"] = mimetypes.guess_type(self.__str_location)[0]

            if self.is_python_source_code and "no_imports" not in self.metadata:
                try:
                    imports = find_imports.find_imports(self.location, metadata=self.metadata)
                    if imports:
                        self.metadata["py_imports"] = imports
                except PythonExecutorError:
                    pass


    def __compute_hashes(self):
        tl = tlsh.Tlsh()
        md5 = hashlib.md5()
        sha1 = hashlib.sha1()
        sha256 = hashlib.sha256()
        sha512 = hashlib.sha512()

        with self.location.open("rb") as fd:
            buffer = fd.read(4096)

            while buffer:
                tl.update(buffer)
                md5.update(buffer)
                sha1.update(buffer)
                sha256.update(buffer)
                sha512.update(buffer)
                buffer = fd.read(4096)

        try:
            tl.final()
            self.metadata["tlsh"] = tl.hexdigest()
        except ValueError:  # TLSH needs at least 256 bytes
            pass

        self.metadata["md5"] = md5.hexdigest()
        self.metadata["sha1"] = sha1.hexdigest()
        self.metadata["sha256"] = sha256.hexdigest()
        self.metadata["sha512"] = sha512.hexdigest()


    def __str__(self):
        return self.strip(self.str_location)

    @property
    def str_location(self) -> str:
        return self.__str_location

    @property
    def str_parent(self) -> Optional[str]:
        if self.parent is None:
            return None

        if self.__str_parent is None:
            if type(self.parent) == str:
                self.__str_parent = self.parent
            else:
                self.__str_parent = os.fspath(self.parent)

        return self.__str_parent

    @property
    def filename(self) -> Optional[str]:
        if self.location.is_file():
            return self.location.name
        else:
            return None

    @property
    def is_python_source_code(self) -> bool:
        return (self.metadata["mime"] in ("text/x-python", "text/x-script.python"))

    def create_child(self, new_location: Union[str, Path], metadata=None, **kwargs) -> ScanLocation:
        if metadata is None:
            metadata = copy.deepcopy(self.metadata)
            metadata["depth"] = self.metadata["depth"] + 1

        for x in ("mime", "interpreter_path", "interpreter_name"):
            metadata.pop(x, None)

        metadata["analyzers"] = self.metadata.get("analyzers")

        if type(new_location) == str:
            str_loc = new_location
            new_location = Path(new_location)
        else:
            str_loc = os.fspath(new_location)

        if "parent" in kwargs:
            parent = kwargs["parent"]
        elif self.location.is_dir():
            parent = self.parent
        else:
            parent = self.location

        if "strip_path" in kwargs:
            strip_path = kwargs["strip_path"]
        elif str_loc.startswith(os.fspath(tempfile.gettempdir())):
            strip_path = str_loc
        else:
            strip_path = self.strip_path

        child = ScanLocation(
            location=new_location,
            metadata=metadata,
            strip_path=strip_path,
            parent=parent,
            cleanup=kwargs.get("cleanup", False)
        )

        return child

    def strip(self, target: Union[str, Path]) -> str:
        """
        Strip/normalize given path
        Left side part of the target is replaced with the configured strip path
        This is to prevent temporary locations to appear in a part and are instead replaced with a normalize path
        E.g.:
        `/var/tmp/some_extracted_archive.zip/setup.py`
        would become:
        `some_extracted_archive.zip$setup.py`
        which signifies that the setup.py is inside the archive and leaves out the temporary unpack location

        :param target: Path to replace/strip
        :return: normalized path
        """
        target: str = os.fspath(target)

        if self.strip_path and target.startswith(self.strip_path):
            size = len(self.strip_path)
            if self.strip_path[-1] != "/":
                size += 1

            target = target[size:]

        if self.parent:
            if not target.startswith(self.str_parent):  # Target might be already stripped
                target = self.str_parent + "$" + target

        return target

    def should_continue(self) -> Union[bool, Detection]:
        """
        Determine if the processing of this scan location should continue
        Currently, the following reasons can halt the processing:
        - maximum depth was reached (recursive unpacking)

        :return: True if the processing should continue otherwise an instance of Rule that would halt the processing
        """
        max_depth = int(config.CFG["aura"].get("max-depth", 5))
        if self.metadata["depth"] > max_depth:
            d = DataProcessing(
                message = f"Maximum processing depth reached",
                extra = {
                    "reason": "max_depth",
                    "location": str(self)
                },
                location=self.location,
                signature = f"data_processing#max_depth#{str(self)}"
            )
            self.post_analysis([d])
            return d

        return True

    def pprint(self):
        from prettyprinter import pprint as pp
        pp(self)

    def post_analysis(self, detections: Iterable[Detection]):
        encoding = self.metadata.get("encoding") or "utf-8"
        line_numbers = [d.line_no for d in detections if d.line_no is not None and d.line is None]

        lines = lookup_lines(self.str_location, line_numbers, encoding=encoding)

        for d in detections:
            d.tags |= self.metadata["tags"]  # Lookup if we can remove this

            if d.location is None:
                d.location = str(self)
            else:
                d.location = self.strip(d.location)

            if d.scan_location is None:
                d.scan_location = self

            if d.line is None:
                line = lines.get(d.line_no)
                d.line = line

            if d._metadata is None:
                d._metadata = self.metadata

            if d._severity is None:
                d._severity = get_severity(d)


def cleanup_locations():
    """
    Iterate over all created locations and delete path tree for those marked with cleanup
    """
    for obj in ScanLocation.get_instances():  # type: ScanLocation
        if not obj.cleanup:
            continue

        if obj.location in CLEANUP_LOCATIONS:
            CLEANUP_LOCATIONS.remove(obj.location)

        if obj.location.exists():
            shutil.rmtree(obj.location)

    for location in CLEANUP_LOCATIONS:  # type: Path
        if location.exists():
            shutil.rmtree(location)


atexit.register(cleanup_locations)

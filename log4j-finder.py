#!/usr/bin/env python3
#
# file:     log4j-finder.py
# author:   NCC Group / Fox-IT / Research and Intelligence Fusion Team (RIFT)
#           filesystem recursing mods by gmoniker https://github.com/gmoniker all rights of original author retained
#
#  Scan the filesystem to find Log4j2 files that is vulnerable to Log4Shell (CVE-2021-44228)
#  It scans recursively both on disk and inside Java Archive files (JARs).
#
#  Example usage to scan a path (defaults to /):
#      $ python3 log4j-finder.py /path/to/scan
#
#  Or directly a JAR file:
#      $ python3 log4j-finder.py /path/to/jarfile.jar
#
#  Or multiple directories:
#      $ python3 log4j-finder.py /path/to/dir1 /path/to/dir2
#
import os
import io
import sys
import time
import zipfile
import logging
import argparse
import hashlib
import datetime
import functools
import itertools
import collections

from pathlib import Path

__version__ = "1.0.2"
FIGLET = f"""\
 __               _____  __         ___ __           __
|  |.-----.-----.|  |  ||__|______.'  _|__|.-----.--|  |.-----.----.
|  ||  _  |  _  ||__    |  |______|   _|  ||     |  _  ||  -__|   _|
|__||_____|___  |   |__||  |      |__| |__||__|__|_____||_____|__|
          |_____|      |___| v{__version__} https://github.com/fox-it/log4j-finder
"""

# Optionally import colorama to enable colored output for Windows
try:
    import colorama

    colorama.init()
    NO_COLOR = False
except ImportError:
    NO_COLOR = True if sys.platform == "win32" else False

log = logging.getLogger(__name__)

# Java Archive Extensions
JAR_EXTENSIONS = (".jar", ".war", ".ear")

# Filenames to find and MD5 hash (also recursively in JAR_EXTENSIONS)
# Currently we just look for JndiManager.class
FILENAMES = [
    p.lower()
    for p in [
        "JndiManager.class",
    ]
]

BLOCKED_DIRS = {
    ".git",
    ".cvs",
}

# Known BAD
MD5_BAD = {
    # JndiManager.class (source: https://github.com/nccgroup/Cyber-Defence/blob/master/Intelligence/CVE-2021-44228/modified-classes/md5sum.txt)
    "04fdd701809d17465c17c7e603b1b202": "log4j 2.9.0 - 2.11.2",
    "21f055b62c15453f0d7970a9d994cab7": "log4j 2.13.0 - 2.13.3",
    "3bd9f41b89ce4fe8ccbf73e43195a5ce": "log4j 2.6 - 2.6.2",
    "415c13e7c8505fb056d540eac29b72fa": "log4j 2.7 - 2.8.1",
    "5824711d6c68162eb535cc4dbf7485d3": "log4j 2.12.0 - 2.12.1",
    "6b15f42c333ac39abacfeeeb18852a44": "log4j 2.1 - 2.3",
    "8b2260b1cce64144f6310876f94b1638": "log4j 2.4 - 2.5",
    "a193703904a3f18fb3c90a877eb5c8a7": "log4j 2.8.2",
    "f1d630c48928096a484e4b95ccb162a0": "log4j 2.14.0 - 2.14.1",
    # 2.15.0 vulnerable to Denial of Service attack (source: https://cve.mitre.org/cgi-bin/cvename.cgi?name=CVE-2021-45046)
    "5d253e53fa993e122ff012221aa49ec3": "log4j 2.15.0",
}

# Known GOOD
MD5_GOOD = {
    # JndiManager.class (source: https://repo.maven.apache.org/maven2/org/apache/logging/log4j/log4j-core/2.16.0/log4j-core-2.16.0.jar)
    "ba1cf8f81e7b31c709768561ba8ab558": "log4j 2.16.0",
}


def md5_digest(fobj):
    """Calculate the MD5 digest of a file object."""
    d = hashlib.md5()
    for buf in iter(functools.partial(fobj.read, io.DEFAULT_BUFFER_SIZE), b""):
        d.update(buf)
    return d.hexdigest()

def iter_scandir(path, stats=None):
    """
    Yields all files matching JAR_EXTENSIONS or FILENAMES recursively in path
    Directories in BLOCKED_DIRS or beneath them are not recursed
    Any symlink (directory or file) is not considered for recursing or matching
    """
    p = Path(path)
    if p.is_file():
        if stats:
            stats["files"] += 1
        yield p
    try:
        for path, dirnames, filenames in os.walk(path):
            for name in filenames:
                entry = Path(os.path.join(path, name))
                if entry.is_symlink() or not entry.is_file():
                    continue
                log.debug(f"recursed: {os.path.join(path, name)}")
                if stats:
                    stats["files"] += 1
                if name.endswith(JAR_EXTENSIONS):
                    yield entry
                elif name in FILENAMES:
                    yield entry
                else:
                    continue
            for dirname in BLOCKED_DIRS:
                if dirname in dirnames:
                    dirnames.remove(dirname)
    except IOError as e:
        log.debug(e)

def iter_jarfile(fobj, parents=None, stats=None):
    """
    Yields (zfile, zinfo, zpath, parents) for each file in zipfile that matches `FILENAMES` or `JAR_EXTENSIONS` (recursively)
    """
    parents = parents or []
    try:
        with zipfile.ZipFile(fobj) as zfile:
            for zinfo in zfile.infolist():
                # log.debug(zinfo.filename)
                zpath = Path(zinfo.filename)
                if zpath.name.lower() in FILENAMES:
                    yield (zinfo, zfile, zpath, parents)
                elif zpath.name.lower().endswith(JAR_EXTENSIONS):
                    yield from iter_jarfile(
                        zfile.open(zinfo.filename), parents=parents + [zpath]
                    )
    except IOError as e:
        log.debug(f"{fobj}: {e}")
    except zipfile.BadZipFile as e:
        log.debug(f"{fobj}: {e}")


def red(s):
    if NO_COLOR:
        return s
    return f"\033[31m{s}\033[0m"


def green(s):
    if NO_COLOR:
        return s
    return f"\033[32m{s}\033[0m"


def yellow(s):
    if NO_COLOR:
        return s
    return f"\033[33m{s}\033[0m"


def bold(s):
    if NO_COLOR:
        return s
    return f"\033[1m{s}\033[0m"


def check_vulnerable(fobj, path_chain, stats):
    """
    Test if fobj matches any of the known bad or known good MD5 hashes.
    Also prints message if fobj is vulnerable or known good or unknown.
    """
    md5sum = md5_digest(fobj)
    first_path = bold(path_chain.pop(0))
    path_chain = " -> ".join(str(p) for p in [first_path] + path_chain)
    dt = datetime.datetime.utcnow()
    vulnerable = red("VULNERABLE")
    good = green("GOOD")
    unknown = yellow("UNKNOWN")
    if md5sum in MD5_BAD:
        comment = MD5_BAD[md5sum]
        print(f"[{dt}] {vulnerable}: {path_chain} [{md5sum}: {comment}]")
        stats["vulnerable"] += 1
    elif md5sum in MD5_GOOD:
        comment = MD5_GOOD[md5sum]
        print(f"[{dt}] {good}: {path_chain} [{md5sum}: {comment}]")
        stats["good"] += 1
    else:
        print(f"[{dt}] {unknown}: MD5 not known for {path_chain} [{md5sum}]")
        stats["unknown"] += 1


def main():
    parser = argparse.ArgumentParser(
        description="Find vulnerable log4j2 on filesystem (Log4Shell CVE-2021-4428)",
        epilog="Files are scanned recursively, both on disk and in Java Archive Files",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "path",
        metavar="PATH",
        nargs="*",
        default=["/"],
        help="Directory or file(s) to scan (recursively)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="verbose output (-v is info, -vv is debug)",
    )
    parser.add_argument(
        "-n", "--no-color", action="store_true", help="disable color output"
    )
    parser.add_argument("-b", "--no-banner", action="store_true", help="disable banner")
    args = parser.parse_args()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.verbose == 1:
        log.setLevel(logging.INFO)
        log.info("info logging enabled")
    elif args.verbose >= 2:
        log.setLevel(logging.DEBUG)
        log.debug("debug logging enabled")

    if args.no_color:
        global NO_COLOR
        NO_COLOR = True

    stats = {
        "scanned": 0,
        "files": 0,
        "directories": 0,
        "vulnerable": 0,
        "good": 0,
        "unknown": 0,
    }
    start_time = time.monotonic()

    if not args.no_banner:
        print(FIGLET)
    for directory in args.path:
        print(f"[{datetime.datetime.utcnow()}] Scanning: {directory}")
        for p in iter_scandir(directory, stats=stats):
            if p.name.lower() in FILENAMES:
                stats["scanned"] += 1
                log.info(f"Found file: {p}")
                with p.open("rb") as fobj:
                    check_vulnerable(fobj, [p], stats)
            if p.suffix.lower() in JAR_EXTENSIONS:
                try:
                    log.info(f"Found jar file: {p}")
                    stats["scanned"] += 1
                    for (zinfo, zfile, zpath, parents) in iter_jarfile(
                        p.resolve().open("rb"), parents=[p.resolve()]
                    ):
                        log.info(f"Found zfile: {zinfo} ({parents}")
                        with zfile.open(zinfo.filename) as zf:
                            check_vulnerable(zf, parents + [zpath], stats)
                except IOError as e:
                    log.debug(f"{p}: {e}", e)

    elapsed_time = time.monotonic() - start_time
    print(
        f"[{datetime.datetime.utcnow()}] Finished scan, elapsed time: {elapsed_time:.2f} seconds"
    )

    print("\nSummary:")
    print(f" Processed {stats['files']} files and {stats['directories']} directories")
    print(f" Scanned {stats['scanned']} files")
    if stats["vulnerable"]:
        print("  Found {} vulnerable files".format(stats["vulnerable"]))
    if stats["good"]:
        print("  Found {} good files".format(stats["good"]))
    if stats["unknown"]:
        print("  Found {} unknown files".format(stats["unknown"]))
    print(f"\nElapsed time: {elapsed_time:.2f} seconds ")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted!")

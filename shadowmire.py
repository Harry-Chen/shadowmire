#!/usr/bin/env python
import sys
from typing import Any, Optional
import xmlrpc.client
from dataclasses import dataclass
import re
import json
from urllib.parse import urljoin, urlparse, urlunparse
from pathlib import Path
from html.parser import HTMLParser
import logging
import html
import os
from contextlib import contextmanager
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
import signal
import tomllib
from copy import deepcopy
import requests
import click
from tqdm import tqdm
from requests.adapters import HTTPAdapter, Retry

logger = logging.getLogger(__name__)


USER_AGENT = "Shadowmire (https://github.com/taoky/shadowmire)"

# Note that it's suggested to use only 3 workers for PyPI.
WORKERS = int(os.environ.get("SHADOWMIRE_WORKERS", "3"))

# https://github.com/pypa/bandersnatch/blob/a05af547f8d1958217ef0dc0028890b1839e6116/src/bandersnatch_filter_plugins/prerelease_name.py#L18C1-L23C6
PRERELEASE_PATTERNS = (
    re.compile(r".+rc\d+$"),
    re.compile(r".+a(lpha)?\d+$"),
    re.compile(r".+b(eta)?\d+$"),
    re.compile(r".+dev\d+$"),
)


class PackageNotFoundError(Exception):
    pass


class ExitProgramException(Exception):
    pass


def exit_handler(signum, frame):
    raise ExitProgramException


signal.signal(signal.SIGTERM, exit_handler)


class LocalVersionKV:
    """
    A key-value database wrapper over sqlite3.

    As it would have consistency issue if it's writing while downstream is downloading the database.
    An extra "jsonpath" is used, to store kv results when necessary.
    """

    def __init__(self, dbpath: Path, jsonpath: Path) -> None:
        self.conn = sqlite3.connect(dbpath)
        self.jsonpath = jsonpath
        cur = self.conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS local(key TEXT PRIMARY KEY, value INT NOT NULL)"
        )
        self.conn.commit()

    def get(self, key: str) -> Optional[int]:
        cur = self.conn.cursor()
        res = cur.execute("SELECT value FROM local WHERE key = ?", (key,))
        row = res.fetchone()
        return row[0] if row else None

    INSERT_SQL = "INSERT INTO local (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"

    def set(self, key: str, value: int) -> None:
        cur = self.conn.cursor()
        cur.execute(self.INSERT_SQL, (key, value))
        self.conn.commit()

    def batch_set(self, d: dict[str, int]) -> None:
        cur = self.conn.cursor()
        kvs = [(k, v) for k, v in d.items()]
        cur.executemany(self.INSERT_SQL, kvs)
        self.conn.commit()

    def remove(self, key: str) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM local WHERE key = ?", (key,))
        self.conn.commit()

    def nuke(self, commit: bool = True) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM local")
        if commit:
            self.conn.commit()

    def keys(self, skip_invalid: bool = True) -> list[str]:
        cur = self.conn.cursor()
        if skip_invalid:
            res = cur.execute("SELECT key FROM local WHERE value != -1")
        else:
            res = cur.execute("SELECT key FROM local")
        rows = res.fetchall()
        return [row[0] for row in rows]

    def dump(self, skip_invalid: bool = True) -> dict[str, int]:
        cur = self.conn.cursor()
        if skip_invalid:
            res = cur.execute("SELECT key, value FROM local WHERE value != -1")
        else:
            res = cur.execute("SELECT key, value FROM local")
        rows = res.fetchall()
        return {row[0]: row[1] for row in rows}

    def dump_json(self, skip_invalid: bool = True) -> None:
        res = self.dump(skip_invalid)
        with overwrite(self.jsonpath) as f:
            json.dump(res, f, indent=2)


@contextmanager
def overwrite(file_path: Path, mode: str = "w", tmp_suffix: str = ".tmp"):
    tmp_path = file_path.parent / (file_path.name + tmp_suffix)
    try:
        with open(tmp_path, mode) as tmp_file:
            yield tmp_file
        tmp_path.rename(file_path)
    except Exception:
        # well, just keep the tmp_path in error case.
        raise


def normalize(name: str) -> str:
    """
    See https://peps.python.org/pep-0503/#normalized-names
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def remove_dir_with_files(directory: Path) -> None:
    """
    Remove dir in a safer (non-recursive) way, which means that the directory should have no child directories.
    """
    if not directory.exists():
        return
    assert directory.is_dir()
    for item in directory.iterdir():
        item.unlink()
    directory.rmdir()
    logger.info("Removed dir %s", directory)


def get_packages_from_index_html(html_path: Path) -> list[str]:
    """
    Get all <a> href (fragments removed) from given simple/<package>/index.html contents
    """

    class ATagHTMLParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.hrefs: list[Optional[str]] = []

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            if tag == "a":
                for attr in attrs:
                    if attr[0] == "href":
                        self.hrefs.append(attr[1])

    p = ATagHTMLParser()
    with open(html_path) as f:
        p.feed(f.read())

    ret = []
    for href in p.hrefs:
        if href:
            parsed_url = urlparse(href)
            clean_url = urlunparse(parsed_url._replace(fragment=""))
            ret.append(clean_url)
    return ret


def get_packages_from_index_json(json_path: Path) -> list[str]:
    """
    Get all urls from given simple/<package>/index.v1_json contents
    """
    with open(json_path) as f:
        contents_dict = json.load(f)
    urls = [i["url"] for i in contents_dict["files"]]
    return urls


def get_existing_hrefs(package_simple_path: Path) -> Optional[list[str]]:
    """
    There exists packages that have no release files, so when it encounters errors it would return None,
    otherwise empty list or list with hrefs.

    Priority: index.v1_json -> index.html
    """
    if not package_simple_path.exists():
        return None
    json_file = package_simple_path / "index.v1_json"
    html_file = package_simple_path / "index.html"
    if json_file.exists():
        return get_packages_from_index_json(json_file)
    if html_file.exists():
        return get_packages_from_index_html(html_file)
    return None


class CustomXMLRPCTransport(xmlrpc.client.Transport):
    """
    Set user-agent for xmlrpc.client
    """

    user_agent = USER_AGENT


def create_requests_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.1)
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update({"User-Agent": USER_AGENT})
    return s


class PyPI:
    """
    Upstream which implements full PyPI APIs
    """

    host = "https://pypi.org"
    # Let's assume that only sha256 exists...
    digest_name = "sha256"

    def __init__(self) -> None:
        self.xmlrpc_client = xmlrpc.client.ServerProxy(
            urljoin(self.host, "pypi"), transport=CustomXMLRPCTransport()
        )
        self.session = create_requests_session()

    def list_packages_with_serial(self) -> dict[str, int]:
        logger.info("Calling list_packages_with_serial() RPC, this requires some time...")
        return self.xmlrpc_client.list_packages_with_serial()  # type: ignore

    def get_package_metadata(self, package_name: str) -> dict:
        req = self.session.get(urljoin(self.host, f"pypi/{package_name}/json"))
        if req.status_code == 404:
            raise PackageNotFoundError
        return req.json()

    @staticmethod
    def get_release_files_from_meta(package_meta: dict) -> list[dict]:
        release_files = []
        for release in package_meta["releases"].values():
            release_files.extend(release)
        release_files.sort(key=lambda x: x["filename"])
        return release_files

    @staticmethod
    def file_url_to_local_url(url: str) -> str:
        parsed = urlparse(url)
        assert parsed.path.startswith("/packages")
        prefix = "../.."
        return prefix + parsed.path

    # Func modified from bandersnatch
    @classmethod
    def generate_html_simple_page(cls, package_meta: dict) -> str:
        package_rawname = package_meta["info"]["name"]
        simple_page_content = (
            "<!DOCTYPE html>\n"
            "<html>\n"
            "  <head>\n"
            '    <meta name="pypi:repository-version" content="{0}">\n'
            "    <title>Links for {1}</title>\n"
            "  </head>\n"
            "  <body>\n"
            "    <h1>Links for {1}</h1>\n"
        ).format("1.0", package_rawname)

        release_files = cls.get_release_files_from_meta(package_meta)

        def gen_html_file_tags(release: dict) -> str:
            file_tags = ""

            # data-requires-python: requires_python
            if "requires_python" in release and release["requires_python"] is not None:
                file_tags += (
                    f' data-requires-python="{html.escape(release["requires_python"])}"'
                )

            # data-yanked: yanked_reason
            if "yanked" in release and release["yanked"]:
                if "yanked_reason" in release and release["yanked_reason"]:
                    file_tags += (
                        f' data-yanked="{html.escape(release["yanked_reason"])}"'
                    )
                else:
                    file_tags += ' data-yanked=""'

            return file_tags

        simple_page_content += "\n".join(
            [
                '    <a href="{}#{}={}"{}>{}</a><br/>'.format(
                    cls.file_url_to_local_url(r["url"]),
                    cls.digest_name,
                    r["digests"][cls.digest_name],
                    gen_html_file_tags(r),
                    r["filename"],
                )
                for r in release_files
            ]
        )

        simple_page_content += (
            f"\n  </body>\n</html>\n<!--SERIAL {package_meta['last_serial']}-->"
        )

        return simple_page_content

    # Func modified from bandersnatch
    @classmethod
    def generate_json_simple_page(cls, package_meta: dict) -> str:
        package_json: dict[str, Any] = {
            "files": [],
            "meta": {
                "api-version": "1.1",
                "_last-serial": str(package_meta["last_serial"]),
            },
            "name": package_meta["info"]["name"],
            # (bandsnatch) TODO: Just sorting by default sort - Maybe specify order in future PEP
            "versions": sorted(package_meta["releases"].keys()),
        }

        release_files = cls.get_release_files_from_meta(package_meta)

        # Add release files into the JSON dict
        for r in release_files:
            package_json["files"].append(
                {
                    "filename": r["filename"],
                    "hashes": {
                        cls.digest_name: r["digests"][cls.digest_name],
                    },
                    "requires-python": r.get("requires_python", ""),
                    "size": r["size"],
                    "upload-time": r.get("upload_time_iso_8601", ""),
                    "url": cls.file_url_to_local_url(r["url"]),
                    "yanked": r.get("yanked", False),
                }
            )

        return json.dumps(package_json)


# (normalized_name as key, value)
ShadowmirePackageItem = tuple[str, int]


@dataclass
class Plan:
    remove: list[str]
    update: list[str]


def match_patterns(
    s: str, ps: list[re.Pattern[str]] | tuple[re.Pattern[str], ...]
) -> bool:
    for p in ps:
        if p.match(s):
            return True
    return False


class SyncBase:
    def __init__(
        self, basedir: Path, local_db: LocalVersionKV, sync_packages: bool = False
    ) -> None:
        self.basedir = basedir
        self.local_db = local_db
        self.simple_dir = basedir / "simple"
        self.packages_dir = basedir / "packages"
        self.jsonmeta_dir = basedir / "json"
        # create the dirs, if not exist
        self.simple_dir.mkdir(parents=True, exist_ok=True)
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        self.jsonmeta_dir.mkdir(parents=True, exist_ok=True)
        self.sync_packages = sync_packages

    def filter_remote_with_excludes(
        self, remote: dict[str, int], excludes: list[re.Pattern[str]]
    ) -> dict[str, int]:
        if not excludes:
            return remote
        res = {}
        for k, v in remote.items():
            matched = match_patterns(k, excludes)
            if not matched:
                res[k] = v
        return res

    def determine_sync_plan(
        self, local: dict[str, int], excludes: list[re.Pattern[str]]
    ) -> Plan:
        """
        local should NOT skip invalid (-1) serials
        """
        remote = self.fetch_remote_versions()
        remote = self.filter_remote_with_excludes(remote, excludes)
        # store remote to remote.json
        with overwrite(self.basedir / "remote.json") as f:
            json.dump(remote, f)
        to_remove = []
        to_update = []
        local_keys = set(local.keys())
        remote_keys = set(remote.keys())
        for i in local_keys - remote_keys:
            to_remove.append(i)
            local_keys.remove(i)
        for i in remote_keys - local_keys:
            to_update.append(i)
        for i in local_keys:
            local_serial = local[i]
            remote_serial = remote[i]
            if local_serial != remote_serial:
                if local_serial == -1:
                    logger.info("skip %s, as it's marked as not exist at upstream", i)
                    to_remove.append(i)
                else:
                    to_update.append(i)
        output = Plan(remove=to_remove, update=to_update)
        return output

    def fetch_remote_versions(self) -> dict[str, int]:
        raise NotImplementedError

    def check_and_update(
        self, package_names: list[str], prerelease_excludes: list[re.Pattern[str]]
    ) -> None:
        to_update = []
        for package_name in tqdm(package_names, desc="Checking consistency"):
            package_jsonmeta_path = self.jsonmeta_dir / package_name
            if not package_jsonmeta_path.exists():
                logger.info("add %s as it does not have json API file", package_name)
                to_update.append(package_name)
                continue
            package_simple_path = self.simple_dir / package_name
            html_simple = package_simple_path / "index.html"
            json_simple = package_simple_path / "index.v1_json"
            if not (html_simple.exists() and json_simple.exists()):
                logger.info(
                    "add %s as it does not have index.html or index.v1_json",
                    package_name,
                )
                to_update.append(package_name)
                continue
            hrefs1 = get_packages_from_index_html(html_simple)
            hrefs2 = get_packages_from_index_json(json_simple)
            if hrefs1 is None or hrefs2 is None or hrefs1 != hrefs2:
                # something unexpected happens...
                logger.info("add %s as its indexes are not consistent", package_name)
                to_update.append(package_name)
                continue
            hrefs = hrefs1
            # OK, check if all hrefs have corresponding files
            if self.sync_packages:
                should_update = False
                for href in hrefs:
                    dest = (package_simple_path / href).resolve()
                    if not dest.exists():
                        should_update = True
                        break
                if should_update:
                    logger.info("add %s as it's missing packages", package_name)
                    to_update.append(package_name)
        self.parallel_update(to_update, prerelease_excludes)

    def parallel_update(
        self, package_names: list, prerelease_excludes: list[re.Pattern[str]]
    ) -> None:
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {
                executor.submit(
                    self.do_update, package_name, prerelease_excludes, False
                ): (
                    idx,
                    package_name,
                )
                for idx, package_name in enumerate(package_names)
            }
            try:
                for future in tqdm(
                    as_completed(futures), total=len(package_names), desc="Updating"
                ):
                    idx, package_name = futures[future]
                    try:
                        serial = future.result()
                        if serial:
                            self.local_db.set(package_name, serial)
                    except Exception as e:
                        if e is ExitProgramException or e is KeyboardInterrupt:
                            raise
                        logger.warning(
                            "%s generated an exception", package_name, exc_info=True
                        )
                    if idx % 100 == 0:
                        self.local_db.dump_json()
            except (ExitProgramException, KeyboardInterrupt):
                logger.info("Get ExitProgramException or KeyboardInterrupt, exiting...")
                for future in futures:
                    future.cancel()
                sys.exit(1)

    def do_sync_plan(
        self, plan: Plan, prerelease_excludes: list[re.Pattern[str]]
    ) -> None:
        to_remove = plan.remove
        to_update = plan.update

        for package_name in to_remove:
            self.do_remove(package_name)

        self.parallel_update(to_update, prerelease_excludes)

    def do_remove(self, package_name: str, use_db: bool = True) -> None:
        metajson_path = self.jsonmeta_dir / package_name
        if metajson_path.exists():
            # To make this less noisy...
            logger.info("removing %s", package_name)
        package_simple_dir = self.simple_dir / package_name
        packages_to_remove = get_existing_hrefs(package_simple_dir)
        if packages_to_remove:
            paths_to_remove = [package_simple_dir / p for p in packages_to_remove]
            for p in paths_to_remove:
                if p.exists():
                    p.unlink()
                    logger.info("Removed file %s", p)
        remove_dir_with_files(package_simple_dir)
        metajson_path = self.jsonmeta_dir / package_name
        metajson_path.unlink(missing_ok=True)
        if use_db:
            old_serial = self.local_db.get(package_name)
            if old_serial != -1:
                self.local_db.remove(package_name)

    def do_update(
        self,
        package_name: str,
        prerelease_excludes: list[re.Pattern[str]],
        use_db: bool = True,
    ) -> Optional[int]:
        raise NotImplementedError

    def write_meta_to_simple(self, package_simple_path: Path, meta: dict) -> None:
        simple_html_contents = PyPI.generate_html_simple_page(meta)
        simple_json_contents = PyPI.generate_json_simple_page(meta)
        for html_filename in ("index.html", "index.v1_html"):
            html_path = package_simple_path / html_filename
            with overwrite(html_path) as f:
                f.write(simple_html_contents)
        for json_filename in ("index.v1_json",):
            json_path = package_simple_path / json_filename
            with overwrite(json_path) as f:
                f.write(simple_json_contents)

    def finalize(self) -> None:
        local_names = self.local_db.keys()
        # generate index.html at basedir
        index_path = self.basedir / "simple" / "index.html"
        # modified from bandersnatch
        with overwrite(index_path) as f:
            f.write("<!DOCTYPE html>\n")
            f.write("<html>\n")
            f.write("  <head>\n")
            f.write('    <meta name="pypi:repository-version" content="1.0">\n')
            f.write("    <title>Simple Index</title>\n")
            f.write("  </head>\n")
            f.write("  <body>\n")
            # This will either be the simple dir, or if we are using index
            # directory hashing, a list of subdirs to process.
            for pkg in local_names:
                # We're really trusty that this is all encoded in UTF-8. :/
                f.write(f'    <a href="{pkg}/">{pkg}</a><br/>\n')
            f.write("  </body>\n</html>")
        self.local_db.dump_json()


def download(
    session: requests.Session, url: str, dest: Path
) -> tuple[bool, Optional[requests.Response]]:
    try:
        resp = session.get(url, allow_redirects=True)
    except requests.RequestException:
        logger.warning("download %s failed with exception", exc_info=True)
        return False, None
    if resp.status_code >= 400:
        logger.warning(
            "download %s failed with status %s, skipping this package",
            url,
            resp.status_code,
        )
        return False, resp
    with overwrite(dest, "wb") as f:
        f.write(resp.content)
    return True, resp


def filter_release_from_meta(
    meta: dict, patterns: list[re.Pattern[str]] | tuple[re.Pattern[str], ...]
) -> None:
    for release in list(meta["releases"].keys()):
        if match_patterns(release, patterns):
            del meta["releases"][release]


class SyncPyPI(SyncBase):
    def __init__(
        self, basedir: Path, local_db: LocalVersionKV, sync_packages: bool = False
    ) -> None:
        self.pypi = PyPI()
        self.session = create_requests_session()
        super().__init__(basedir, local_db, sync_packages)

    def fetch_remote_versions(self) -> dict[str, int]:
        remote_serials = self.pypi.list_packages_with_serial()
        ret = {}
        for key in remote_serials:
            ret[normalize(key)] = remote_serials[key]
        return ret

    def do_update(
        self,
        package_name: str,
        prerelease_excludes: list[re.Pattern[str]],
        use_db: bool = True,
    ) -> Optional[int]:
        logger.info("updating %s", package_name)
        package_simple_path = self.simple_dir / package_name
        package_simple_path.mkdir(exist_ok=True)
        try:
            meta = self.pypi.get_package_metadata(package_name)
            meta_original = deepcopy(meta)
            logger.debug("%s meta: %s", package_name, meta)
        except PackageNotFoundError:
            logger.warning(
                "%s missing from upstream, remove and ignore in the future.",
                package_name,
            )
            # try remove it locally, if it does not exist upstream
            self.do_remove(package_name, use_db=False)
            if not use_db:
                return -1
            self.local_db.set(package_name, -1)
            return None

        # filter prerelease, if necessary
        if match_patterns(package_name, prerelease_excludes):
            filter_release_from_meta(meta, PRERELEASE_PATTERNS)

        if self.sync_packages:
            # sync packages first, then sync index
            existing_hrefs = get_existing_hrefs(package_simple_path)
            existing_hrefs = [] if existing_hrefs is None else existing_hrefs
            release_files = PyPI.get_release_files_from_meta(meta)
            # remove packages that no longer exist remotely
            remote_hrefs = [
                self.pypi.file_url_to_local_url(i["url"]) for i in release_files
            ]
            should_remove = list(set(existing_hrefs) - set(remote_hrefs))
            for p in should_remove:
                logger.info("removing file %s (if exists)", p)
                package_path = (package_simple_path / p).resolve()
                package_path.unlink(missing_ok=True)
            for i in release_files:
                url = i["url"]
                dest = (
                    package_simple_path / self.pypi.file_url_to_local_url(i["url"])
                ).resolve()
                logger.info("downloading file %s -> %s", url, dest)
                if dest.exists():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                success, resp = download(self.session, url, dest)
                if not success:
                    logger.warning("skipping %s as it fails downloading", package_name)
                    return None

        last_serial: int = meta["last_serial"]

        self.write_meta_to_simple(package_simple_path, meta)
        json_meta_path = self.jsonmeta_dir / package_name
        with overwrite(json_meta_path) as f:
            # Note that we're writing meta_original here!
            json.dump(meta_original, f)

        if use_db:
            self.local_db.set(package_name, last_serial)

        return last_serial


class SyncPlainHTTP(SyncBase):
    def __init__(
        self,
        upstream: str,
        basedir: Path,
        local_db: LocalVersionKV,
        sync_packages: bool = False,
    ) -> None:
        self.upstream = upstream
        self.session = create_requests_session()
        super().__init__(basedir, local_db, sync_packages)

    def fetch_remote_versions(self) -> dict[str, int]:
        remote_url = urljoin(self.upstream, "local.json")
        resp = self.session.get(remote_url)
        resp.raise_for_status()
        remote: dict[str, int] = resp.json()
        return remote

    def do_update(
        self,
        package_name: str,
        prerelease_excludes: list[re.Pattern[str]],
        use_db: bool = True,
    ) -> Optional[int]:
        logger.info("updating %s", package_name)
        package_simple_path = self.simple_dir / package_name
        package_simple_path.mkdir(exist_ok=True)
        if self.sync_packages:
            hrefs = get_existing_hrefs(package_simple_path)
            existing_hrefs = [] if hrefs is None else hrefs
        # Download JSON meta
        file_url = urljoin(self.upstream, f"/json/{package_name}")
        success, resp = download(
            self.session, file_url, self.jsonmeta_dir / (package_name + ".new")
        )
        if not success:
            logger.error(
                "download %s JSON meta fails with code %s",
                package_name,
                resp.status_code if resp else None,
            )
            return None
        assert resp
        meta = resp.json()
        # filter prerelease, if necessary
        if match_patterns(package_name, prerelease_excludes):
            filter_release_from_meta(meta, PRERELEASE_PATTERNS)

        if self.sync_packages:
            release_files = PyPI.get_release_files_from_meta(meta)
            remote_hrefs = [PyPI.file_url_to_local_url(i["url"]) for i in release_files]
            should_remove = list(set(existing_hrefs) - set(remote_hrefs))
            for p in should_remove:
                logger.info("removing file %s (if exists)", p)
                package_path = (package_simple_path / p).resolve()
                package_path.unlink(missing_ok=True)
            package_simple_url = urljoin(self.upstream, f"/simple/{package_name}/")
            for href in remote_hrefs:
                url = urljoin(package_simple_url, href)
                dest = (package_simple_path / href).resolve()
                logger.info("downloading file %s -> %s", url, dest)
                if dest.exists():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                success, resp = download(self.session, url, dest)
                if not success:
                    logger.warning("skipping %s as it fails downloading", package_name)
                    return None

        # OK, now it's safe to rename
        (self.jsonmeta_dir / (package_name + ".new")).rename(
            self.jsonmeta_dir / package_name
        )
        # generate indexes
        self.write_meta_to_simple(package_simple_path, meta)

        last_serial = get_local_serial(package_simple_path)
        if not last_serial:
            logger.warning("cannot get valid package serial from %s", package_name)
        else:
            if use_db:
                self.local_db.set(package_name, last_serial)

        return last_serial


def get_local_serial(package_meta_path: Path) -> Optional[int]:
    """
    Accepts /json/<package_name> as package_meta_path
    """
    package_name = package_meta_path.name
    try:
        with open(package_meta_path) as f:
            contents = f.read()
    except FileNotFoundError:
        logger.warning("%s does not have index.html, skipping", package_name)
        return None
    try:
        meta = json.loads(contents)
        return meta["last_serial"]
    except Exception:
        logger.warning("cannot parse %s's JSON metadata", package_name, exc_info=True)
        return None


def sync_shared_args(func):
    shared_options = [
        click.option(
            "--sync-packages/--no-sync-packages",
            default=False,
            help="Sync packages instead of just indexes, by default it's --no-sync-packages",
        ),
        click.option(
            "--shadowmire-upstream",
            required=False,
            type=str,
            help="Use another upstream using shadowmire instead of PyPI",
        ),
        click.option(
            "--exclude", multiple=True, help="Remote package names to exclude. Regex."
        ),
        click.option(
            "--prerelease-exclude",
            multiple=True,
            help="Package names that shall exclude prerelease. Regex.",
        ),
    ]
    for option in shared_options[::-1]:
        func = option(func)
    return func


def read_config(
    ctx: click.Context, param: click.Option, filename: Optional[str]
) -> None:
    if filename is None:
        return
    with open(filename, "rb") as f:
        data = tomllib.load(f)
    try:
        options = dict(data["options"])
    except KeyError:
        options = {}
    ctx.default_map = {
        "sync": options,
        "verify": options,
        "do-update": options,
        "do-remove": options,
    }


@click.group()
@click.option(
    "--config",
    type=click.Path(dir_okay=False),
    help="Read option defaults from specified TOML file",
    callback=read_config,
    expose_value=False,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    log_level = logging.DEBUG if os.environ.get("DEBUG") else logging.INFO
    logging.basicConfig(level=log_level)
    ctx.ensure_object(dict)

    if WORKERS > 10:
        logger.warning(
            "You have set a worker value larger than 10, which is forbidden by PyPI maintainers."
        )
        logger.warning("Don't blame me if you were banned!")

    basedir = Path(os.environ.get("REPO", "."))
    local_db = LocalVersionKV(basedir / "local.db", basedir / "local.json")

    ctx.obj["basedir"] = basedir
    ctx.obj["local_db"] = local_db


def exclude_to_excludes(exclude: tuple[str]) -> list[re.Pattern[str]]:
    return [re.compile(i) for i in exclude]


def get_syncer(
    basedir: Path,
    local_db: LocalVersionKV,
    sync_packages: bool,
    shadowmire_upstream: Optional[str],
) -> SyncBase:
    syncer: SyncBase
    if shadowmire_upstream:
        syncer = SyncPlainHTTP(
            upstream=shadowmire_upstream,
            basedir=basedir,
            local_db=local_db,
            sync_packages=sync_packages,
        )
    else:
        syncer = SyncPyPI(
            basedir=basedir, local_db=local_db, sync_packages=sync_packages
        )
    return syncer


@cli.command(help="Sync from upstream")
@click.pass_context
@sync_shared_args
def sync(
    ctx: click.Context,
    sync_packages: bool,
    shadowmire_upstream: Optional[str],
    exclude: tuple[str],
    prerelease_exclude: tuple[str],
) -> None:
    basedir: Path = ctx.obj["basedir"]
    local_db: LocalVersionKV = ctx.obj["local_db"]
    excludes = exclude_to_excludes(exclude)
    prerelease_excludes = exclude_to_excludes(prerelease_exclude)
    syncer = get_syncer(basedir, local_db, sync_packages, shadowmire_upstream)
    local = local_db.dump(skip_invalid=False)
    plan = syncer.determine_sync_plan(local, excludes)
    # save plan for debugging
    with overwrite(basedir / "plan.json") as f:
        json.dump(plan, f, default=vars, indent=2)
    syncer.do_sync_plan(plan, prerelease_excludes)
    syncer.finalize()


@cli.command(help="(Re)generate local db and json from json/")
@click.pass_context
def genlocal(ctx: click.Context) -> None:
    basedir: Path = ctx.obj["basedir"]
    local_db: LocalVersionKV = ctx.obj["local_db"]
    local = {}
    for package_metapath in tqdm((basedir / "json").iterdir(), desc="Iterating json/"):
        if not package_metapath.is_file():
            continue
        package_name = package_metapath.name
        serial = get_local_serial(package_metapath)
        if serial:
            local[package_name] = serial
    local_db.nuke(commit=False)
    local_db.batch_set(local)
    local_db.dump_json()


@cli.command(
    help="Verify existing sync from local db, download missing things, remove unreferenced packages"
)
@click.pass_context
@sync_shared_args
def verify(
    ctx: click.Context,
    sync_packages: bool,
    shadowmire_upstream: Optional[str],
    exclude: tuple[str],
    prerelease_exclude: tuple[str],
) -> None:
    basedir: Path = ctx.obj["basedir"]
    local_db: LocalVersionKV = ctx.obj["local_db"]
    excludes = exclude_to_excludes(exclude)
    prerelease_excludes = exclude_to_excludes(prerelease_exclude)
    syncer = get_syncer(basedir, local_db, sync_packages, shadowmire_upstream)

    logger.info("remove packages NOT in local db")
    local_names = set(local_db.keys())
    simple_dirs = set([i.name for i in (basedir / "simple").iterdir() if i.is_dir()])
    for package_name in simple_dirs - local_names:
        syncer.do_remove(package_name)

    logger.info("remove packages NOT in remote")
    local = local_db.dump(skip_invalid=False)
    plan = syncer.determine_sync_plan(local, excludes)
    for package_name in plan.remove:
        # We only take the plan.remove part here
        syncer.do_remove(package_name)

    logger.info(
        "make sure all local indexes are valid, and (if --sync-packages) have valid local package files"
    )
    syncer.check_and_update(list(local_names), prerelease_excludes)
    syncer.finalize()

    logger.info("delete unreferenced files in `packages` folder")
    ref_set = set()
    for sname in simple_dirs:
        sd = basedir / "simple" / sname
        hrefs = get_existing_hrefs(sd)
        hrefs = [] if hrefs is None else hrefs
        for i in hrefs:
            ref_set.add(str((sd / i).resolve()))
    for file in (basedir / "packages").glob("*/*/*/*"):
        file = file.resolve()
        if str(file) not in ref_set:
            logger.info("removing unreferenced %s", file)
            file.unlink()


@cli.command(help="Manual update given package for debugging purpose")
@click.pass_context
@sync_shared_args
@click.argument("package_name")
def do_update(
    ctx: click.Context,
    sync_packages: bool,
    shadowmire_upstream: Optional[str],
    exclude: tuple[str],
    prerelease_exclude: tuple[str],
    package_name: str,
) -> None:
    basedir: Path = ctx.obj["basedir"]
    local_db: LocalVersionKV = ctx.obj["local_db"]
    excludes = exclude_to_excludes(exclude)
    if excludes:
        logger.warning("--exclude is ignored in do_update()")
    prerelease_excludes = exclude_to_excludes(prerelease_exclude)
    syncer = get_syncer(basedir, local_db, sync_packages, shadowmire_upstream)
    syncer.do_update(package_name, prerelease_excludes)


@cli.command(help="Manual remove given package for debugging purpose")
@click.pass_context
@sync_shared_args
@click.argument("package_name")
def do_remove(
    ctx: click.Context,
    sync_packages: bool,
    shadowmire_upstream: Optional[str],
    exclude: tuple[str],
    prerelease_exclude: tuple[str],
    package_name: str,
) -> None:
    basedir = ctx.obj["basedir"]
    local_db = ctx.obj["local_db"]
    if exclude or prerelease_exclude:
        logger.warning("exclusion rules are ignored in do_remove()")
    syncer = get_syncer(basedir, local_db, sync_packages, shadowmire_upstream)
    syncer.do_remove(package_name)


if __name__ == "__main__":
    cli(obj={})

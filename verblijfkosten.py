#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pypdf>=6.21.1", "python-constraint2>=2.7.3"]
# ///

"""
This script:

1. Downloads the verblijf kosten PDF from the leiden website (only when the server copy is changed).
2. Parses the downloaded PDF into a .toml file
3. Ask for the country and then ask to select from the city names
4. Uses python-constraint2 solver to figure out how many meals and hours you should declare
"""

import argparse
import json
import locale
import os
import random
import shutil
import sys
import io
import re
import time
import tomllib
from collections import defaultdict
from dataclasses import dataclass

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime, timezone, timedelta, date
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import TypedDict, NamedTuple
from urllib.error import HTTPError, URLError
from urllib.request import Request

from constraint import Problem, ExactSumConstraint, MaxSumConstraint, MinSumConstraint
from pypdf import PdfReader

__author__ = "Quinten Cabo <q.m.j.cabo@liacs.leidenuniv.nl>"

USER_AGENT = "tarieflijst-sync/1.0"
#USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
TIMEOUT_SECONDS = 300
NOT_MODIFIED = 304
STALE_WARNING_AGE = timedelta(days=200)

MAX_SOLVE_ATTEMPTS = 10_000 # Probeert binnen 10 euro te komen van het maximumbedrag
DEFAULT_STEPSIZE = Decimal(0.10) # Hoeveel elke solve attempt het gat tussen minimum en maximum vergoeding naar boven groeit.

MODULE_DIR = Path(__file__).parent
DEFAULT_TOML_DESTINATION = MODULE_DIR / Path("verblijfkosten-tarieven.toml")
DEFAULT_PDF_DESTINATION = MODULE_DIR / Path("tarieflijst-verblijfkosten-buitenlandse-dienstreizen.pdf")
DEFAULT_PDF_URL = "https://www.medewerkers.universiteitleiden.nl/binaries/content/assets/ul2staff/po/arbeidsvoorwaarden/tarieflijst-verblijfkosten-buitenlandse-dienstreizen.pdf"

RATE_LINE_PATTERN = re.compile(
        r"^(?P<gebied>.+?)\s+"
        r"€\s*(?P<logies>[\d.]+,\d{2})\s+"
        r"€\s*(?P<ontbijt>[\d.]+,\d{2})\s+"
        r"€\s*(?P<lunch>[\d.]+,\d{2})\s+"
        r"€\s*(?P<diner>[\d.]+,\d{2})\s+"
        r"€\s*(?P<urencomp>[\d.]+,\d{2})$"
    )

@dataclass(slots=True)
class VerblijfTomlMeta:
    bron: str = DEFAULT_PDF_URL
    toml_destination : Path = DEFAULT_TOML_DESTINATION
    pdf_location : Path = DEFAULT_PDF_DESTINATION
    geldig_vanaf: date | None = None
    valuta: str = "EUR"



class Color:
    """ Colors """
    waarschuw = '\033[33m'
    title = '\033[1m'
    danger = '\033[91m'
    reset = '\033[0m'
    amount = '\033[34m'
    bold_amount = '\033[1m\033[34m'
    success = '\033[92m'
    success_bold = '\033[1m\033[92m'

    @staticmethod
    def disable_if_unwanted():
        is_color_forced = bool(os.environ.get("FORCE_COLOR"))
        if is_color_forced:
            return

        try:
            is_stdout_terminal = sys.stdout.isatty()
        except AttributeError:  # sys.stdout is None, e.g. under pythonw
            is_stdout_terminal = False

        is_no_color_requested = bool(os.environ.get("NO_COLOR"))
        is_dumb_terminal = os.environ.get("TERM") == "dumb"
        is_output_redirected = not is_stdout_terminal
        should_disable = is_no_color_requested or is_dumb_terminal or is_output_redirected
        if not should_disable:
            return

        # Actually disable the colours
        for name, value in list(vars(Color).items()):
            is_escape_code = isinstance(value, str) and value.startswith("\033")
            if is_escape_code:
                setattr(Color, name, "")

""" 
Step 1: Download the pdf 
"""

def sidecar_path_for(destination: Path) -> Path:
    """The sidecar is a file that contains metadata about the downloaded PDF file.
       When it was downloaded and the Etag for caching. """
    return destination.with_name(destination.name + ".http.json")


class Validators(TypedDict):
    etag: str
    last_modified: date

def read_validators(destination: Path) -> dict[str, str]:
    """ETag and Last-Modified of the copy on disk, as far as they are known."""
    sidecar_path = sidecar_path_for(destination)
    try:
        stored = json.loads(sidecar_path.read_text())
    except FileNotFoundError:
        stored = {}
    except json.JSONDecodeError:
        print(f"{sidecar_path} is corrupt, ignoring it", file=sys.stderr)
        stored = {}

    has_stored_last_modified = "last_modified" in stored
    if has_stored_last_modified:
        return stored

    local_mtime = datetime.fromtimestamp(destination.stat().st_mtime, tz=timezone.utc)
    return stored | {"last_modified": format_datetime(local_mtime, usegmt=True)}


def write_validators(destination: Path, headers) -> None:
    """ Write the sidecar file with the validators for the PDF file. """
    sidecar_path = sidecar_path_for(destination)
    validators = {
        key: value
        for key, value in (
            ("etag", headers.get("ETag")),
            ("last_modified", headers.get("Last-Modified")),
        )
        if value is not None
    }
    if not validators:
        sidecar_path.unlink(missing_ok=True)
        return
    sidecar_path.write_text(json.dumps(validators, indent=2) + "\n")


def build_conditional_request(url: str, destination: Path) -> Request:
    request = Request(url, headers={"User-Agent": USER_AGENT})

    has_local_copy = destination.exists()
    if not has_local_copy:
        return request

    validators = read_validators(destination)
    if (etag := validators.get("etag")) is not None:
        request.add_header("If-None-Match", etag)
    if (last_modified := validators.get("last_modified")) is not None:
        request.add_header("If-Modified-Since", last_modified)
    return request


def apply_remote_mtime(destination: Path, headers) -> None:
    if (last_modified_header := headers.get("Last-Modified")) is None:
        return
    remote_timestamp = parsedate_to_datetime(last_modified_header).timestamp()
    os.utime(destination, (remote_timestamp, remote_timestamp))


from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, build_opener

OPENER = build_opener(HTTPCookieProcessor(CookieJar()))

def download_if_changed(url: str = DEFAULT_PDF_URL, destination: Path = DEFAULT_PDF_DESTINATION) -> bool:
    """Returns True when the file was (re)downloaded."""
    request = build_conditional_request(url, destination)

    try:
        response = OPENER.open(request, timeout=TIMEOUT_SECONDS)
    except HTTPError as error:
        if error.code == NOT_MODIFIED:
            return False
        raise

    partial_path = destination.with_name(destination.name + ".part")
    try:
        with response:
            assert response.status == 200, f"GET {url} returned {response.status}"
            headers = response.headers
            with partial_path.open("wb") as partial_file:
                shutil.copyfileobj(response, partial_file)

        downloaded_size = partial_path.stat().st_size
        assert downloaded_size, f"{url} returned an empty body"

        expected_size = headers.get("Content-Length")
        is_size_known = expected_size is not None
        if is_size_known and int(expected_size) != downloaded_size:
            raise ConnectionResetError(
                f"{url}: {downloaded_size} van {expected_size} bytes ontvangen"
            )

        partial_path.replace(destination)
    except BaseException:
        partial_path.unlink(missing_ok=True)
        raise

    apply_remote_mtime(destination, headers)
    write_validators(destination, headers)
    return True


def download_if_changed_with_retries(url: str = DEFAULT_PDF_URL, destination: Path = DEFAULT_PDF_DESTINATION) -> bool:
    """ Download the PDF file if it has changed. """
    retry_delays_seconds = 2, 8, 30, None

    for attempt, delay in enumerate(retry_delays_seconds, start=1):
        try:
            return download_if_changed(url, destination)
        except (ConnectionResetError, TimeoutError) as error:
            is_last_attempt = delay is None
            if is_last_attempt:
                raise RuntimeError(f"{url} gave a reset {attempt} times") from error
            print(f"pdf download (up to date check) attempt {attempt} got {error}, trying again in ~{delay}s", file=sys.stderr)
            if delay:
                time.sleep(random.uniform(0, delay / 2) + delay)
    raise AssertionError("PDF url is not reachable")


"""
Step 2: Turn the pdf into a toml file
"""


def to_decimal(dutch_amount: str) -> Decimal:
    return Decimal(dutch_amount.replace(".", "").replace(",", "."))

class Tarief(TypedDict):
    """ Het toegestane tarief voor verblijf in een land of regio. """
    land: str
    regio: str
    logies: Decimal
    ontbijt: Decimal
    lunch: Decimal
    diner: Decimal
    urencomp: Decimal


def split_gebied(gebied: str) -> tuple[str, str | None]:
    if " - " not in gebied:
        return gebied, None
    land, regio = gebied.split(" - ", 1)
    return land, regio

COMPONENT_PERCENTAGES = {
    "ontbijt": Decimal("0.12"),
    "lunch": Decimal("0.20"),
    "diner": Decimal("0.32"),
    "urencomp": Decimal("0.015"),
}

def check_internal_consistency(rows: list[Tarief]) -> list[Tarief]:
    """Ontbijt/lunch/diner/urencomp are fixed ratios of each other in this list;
    deviations signal a parse error rather than a real rate."""
    anomalies = []
    for row in rows:
        overige_kosten = row["lunch"] / COMPONENT_PERCENTAGES["lunch"]
        is_whole_euro_base = overige_kosten % 1 == 0
        if not is_whole_euro_base:
            anomalies.append(f"{row['land']} - {row['regio']}: grondslag {overige_kosten} is geen heel bedrag")
        for component, percentage in COMPONENT_PERCENTAGES.items():
            expected = (overige_kosten * percentage).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if expected != row[component]:
                anomalies.append(f"{row['land']} - {row['regio']}: {component} {row[component]} != {expected}")
    return anomalies


def read_verblijf_pdf(pdf_location: Path = DEFAULT_PDF_DESTINATION) -> io.StringIO:
    # Open your PDF file in read-binary mode
    with open(pdf_location, "rb") as pdf_file:
        reader = PdfReader(pdf_file)

        # Loop through every page and extract text
        output = io.StringIO()
        for page in reader.pages:
            text = page.extract_text()
            output.write(text)
    output.seek(0)
    return output

RELEASE_DATE_PATTERN = re.compile(r'Tarieflijst verblijfkosten buitenlandse dienstreizen per (?P<release>\d\d?.+?\d\d\d\d)')

def parse_verblijf_pdf_text_into_valid_from_date(text: io.TextIOBase) -> date:
    for raw_line in text:
        line = raw_line.strip()
        if (match := RELEASE_DATE_PATTERN.search(line)) is None:
            continue
        release = match.group("release")
        try:
            locale.setlocale(locale.LC_TIME, 'nl_NL.UTF-8')
        except locale.Error:
            locale.setlocale(locale.LC_TIME, 'nl_NL')  # Alternative fallback
        valid_from = datetime.strptime(release, "%d %B %Y").date()
        return valid_from
    else:
        raise ValueError("Geldig vanaf regex patroon niet gevonden in de text")

def parse_verblijf_pdf_text_into_tarieven(text: io.TextIOBase) -> list[Tarief]:
    tarieven: list[Tarief] = []
    for raw_line in text:
        line = raw_line.strip()
        if (match := RATE_LINE_PATTERN.match(line)) is None:
            continue
        gebied = re.sub(r"\s+", " ", match.group("gebied")).strip()
        land, regio = split_gebied(gebied)
        tarieven.append(
            {
                "land": land,
                "regio": regio or land,
                "logies": to_decimal(match.group("logies")),
                "ontbijt": to_decimal(match.group("ontbijt")),
                "lunch": to_decimal(match.group("lunch")),
                "diner": to_decimal(match.group("diner")),
                "urencomp": to_decimal(match.group("urencomp")),
            }
        )
    return tarieven

# n pages, name of pdf, and updated at in file
def render_tarieven_as_toml(tarieven: list[Tarief], render_meta : VerblijfTomlMeta) -> str:
    """ Render the tarieven as a TOML string. """
    toml = io.StringIO()
    today = date.today()
    toml.write(
        f'# Tarieflijst verblijfkosten buitenlandse dienstreizen.\n'
        f'# Bron: {render_meta.bron}\n'
        f'# Gedownload op {today.isoformat()}\n'
        '# logies/ontbijt/lunch/diner = bedrag per dag, urencomp = bedrag per uur.\n'
        "\n"
        f'valuta = "{render_meta.valuta}"\n'
        f"{f'geldig_vanaf = {render_meta.geldig_vanaf.isoformat()}' if render_meta.geldig_vanaf else ''}"
        "\n\n")

    for tarief in tarieven:
        toml.write("[[tarief]]\n")
        toml.write(f'land = "{tarief["land"]}"\n')
        if tarief["regio"] is not None:
            toml.write(f'regio = "{tarief["regio"]}"\n')
        toml.write(f'logies = {tarief["logies"]}\n'
                   f'ontbijt = {tarief["ontbijt"]}\n'
                   f'lunch = {tarief["lunch"]}\n'
                   f'diner = {tarief["diner"]}\n'
                   f'urencomp = {tarief["urencomp"]}\n\n')

    toml.seek(0)
    toml_text = toml.read()
    return toml_text


def create_verblijf_toml(meta: VerblijfTomlMeta) -> None:
    verblijf_text = read_verblijf_pdf(meta.pdf_location)

    tarieven = parse_verblijf_pdf_text_into_tarieven(verblijf_text)
    verblijf_text.seek(0)

    try:
        meta.geldig_vanaf = parse_verblijf_pdf_text_into_valid_from_date(verblijf_text)
    except ValueError as e:
        print(f"{Color.waarschuw}Let op{Color.reset}: kon niet de geldigheidsdatum van de tarievenlijst bepalen: {e}", file=sys.stderr)

    toml = render_tarieven_as_toml(tarieven, meta)
    with open(meta.toml_destination, "w", encoding="utf-8") as tarieven_toml_file:
        tarieven_toml_file.write(toml)

    print(f"{Color.success}Verblijfkosten opgeslagen in {meta.toml_destination.absolute()}{Color.reset}", file=sys.stderr)


""" 
Step 4: Ask for the country and then ask to select from the city names
"""
from difflib import get_close_matches
import unicodedata

NON_DECOMPOSING_REPLACEMENTS = str.maketrans(
    {
        "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D",
        "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th", "æ": "ae", "Æ": "Ae",
        "œ": "oe", "Œ": "Oe", "ı": "i", "'": "'",
        "–": "-", "—": "-", "‐": "-",
    }
)


def normalise_name(name: str) -> str:
    """Lowercase ASCII form for matching: Curaçao, CURACAO and curaçao all match."""
    without_specials = name.translate(NON_DECOMPOSING_REPLACEMENTS)
    decomposed = unicodedata.normalize("NFKD", without_specials)
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(without_marks.casefold().split())

def find_country(user_input: str, tarieven: list[Tarief]) -> list[Tarief]:
    """Alle rijen van een land. Gooit LookupError met suggesties als het land onbekend is."""
    country_index = defaultdict[str, list[Tarief]](list)
    for tarief in tarieven:
        normalised_land = normalise_name(tarief['land'])
        country_index[normalised_land].append(tarief)

    key = normalise_name(user_input)
    if (country_row := country_index.get(key)) is not None:
        return country_row
    suggestions = get_close_matches(key, country_index, n=3, cutoff=0.7)
    if suggestions:
        raise LookupError(f"'{user_input}' country not found. Did you mean: {suggestions}?")
    else:
        destinations = {land for land in country_index}
        raise LookupError(f"'{user_input}' country not found. The possible countries are: {', '.join(destinations)}.")


def choose_region(regions: list[Tarief], answer_region: str | None = None) -> Tarief:
    """Kiest de rij binnen een land. answer_region None betekent: vraag het interactief."""
    assert regions, "leeg land zonder regions, build_country_index maakt geen lege lijsten"

    has_single_option = len(regions) == 1
    if has_single_option:
        return regions[0]

    if answer_region is None:
        for number, region in enumerate(regions, start=1):
            region_string = f"{region['regio']}"
            print(f"  {number}. {region_string}")
        answer_region = input("Choose region (nummer or name): ")

    is_number = answer_region.strip().isdigit()
    if is_number:
        index = int(answer_region) - 1
        is_in_range = 0 <= index < len(regions)
        if not is_in_range:
            raise LookupError(f"Choice {answer_region} lies outside 1..{len(regions)}")
        return regions[index]

    wanted = normalise_name(answer_region)
    for tarief in regions:
        region = normalise_name(tarief['regio'])
        if normalise_name(region) == wanted:
            return tarief

    suggestions = get_close_matches(wanted, [normalise_name(tarief['regio']) for tarief in regions], n=3, cutoff=0.6)
    land = next(iter(regions)).get('land')
    raise LookupError(f"Region '{answer_region}' not found in {land}" + (f", did you mean: {', '.join(suggestions)}?" if suggestions else "."))

"""
Main:
"""

def is_pdf_stale_check_due(destination: Path) -> bool:
    """Controleer als de lokale lijst zijn geldigheidsperiode uit is, of als we
    nog nooit gecontroleerd hebben. Binnen een lopende periode wijzigt de lijst niet."""
    if not destination.exists():
        return True

    sidecar_path = sidecar_path_for(destination)
    if not sidecar_path.exists():
        return True

    validators = read_validators(destination)
    if (valid_from_text := validators.get("valid_from")) is None:
        return True
    valid_from = date.fromisoformat(valid_from_text)
    next_publication = next_indexation_date(valid_from)
    has_new_list_been_published = date.today() >= next_publication
    if not has_new_list_been_published:
        return False

    last_check = datetime.fromtimestamp(sidecar_path.stat().st_mtime, tz=timezone.utc).date()
    return last_check < next_publication


def next_indexation_date(valid_from: date) -> date:
    """De lijst wordt geïndexeerd per 1 januari en 1 juli. We doen 1 maand van tevoren al checken."""
    is_first_half = valid_from.month < 7
    if is_first_half:
        return date(valid_from.year, 6, 1)
    return date(valid_from.year, 12, 1)


def setup(is_forced: bool, pdf_destination: Path = DEFAULT_PDF_DESTINATION, toml_destination : Path = DEFAULT_TOML_DESTINATION, pdf_url: str = DEFAULT_PDF_URL):
    Color.disable_if_unwanted()
    has_local_pdf = pdf_destination.exists()
    is_download_needed = is_forced or not has_local_pdf and is_pdf_stale_check_due(pdf_destination)
    changed = False
    if is_download_needed:
        try:
            changed = download_if_changed_with_retries(url=pdf_url)
        except (RuntimeError, URLError, OSError) as error:
            if not has_local_pdf:
                raise SystemExit(f"No list of verblijfkosten rates available and download failed: {error}")
            sidecar_path = sidecar_path_for(pdf_destination)
            last_check = datetime.fromtimestamp(sidecar_path.stat().st_mtime, tz=timezone.utc).date()
            print(f"Download of the rates pdf failed ({error}), continuing with the local copy downloaded at {last_check}", file=sys.stderr)

    create_toml = changed or not toml_destination.exists()
    if create_toml:
        verblijf_toml_meta = VerblijfTomlMeta(bron=pdf_url, toml_destination=toml_destination, pdf_location=pdf_destination)
        create_verblijf_toml(verblijf_toml_meta)

def load_verblijf_tarieven(location: Path = DEFAULT_TOML_DESTINATION) -> list[Tarief]:
    with open(location, "rb") as verblijf_file:
        verblijf = tomllib.load(verblijf_file, parse_float=Decimal)
    try:
        tarieven = verblijf["tarief"]
        return tarieven
    except KeyError as e:
        e.add_note("Try to delete verblijf.toml and rerun the script.")
        raise

def input_int(message: str, non_negative=False) -> int:
    integer = None
    while integer is None:
        try:
            integer = int(input(message))
            if non_negative and integer < 0:
                print(f"{Color.danger}Error{Color.reset}: Your input must be non-negative. Please try again with something different.",file=sys.stderr)
                integer = None
        except ValueError as e:
            print(f"{Color.danger}Error{Color.reset}: Could not parse your input as a base 10 integer. Please try again with something different.",file=sys.stderr)
            continue
    return integer

def input_decimal(message: str) -> Decimal:
    decimal = None
    while decimal is None:
        try:
            decimal = Decimal(input(message))
        except (ValueError, InvalidOperation):
            print(f"{Color.danger}Error{Color.reset}: Could not parse your input as a decimal. Please try again with something different.",file=sys.stderr)
            continue
    return decimal

def warn_if_stale_pdf(destination: Path) -> None:
    now = datetime.now(timezone.utc)
    pdf_creation = datetime.fromtimestamp(destination.stat().st_mtime, tz=timezone.utc)
    pdf_age = now - pdf_creation
    if pdf_age < STALE_WARNING_AGE:
        return
    print(f"{Color.waarschuw}Let op{Color.reset}: tarieflijst is {Color.amount}{pdf_age.days}{Color.reset} dagen oud, mogelijk verouderd", file=sys.stderr)

class Solution(TypedDict):
    logies: Decimal
    ontbijt: Decimal
    lunch: Decimal
    diner: Decimal
    urencomp: Decimal

def max_possible_reimbursement(tarief: Tarief, nights: int) -> Decimal:
    max_ontbijt = nights * tarief['ontbijt']
    max_lunch = nights * tarief['lunch']
    max_diner = nights * tarief['diner']
    max_uren = nights * tarief['urencomp'] * nights * 24
    max_compensatie = max_ontbijt + max_lunch + max_diner + max_uren
    return max_compensatie

class SolverGaveUpError(Exception):
    f"""Undecided: limit reached ({MAX_SOLVE_ATTEMPTS}) before the solver could find a solution."""

def addTariefVariablesConstraints(problem: Problem, tarief: Tarief, nights: int) -> Problem:
    problem.addVariable("ontbijt", [tarief['ontbijt'] * night for night in range(0, nights + 1)])
    problem.addVariable("lunch", [tarief['lunch'] * night for night in range(0, nights + 1)])
    problem.addVariable("diner", [tarief['diner'] * night for night in range(0, nights + 1)])
    problem.addVariable("urencomp", [tarief['urencomp'] * night for night in range(0, (nights + 1) * 24)])

    return problem

def solve(tarief: Tarief, kosten: Decimal, nights: int, stepsize= DEFAULT_STEPSIZE) -> Solution:
    problem = Problem()
    problem = addTariefVariablesConstraints(problem, tarief, nights)
    problem.addConstraint(ExactSumConstraint(kosten))

    stepsize = Decimal(stepsize)
    attempt = 0

    # If we have not found an exact solution yet, try again with wider gaps
    for attempt in range(MAX_SOLVE_ATTEMPTS):
        problem.reset()
        problem = addTariefVariablesConstraints(problem, tarief, nights)
        # We want to find a solution that always at least reimburses the cost that was made
        problem.addConstraint(MaxSumConstraint(kosten + tarief['urencomp'] + stepsize * attempt))
        problem.addConstraint(MinSumConstraint(kosten))
        solution: Solution = problem.getSolution()
        if solution:
            break
    else:
        s = 's' if attempt != 1 else ''
        maximum = round(kosten + stepsize * attempt, 2)
        raise SolverGaveUpError(f"After {MAX_SOLVE_ATTEMPTS} attempt{s} the solver gave up. No solutions found in the reimbursement range of €{kosten} to €{maximum} ")


    assert solution, "No solution could be found"

    solution['ontbijt'] = solution['ontbijt'] / tarief['ontbijt']
    solution['lunch'] = solution['lunch'] / tarief['lunch']
    solution['diner'] = solution['diner'] / tarief['diner']
    solution['urencomp'] = solution['urencomp'] / tarief['urencomp']

    return solution

def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--country", help="Which country did you go to?", required=False)
    parser.add_argument("--region", help="Which region did you go to (see verblijf.toml)?", required=False)
    parser.add_argument("--logies-cost", help="How many logies cost did you make?", required=False, type=Decimal)
    parser.add_argument("--costs", help="How many other cost did you make?", required=False, type=Decimal)
    parser.add_argument("--nights", help="How many nights were you away?", required=False, type=int)
    parser.add_argument("--force-download", help="Force downloading the pdf with the amounts", required=False, default=False, action='store_true')
    parser.add_argument("--reimburse-overspend-logies", help="Force downloading the pdf with the amounts", required=False, default=False, action='store_true')

    args = parser.parse_args()

    setup(args.force_download)

    verblijf_tarieven = load_verblijf_tarieven()

    regions = None # A list of regions belonging to the country
    while regions is None:
        if args.country is None:
            args.country = input("Which country did you go to: ")
        try:
            regions = find_country(args.country, verblijf_tarieven)
        except LookupError as e:
            print(e)
            args.country = None
            continue

    region_name = args.region
    region = None
    while region is None:
        try:
            region = choose_region(regions, region_name)
        except LookupError as e:
            print(e)
            region_name = None
            continue

    if args.logies_cost is None:
        args.logies_cost = input_decimal("Enter how many logies cost you made: ")

    if args.costs is None:
        args.costs = input_decimal("Enter many other cost you made besides logies: ")

    if args.nights is None:
        args.nights = input_int("Enter how many nights were you away: ", non_negative=True)
    elif args.nights < 0:
        print(f"{Color.danger}Error{Color.reset}: Your nights must be non-negative. Please try again with a positive number of nights.",file=sys.stderr)
        return 1
    s = 's' if args.nights != 1 else ''

    allowed_logies = region["logies"] * args.nights
    overspend_logies = args.logies_cost > allowed_logies
    if overspend_logies:
        logies_to_declare = allowed_logies
        logies_to_retrieve = args.logies_cost - allowed_logies
        maximum = f" (You will declare the maximum logies allowed: {Color.amount}€{region['logies']}{Color.reset} per night)."
        if args.reimburse_overspend_logies:
            args.costs += logies_to_retrieve
            maximum += f"\nThe rest of the logies cost: €{Color.amount}{round(logies_to_retrieve, 2)}{Color.reset} will be reimbursed using more meals or hours. This is only allowed if agreed upon in advance/dit mag alleen in overleg!"
    else:
        logies_to_declare = args.logies_cost
        maximum = ""

    # Check if the reimbursement is within the max possible range
    possible_reimbursement = max_possible_reimbursement(region, args.nights)
    too_much = args.costs > possible_reimbursement
    if too_much:
        print(f"You spend more than can be reimbursed using meals and hours for the number of night{s} of your verblijf.")
        print(f"You want to reimburse €{Color.amount}{args.costs}{Color.reset} but for {args.nights} night{s} you can only declare €{Color.amount}{possible_reimbursement}{Color.reset}.")
        print(f"So declare {args.nights}x ontbijt, {args.nights}x lunch, {args.nights}x diner and {args.nights * 24} hours to at least get €{Color.amount}{possible_reimbursement}{Color.reset} back.")
        if args.reimburse_overspend_logies:
            too_much_without_logies = possible_reimbursement >= args.costs - args.logies_cost
            if too_much_without_logies:
                print(f"\nWithout trying to reimburse the overspend logies from the meals and hours it is possible to declare all your cost with {args.nights} nights.")
                print("Rerun without '--reimburse-overspend-logies' to see the results of that analysis.")
        return 0

    solution = solve(region, args.costs, args.nights)

    vergoeding = solution['ontbijt'] * region['ontbijt'] + solution['lunch'] * region['lunch'] + solution['diner'] * region['diner'] + solution['urencomp'] * region['urencomp']

    print(f"""{Color.title}Declare the following for your {args.nights} night{s} in {region['land']} {region['regio']}:{Color.reset}
   
Logies to declare: €{Color.amount}{logies_to_declare}{Color.reset}{maximum}

{Color.bold_amount}{solution['ontbijt']}x{Color.reset} Ontbijt\t({solution['ontbijt']} ontbijt × €{region['ontbijt']} = €{Color.amount}{solution['ontbijt'] * region['ontbijt']}{Color.reset})
{Color.bold_amount}{solution['lunch']}x{Color.reset} Lunch\t({solution['lunch']} lunch × €{region['lunch']} = €{Color.amount}{solution['lunch'] * region['lunch']}{Color.reset})
{Color.bold_amount}{solution['diner']}x{Color.reset} Diner\t({solution['diner']} diner × €{region['diner']} = €{Color.amount}{solution['diner'] * region['diner']}{Color.reset})
{Color.bold_amount}{solution['urencomp']}x{Color.reset} Uren\t({solution['urencomp']} uren × €{region['urencomp']} = €{Color.amount}{solution['urencomp'] * region['urencomp']}{Color.reset})

In total: {solution['ontbijt'] * region['ontbijt']} + {solution['lunch'] * region['lunch']} + {solution['diner'] * region['diner']} + {solution['urencomp'] * region['urencomp']} = €{Color.amount}{vergoeding}{Color.reset}/€{Color.amount}{args.costs}{Color.reset} reimbursed.""")

    # print(sys.argv[0], f"--country {region['land']} --region {region['regio']} --logies-cost {args.logies_cost} --costs {args.costs} --nights {args.nights} {'--reimburse-overspend-logies ' if args.reimburse_overspend_logies else ''}{'--force-download' if args.force_download else ''}")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())

# Copyright (C) 2023 Jan Philipp Berg <git.7ksst@aleeas.com>
#
# This file is part of recipe2txt.
#
# recipe2txt is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, either
# version 3 of the License, or (at your option) any later version.
#
# recipe2txt is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with recipe2txt.
# If not, see <https://www.gnu.org/licenses/>.

import urllib
from enum import IntEnum
from typing import NewType, Final, Optional, NamedTuple, Any, Callable
import re
from os import linesep
import traceback
from importlib_metadata import version
import recipe_scrapers
from recipe_scrapers._exceptions import WebsiteNotImplementedError, NoSchemaFoundInWildMode, SchemaOrgException, \
    ElementNotFoundInHtml
from recipe2txt.utils.markdown import *
from recipe2txt.utils.ContextLogger import get_logger, QueueContextManager as QCM
from recipe2txt.utils.traceback_utils import get_shared_frames, format_stacks
from recipe2txt.utils.misc import URL, dict2str, Counts, is_url
from recipe2txt.utils.conditional_imports import LiteralString

logger = get_logger(__name__)

Parsed = NewType('Parsed', recipe_scrapers._abstract.AbstractScraper)
NA: Final[LiteralString] = "N/A"
SCRAPER_VERSION: Final[str] = version('recipe_scrapers')


class RecipeStatus(IntEnum):
    NOT_INITIALIZED = -1
    UNREACHABLE = 0
    UNKNOWN = 1
    INCOMPLETE_ESSENTIAL = 2
    INCOMPLETE_ON_DISPLAY = 3
    COMPLETE_ON_DISPLAY = 4
    COMPLETE = 5


DUMMY_URL: Final[URL] = URL("https://notinitialized.no")


class Recipe(NamedTuple):
    ingredients: str = NA
    instructions: str = NA
    title: str = NA
    total_time: str = NA
    yields: str = NA
    host: str = NA
    image: str = NA
    nutrients: str = NA
    url: URL = DUMMY_URL
    status: RecipeStatus = RecipeStatus.NOT_INITIALIZED
    scraper_version: str = '-1'


UNINIT_RECIPE: Final[Recipe] = Recipe()


def none2na(t: tuple[Any, ...]) -> tuple[Any, ...]:
    if len(t) > len(RECIPE_ATTRIBUTES):
        raise ValueError(f"Expected a Recipe-based tuple (length {len(t)},"
                         f" but got something longer (length {len(RECIPE_ATTRIBUTES)})")
    if None in t:
        tmp = list(t)
        t = tuple([tmp[i] if tmp[i] else getattr(UNINIT_RECIPE, RECIPE_ATTRIBUTES[i]) for i in range(len(tmp))])
    return t


ESSENTIAL: Final[list[LiteralString]] = [
    "ingredients",
    "instructions"
]
ON_DISPLAY: Final[list[LiteralString]] = ESSENTIAL + [
    "title",
    "total_time",
    "yields",
]
METHODS: Final[list[LiteralString]] = ON_DISPLAY + [
    "host",
    "image",
    "nutrients"
]
RECIPE_ATTRIBUTES: Final[list[LiteralString]] = METHODS + [
    "url",
    "status",
    "scraper_version"
]


def int2status(t: tuple[Any, ...]) -> tuple[Any, ...]:
    if len(t) != len(RECIPE_ATTRIBUTES):
        raise ValueError(f"Wanted length of {len(RECIPE_ATTRIBUTES)}, got {len(t)}")
    assert RECIPE_ATTRIBUTES[-2] == "status"
    try:
        status = RecipeStatus(int(t[-2]))
    except ValueError:
        status = RecipeStatus.NOT_INITIALIZED
    return t[:-2] + (status, t[-1])


class ParsingError(NamedTuple):
    url: URL
    traceback: traceback.TracebackException


categorized_errors: dict[str, dict[str, dict[str, list[ParsingError]]]] = dict()


def handle_parsing_error(url: URL, exception: Exception, method: Optional[str] = None,
                         log: Optional[Callable[..., None]] = None, save_error: bool = True) -> Optional[ParsingError]:
    exception_name = type(exception).__name__
    log = log if log else logger.error
    if method:
        log("No %s found: ", method, exc_info=exception)
    else:
        log("Parsing error: ", exc_info=exception)

    if not save_error:
        return None

    parsing_error = ParsingError(url=url, traceback=traceback.TracebackException.from_exception(exception))
    method = method if method else "general parsing error"
    host = urllib.parse.urlparse(url).hostname
    if not host:
        logger.warning("Could not extract host from %s ", url)
        host = url
    if host in categorized_errors:
        if method in categorized_errors[host]:
            if exception_name in categorized_errors[host][method]:
                categorized_errors[host][method][exception_name].append(parsing_error)
            else:
                categorized_errors[host][method][exception_name] = [parsing_error]
        else:
            categorized_errors[host][method] = {exception_name: [parsing_error]}
    else:
        categorized_errors[host] = {method: {exception_name: [parsing_error]}}

    return parsing_error


pre_check_msg: Final[LiteralString] = """
--- MESSAGE GENERATED BY recipe2txt ---

**Pre-filing checks**

- [ ] I have searched for open issues that report the same problem
- [ ] I have checked that the bug affects the latest version of the library

**Information**

"""


def errors2str() -> list[tuple[str, str]]:
    reports = []
    for host, methoddict in categorized_errors.items():
        for method, exception_names in methoddict.items():
            for exception_name, parsing_error_list in exception_names.items():
                msg = pre_check_msg

                host = host[4:] if host.startswith("www.") else host
                title = f"{host.split('.')[0]}: {method} - {exception_name} (found by recipe2txt)"

                urls = [parsing_error.url for parsing_error in parsing_error_list]
                triggered_by = f"scrape_html()" if method == "general parsing error" else f".{method}()"
                infos = unordered("host: " + code(host),
                                  "recipe-scrapers version: " + code(SCRAPER_VERSION),
                                  "exception: " + code(exception_name),
                                  "triggered by calling: " +  code(triggered_by),
                                  "triggered on: ") + unordered(*urls, level=1)

                tb_ex_list = [error.traceback for error in parsing_error_list]
                shared_frames = get_shared_frames(tb_ex_list)
                formatted_stacks = format_stacks(tb_ex_list, shared_frames, "recipe2txt")

                if len(urls) > 1:
                    dot_explanation = italic("'...' indicates frames present in all traces"
                                             "(but only shown in the first)") + linesep * 2
                else:
                    dot_explanation = ""

                stack_traces = [f"{bold('Stack Traces')}{linesep*2}", dot_explanation]

                for error, stack in zip(parsing_error_list, formatted_stacks):
                    stack_traces.append(f"URL: {error.url}{linesep * 2}")
                    stack_traces += codeblock(*stack, language="python")
                    stack_traces.append(linesep * 2)

                msg += "".join(infos) + linesep + "".join(stack_traces)
                reports.append((title, msg))

    return reports


contains_alphanumeric = re.compile("\w")
def get_url(parsed: Parsed) -> URL:
    if parsed.url:
        if is_url(parsed.url):
            return parsed.url
        else:
            logger.error("Not an URL: %s", parsed.url)
    else:
        logger.error("No URL for parsed data")
    return DUMMY_URL

def info2str(method: str, info: Any) -> str:
    log = logger.error if method in ON_DISPLAY else logger.warning
    unexpected_type = True
    method_name = method.replace("_", " ")

    if isinstance(info, (int, float)):
        info = None if info == 0 else str(info)
        unexpected_type = False
    elif info:
        if isinstance(info, str):
            unexpected_type = False
        if method == "ingredients":
            if isinstance(info, list):
                if len(info[0]) < 2:
                    for i in range(len(info)):
                        if not info[i]:
                            info[i] = ' '
                        elif info[i] == ',':
                            info[i] = linesep
                    info = "".join(info)
                else:
                    info = linesep.join(info)
                unexpected_type = False
        elif method == "nutrients":
            if isinstance(info, dict):
                info = dict2str(info)
                unexpected_type = False
        elif method == "instructions":
            if isinstance(info, str):
                info = info.replace(linesep*2, linesep)
                unexpected_type = False
            elif isinstance(info, list):
                info = linesep.join(info)
                unexpected_type = False
    info = info if info and contains_alphanumeric.search(info) else None
    if not info or info.isspace() or info == "None":
        log("%s contains nothing", method_name.capitalize())
        return NA
    if unexpected_type:
        log("'%s' has the unhandled type %s", method, type(info))
    return str(info)


def _get_info(method: str, data: Parsed, url: URL) -> Any:
    log = logger.error if method in ON_DISPLAY else logger.warning
    method_name = method.replace("_", " ")

    info = None
    try:
        info = getattr(data, method)()
    except (SchemaOrgException, ElementNotFoundInHtml, TypeError, AttributeError, KeyError) as e:
        handle_parsing_error(get_url(data), e, method_name, log)
    except NotImplementedError:
        log("%s not implemented for this website", method_name.capitalize())
    except Exception as e:
        if type(e) in (KeyboardInterrupt, SystemExit, MemoryError):
            raise e
        log("Extraction error for attribute %s:", method_name, exc_info=e)

    return info if info else NA


BETWEEN_RECIPES: Final[str] = linesep * 5


def gen_status(infos: list[str]) -> RecipeStatus:
    if len(infos) > len(METHODS):
        raise ValueError("This function only analyzes attributes contained in html2recipe.methods." +
                         f" Expected {len(METHODS)} elements, got {len(infos)}")
    for i in range(len(ESSENTIAL)):
        if infos[i] == NA:
            return RecipeStatus.INCOMPLETE_ESSENTIAL
    for i in range(len(ESSENTIAL), len(ON_DISPLAY)):
        if infos[i] == NA:
            return RecipeStatus.INCOMPLETE_ON_DISPLAY
    for i in range(len(ON_DISPLAY), len(METHODS)):
        if infos[i] == NA:
            return RecipeStatus.COMPLETE_ON_DISPLAY
    return RecipeStatus.COMPLETE


def parsed2recipe(parsed: Parsed) -> Recipe:
    logger.info("Parsing HTML")
    infos = []
    for method in METHODS:
        info = get_info(method, parsed)
        info_str = info2str(method, info)
        infos.append(info_str)

    status = gen_status(infos)
    recipe = Recipe(url=get_url(parsed), status=status, scraper_version=SCRAPER_VERSION,
                    ingredients=infos[0], instructions=infos[1],
                    title=infos[2], total_time=infos[3], yields=infos[4],
                    host=infos[5], image=infos[6], nutrients=infos[7])
    return recipe


def _re2md(recipe: Recipe) -> list[str]:
    title = recipe.title if recipe.title != NA else recipe.url
    title = esc(title)
    url = esc(recipe.url)
    host = italic(esc(recipe.host))
    if host == NA:
        host = None  # type: ignore

    escaped = [esc(item) for item in recipe.ingredients.split(linesep)]
    ingredients = unordered(*escaped)

    escaped = [esc(step) for step in recipe.instructions.split(linesep)]
    instructions = ordered(*escaped)

    md = [header(title, 2, True), paragraph(),
          recipe.total_time + " min | " + recipe.yields, paragraph()] + \
         ingredients + [EMPTY_COMMENT] + instructions + \
         [paragraph(), italic("from:"), " ", link(url, host), paragraph()]

    return md


def _re2txt(recipe: Recipe) -> list[str]:
    title = recipe.title if recipe.title != NA else recipe.url
    txt = [title,
           linesep*2,
           recipe.total_time + " min | " + recipe.yields + linesep*2,
           recipe.ingredients,
           linesep * 2,
           recipe.instructions.replace(linesep, linesep * 2),
           linesep * 2,
           "from: " + recipe.url,
           BETWEEN_RECIPES]
    return txt


def recipe2out(recipe: Recipe, counts: Optional[Counts] = None, md: bool = False) -> Optional[list[str]]:
    if recipe.status <= RecipeStatus.INCOMPLETE_ESSENTIAL:
        logger.error("Nothing worthwhile could be extracted. Skipping...")
        return None
    if counts:
        if recipe.status < RecipeStatus.INCOMPLETE_ON_DISPLAY:
            counts.parsed_partially += 1
        else:
            counts.parsed_successfully += 1

    if md:
        return _re2md(recipe)
    else:
        return _re2txt(recipe)


def html2parsed(url: URL, content: str) -> Optional[Parsed]:
    try:
        parsed: Parsed = Parsed(recipe_scrapers.scrape_html(html=content, org_url=url))
    except (WebsiteNotImplementedError,
            NoSchemaFoundInWildMode):
        logger.error("Unknown Website. Extraction not supported")
        return None
    except (AttributeError, TypeError) as e:
        handle_parsing_error(url, e)
        return None
    except Exception as e:
        if type(e) in (KeyboardInterrupt, SystemExit, MemoryError):
            raise e
        logger.error("Parsing error: ", exc_info=e)
        return None

    return parsed

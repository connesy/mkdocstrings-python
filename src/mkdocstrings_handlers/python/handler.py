"""This module implements a handler for the Python language."""

from __future__ import annotations

import copy
import glob
import os
import posixpath
import re
import sys
from collections import ChainMap
from contextlib import suppress
from typing import TYPE_CHECKING, Any, BinaryIO, ClassVar, Iterator, Mapping

from griffe.collections import LinesCollection, ModulesCollection
from griffe.docstrings.parsers import Parser
from griffe.exceptions import AliasResolutionError
from griffe.extensions import load_extensions
from griffe.loader import GriffeLoader
from griffe.logger import patch_loggers
from mkdocstrings.extension import PluginError
from mkdocstrings.handlers.base import BaseHandler, CollectionError, CollectorItem
from mkdocstrings.inventory import Inventory
from mkdocstrings.loggers import get_logger

from mkdocstrings_handlers.python import rendering

if TYPE_CHECKING:
    from markdown import Markdown


if sys.version_info >= (3, 11):
    from contextlib import chdir
else:
    # TODO: remove once support for Python 3.10 is dropped
    from contextlib import contextmanager

    @contextmanager
    def chdir(path: str) -> Iterator[None]:  # noqa: D103
        old_wd = os.getcwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(old_wd)


logger = get_logger(__name__)

patch_loggers(get_logger)


class PythonHandler(BaseHandler):
    """The Python handler class.

    Attributes:
        domain: The cross-documentation domain/language for this handler.
        enable_inventory: Whether this handler is interested in enabling the creation
            of the `objects.inv` Sphinx inventory file.
        fallback_theme: The fallback theme.
        fallback_config: The configuration used to collect item during autorefs fallback.
        default_config: The default rendering options,
            see [`default_config`][mkdocstrings_handlers.python.handler.PythonHandler.default_config].
    """

    domain: str = "py"  # to match Sphinx's default domain
    enable_inventory: bool = True
    fallback_theme = "material"
    fallback_config: ClassVar[dict] = {"fallback": True}
    default_config: ClassVar[dict] = {
        "docstring_style": "google",
        "docstring_options": {},
        "show_root_heading": False,
        "show_root_toc_entry": True,
        "show_root_full_path": True,
        "show_root_members_full_path": False,
        "show_object_full_path": False,
        "show_category_heading": False,
        "show_if_no_docstring": False,
        "show_signature": True,
        "show_signature_annotations": False,
        "signature_crossrefs": False,
        "separate_signature": False,
        "line_length": 60,
        "merge_init_into_class": False,
        "show_docstring_attributes": True,
        "show_docstring_functions": True,
        "show_docstring_classes": True,
        "show_docstring_modules": True,
        "show_docstring_description": True,
        "show_docstring_examples": True,
        "show_docstring_other_parameters": True,
        "show_docstring_parameters": True,
        "show_docstring_raises": True,
        "show_docstring_receives": True,
        "show_docstring_returns": True,
        "show_docstring_warns": True,
        "show_docstring_yields": True,
        "show_source": True,
        "show_bases": True,
        "show_submodules": False,
        "group_by_category": True,
        "heading_level": 2,
        "members_order": rendering.Order.alphabetical.value,
        "docstring_section_style": "table",
        "members": None,
        "inherited_members": False,
        "filters": ["!^_[^_]"],
        "annotations_path": "brief",
        "preload_modules": None,
        "load_external_modules": False,
        "allow_inspection": True,
    }
    """
    Attributes: General options:
        allow_inspection (bool): Whether to allow inspecting modules when visiting them is not possible. Default: `True`.
        show_bases (bool): Show the base classes of a class. Default: `True`.
        show_source (bool): Show the source code of this object. Default: `True`.
        preload_modules (list[str] | None): Pre-load modules that are
            not specified directly in autodoc instructions (`::: identifier`).
            It is useful when you want to render documentation for a particular member of an object,
            and this member is imported from another package than its parent.

            For an imported member to be rendered, you need to add it to the `__all__` attribute
            of the importing module.

            The modules must be listed as an array of strings. Default: `None`.

    Attributes: Headings options:
        heading_level (int): The initial heading level to use. Default: `2`.
        show_root_heading (bool): Show the heading of the object at the root of the documentation tree
            (i.e. the object referenced by the identifier after `:::`). Default: `False`.
        show_root_toc_entry (bool): If the root heading is not shown, at least add a ToC entry for it. Default: `True`.
        show_root_full_path (bool): Show the full Python path for the root object heading. Default: `True`.
        show_root_members_full_path (bool): Show the full Python path of the root members. Default: `False`.
        show_object_full_path (bool): Show the full Python path of every object. Default: `False`.
        show_category_heading (bool): When grouped by categories, show a heading for each category. Default: `False`.

    Attributes: Members options:
        inherited_members (list[str] | bool | None): A boolean, or an explicit list of inherited members to render.
            If true, select all inherited members, which can then be filtered with `members`.
            If false or empty list, do not select any inherited member. Default: `False`.
        members (list[str] | bool | None): A boolean, or an explicit list of members to render.
            If true, select all members without further filtering.
            If false or empty list, do not render members.
            If none, select all members and apply further filtering with filters and docstrings. Default: `None`.
        members_order (str): The members ordering to use. Options: `alphabetical` - order by the members names,
            `source` - order members as they appear in the source file. Default: `"alphabetical"`.
        filters (list[str] | None): A list of filters applied to filter objects based on their name.
            A filter starting with `!` will exclude matching objects instead of including them.
            The `members` option takes precedence over `filters` (filters will still be applied recursively
            to lower members in the hierarchy). Default: `["!^_[^_]"]`.
        group_by_category (bool): Group the object's children by categories: attributes, classes, functions, and modules. Default: `True`.
        show_submodules (bool): When rendering a module, show its submodules recursively. Default: `False`.

    Attributes: Docstrings options:
        docstring_style (str): The docstring style to use: `google`, `numpy`, `sphinx`, or `None`. Default: `"google"`.
        docstring_options (dict): The options for the docstring parser. See parsers under [`griffe.docstrings`][].
        docstring_section_style (str): The style used to render docstring sections. Options: `table`, `list`, `spacy`. Default: `"table"`.
        merge_init_into_class (bool): Whether to merge the `__init__` method into the class' signature and docstring. Default: `False`.
        show_if_no_docstring (bool): Show the object heading even if it has no docstring or children with docstrings. Default: `False`.
        show_docstring_attributes (bool): Whether to display the "Attributes" section in the object's docstring. Default: `True`.
        show_docstring_functions (bool): Whether to display the "Functions" or "Methods" sections in the object's docstring. Default: `True`.
        show_docstring_classes (bool): Whether to display the "Classes" section in the object's docstring. Default: `True`.
        show_docstring_modules (bool): Whether to display the "Modules" section in the object's docstring. Default: `True`.
        show_docstring_description (bool): Whether to display the textual block (including admonitions) in the object's docstring. Default: `True`.
        show_docstring_examples (bool): Whether to display the "Examples" section in the object's docstring. Default: `True`.
        show_docstring_other_parameters (bool): Whether to display the "Other Parameters" section in the object's docstring. Default: `True`.
        show_docstring_parameters (bool): Whether to display the "Parameters" section in the object's docstring. Default: `True`.
        show_docstring_raises (bool): Whether to display the "Raises" section in the object's docstring. Default: `True`.
        show_docstring_receives (bool): Whether to display the "Receives" section in the object's docstring. Default: `True`.
        show_docstring_returns (bool): Whether to display the "Returns" section in the object's docstring. Default: `True`.
        show_docstring_warns (bool): Whether to display the "Warns" section in the object's docstring. Default: `True`.
        show_docstring_yields (bool): Whether to display the "Yields" section in the object's docstring. Default: `True`.

    Attributes: Signatures/annotations options:
        annotations_path (str): The verbosity for annotations path: `brief` (recommended), or `source` (as written in the source). Default: `"brief"`.
        line_length (int): Maximum line length when formatting code/signatures. Default: `60`.
        show_signature (bool): Show methods and functions signatures. Default: `True`.
        show_signature_annotations (bool): Show the type annotations in methods and functions signatures. Default: `False`.
        signature_crossrefs (bool): Whether to render cross-references for type annotations in signatures. Default: `False`.
        separate_signature (bool): Whether to put the whole signature in a code block below the heading.
            If Black is installed, the signature is also formatted using it. Default: `False`.
    """

    def __init__(
        self,
        *args: Any,
        config_file_path: str | None = None,
        paths: list[str] | None = None,
        locale: str = "en",
        **kwargs: Any,
    ) -> None:
        """Initialize the handler.

        Parameters:
            *args: Handler name, theme and custom templates.
            config_file_path: The MkDocs configuration file path.
            paths: A list of paths to use as Griffe search paths.
            locale: The locale to use when rendering content.
            **kwargs: Same thing, but with keyword arguments.
        """
        super().__init__(*args, **kwargs)
        self._config_file_path = config_file_path
        paths = paths or []
        glob_base_dir = os.path.dirname(os.path.abspath(config_file_path)) if config_file_path else "."
        with chdir(glob_base_dir):
            resolved_globs = [glob.glob(path) for path in paths]
        paths = [path for glob_list in resolved_globs for path in glob_list]
        if not paths and config_file_path:
            paths.append(os.path.dirname(config_file_path))
        search_paths = [path for path in sys.path if path]  # eliminate empty path
        for path in reversed(paths):
            if not os.path.isabs(path) and config_file_path:
                path = os.path.abspath(os.path.join(os.path.dirname(config_file_path), path))  # noqa: PLW2901
            if path not in search_paths:
                search_paths.insert(0, path)
        self._paths = search_paths
        self._modules_collection: ModulesCollection = ModulesCollection()
        self._lines_collection: LinesCollection = LinesCollection()
        self._locale = locale

    @classmethod
    def load_inventory(
        cls,
        in_file: BinaryIO,
        url: str,
        base_url: str | None = None,
        domains: list[str] | None = None,
        **kwargs: Any,  # noqa: ARG003
    ) -> Iterator[tuple[str, str]]:
        """Yield items and their URLs from an inventory file streamed from `in_file`.

        This implements mkdocstrings' `load_inventory` "protocol" (see [`mkdocstrings.plugin`][mkdocstrings.plugin]).

        Arguments:
            in_file: The binary file-like object to read the inventory from.
            url: The URL that this file is being streamed from (used to guess `base_url`).
            base_url: The URL that this inventory's sub-paths are relative to.
            domains: A list of domain strings to filter the inventory by, when not passed, "py" will be used.
            **kwargs: Ignore additional arguments passed from the config.

        Yields:
            Tuples of (item identifier, item URL).
        """
        domains = domains or ["py"]
        if base_url is None:
            base_url = posixpath.dirname(url)

        for item in Inventory.parse_sphinx(in_file, domain_filter=domains).values():
            yield item.name, posixpath.join(base_url, item.uri)

    def collect(self, identifier: str, config: Mapping[str, Any]) -> CollectorItem:  # noqa: D102
        module_name = identifier.split(".", 1)[0]
        unknown_module = module_name not in self._modules_collection
        if config.get("fallback", False) and unknown_module:
            raise CollectionError("Not loading additional modules during fallback")

        # See: https://github.com/python/typeshed/issues/8430
        mutable_config = dict(copy.deepcopy(config))
        final_config = ChainMap(mutable_config, self.default_config)
        parser_name = final_config["docstring_style"]
        parser_options = final_config["docstring_options"]
        parser = parser_name and Parser(parser_name)

        if unknown_module:
            loader = GriffeLoader(
                extensions=load_extensions(final_config.get("extensions", [])),
                search_paths=self._paths,
                docstring_parser=parser,
                docstring_options=parser_options,
                modules_collection=self._modules_collection,
                lines_collection=self._lines_collection,
                allow_inspection=final_config["allow_inspection"],
            )
            try:
                for pre_loaded_module in final_config.get("preload_modules") or []:
                    if pre_loaded_module not in self._modules_collection:
                        loader.load_module(pre_loaded_module)
                loader.load_module(module_name)
            except ImportError as error:
                raise CollectionError(str(error)) from error
            unresolved, iterations = loader.resolve_aliases(
                implicit=False,
                external=final_config["load_external_modules"],
            )
            if unresolved:
                logger.debug(f"{len(unresolved)} aliases were still unresolved after {iterations} iterations")
                logger.debug(f"Unresolved aliases: {', '.join(sorted(unresolved))}")

        try:
            doc_object = self._modules_collection[identifier]
        except KeyError as error:
            raise CollectionError(f"{identifier} could not be found") from error
        except AliasResolutionError as error:
            raise CollectionError(str(error)) from error

        if not unknown_module:
            with suppress(AliasResolutionError):
                if doc_object.docstring is not None:
                    doc_object.docstring.parser = parser
                    doc_object.docstring.parser_options = parser_options

        return doc_object

    def render(self, data: CollectorItem, config: Mapping[str, Any]) -> str:  # noqa: D102 (ignore missing docstring)
        # See https://github.com/python/typeshed/issues/8430
        mutabled_config = dict(copy.deepcopy(config))
        final_config = ChainMap(mutabled_config, self.default_config)

        template_name = rendering.do_get_template(data)
        template = self.env.get_template(template_name)

        # Heading level is a "state" variable, that will change at each step
        # of the rendering recursion. Therefore, it's easier to use it as a plain value
        # than as an item in a dictionary.
        heading_level = final_config["heading_level"]
        try:
            final_config["members_order"] = rendering.Order(final_config["members_order"])
        except ValueError as error:
            choices = "', '".join(item.value for item in rendering.Order)
            raise PluginError(
                f"Unknown members_order '{final_config['members_order']}', choose between '{choices}'.",
            ) from error

        if final_config["filters"]:
            final_config["filters"] = [
                (re.compile(filtr.lstrip("!")), filtr.startswith("!")) for filtr in final_config["filters"]
            ]

        # TODO: goal reached: remove once `signature_crossrefs` feature becomes public
        final_config["signature_crossrefs"] = False

        return template.render(
            **{
                "config": final_config,
                data.kind.value: data,
                "heading_level": heading_level,
                "root": True,
                "locale": self._locale,
            },
        )

    def update_env(self, md: Markdown, config: dict) -> None:  # noqa: D102 (ignore missing docstring)
        super().update_env(md, config)
        self.env.trim_blocks = True
        self.env.lstrip_blocks = True
        self.env.keep_trailing_newline = False
        self.env.filters["split_path"] = rendering.do_split_path
        self.env.filters["crossref"] = rendering.do_crossref
        self.env.filters["multi_crossref"] = rendering.do_multi_crossref
        self.env.filters["order_members"] = rendering.do_order_members
        self.env.filters["format_code"] = rendering.do_format_code
        self.env.filters["format_signature"] = rendering.do_format_signature
        self.env.filters["format_attribute"] = rendering.do_format_attribute
        self.env.filters["filter_objects"] = rendering.do_filter_objects
        self.env.filters["stash_crossref"] = lambda ref, length: ref
        self.env.filters["get_template"] = rendering.do_get_template
        self.env.tests["existing_template"] = lambda template_name: template_name in self.env.list_templates()

    def get_anchors(self, data: CollectorItem) -> tuple[str, ...]:  # noqa: D102 (ignore missing docstring)
        anchors = [data.path]
        try:
            if data.canonical_path != data.path:
                anchors.append(data.canonical_path)
            for anchor in data.aliases:
                if anchor not in anchors:
                    anchors.append(anchor)
        except AliasResolutionError:
            return tuple(anchors)
        return tuple(anchors)


def get_handler(
    theme: str,
    custom_templates: str | None = None,
    config_file_path: str | None = None,
    paths: list[str] | None = None,
    locale: str = "en",
    **config: Any,  # noqa: ARG001
) -> PythonHandler:
    """Simply return an instance of `PythonHandler`.

    Arguments:
        theme: The theme to use when rendering contents.
        custom_templates: Directory containing custom templates.
        config_file_path: The MkDocs configuration file path.
        paths: A list of paths to use as Griffe search paths.
        locale: The locale to use when rendering content.
        **config: Configuration passed to the handler.

    Returns:
        An instance of `PythonHandler`.
    """
    return PythonHandler(
        handler="python",
        theme=theme,
        custom_templates=custom_templates,
        config_file_path=config_file_path,
        paths=paths,
        locale=locale,
    )

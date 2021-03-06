from pathlib import PurePath, Path
import json
import hashlib
from collections import namedtuple
import pickle
from functools import wraps
from tempfile import TemporaryDirectory

import frontmatter
import yaml

from lettersmith.date import read_file_times, EPOCH, to_datetime
from lettersmith.file import write_file_deep
from lettersmith import path as pathtools
from lettersmith.util import replace, get, maps_if


_EMPTY_TUPLE = tuple()

Doc = namedtuple("Doc", (
    "id_path", "output_path", "input_path", "created", "modified",
    "title", "content", "section", "meta", "templates"
))
Doc.__doc__ = """
Docs are namedtuples that represent a document to be transformed,
and eventually written to disk.

Docs contain a content field — usually the whole contents of a
file. Since this can take up quite a bit of memory, it's typical to avoid
collecting all docs into memory. We usually load and transform them in
generator functions so that only one is in memory at a time.

For collecting many in memory, and cross-referencing, we use Stubs.
Stubs are meant to be stub docs. They contain just meta information
about the doc. You can turn a doc into a stub with
`lettersmith.stub.from_doc(doc)`.
"""


def doc(id_path, output_path,
    input_path=None, created=EPOCH, modified=EPOCH,
    title="", content="", section="", meta=None, templates=None):
    """
    Create a Doc tuple, populating it with sensible defaults
    """
    return Doc(
        id_path=str(id_path),
        output_path=str(output_path),
        input_path=str(input_path) if input_path is not None else None,
        created=to_datetime(created),
        modified=to_datetime(modified),
        title=str(title),
        content=str(content),
        section=str(section),
        meta=meta if meta is not None else {},
        templates=templates if templates is not None else _EMPTY_TUPLE
    )


@get.register(Doc)
def get_doc(doc, key, default=None):
    return getattr(doc, key, default)


@replace.register(Doc)
def replace_doc(doc, **kwargs):
    """
    Replace items in a Doc, returning a new Doc.
    """
    return doc._replace(**kwargs)


def replace_meta(doc, **kwargs):
    """
    Put a value into a doc's meta dictionary.
    Returns a new doc.
    """
    return replace(doc, meta=replace(doc.meta, **kwargs))


def load(pathlike, relative_to=""):
    """
    Loads a basic doc dictionary from a file path.
    `content` field will contain contents of file.
    Typically, you decorate the doc later with meta and other fields.

    Returns a doc.
    """
    file_created, file_modified = read_file_times(pathlike)
    with open(pathlike, 'r') as f:
        content = f.read()
    input_path = PurePath(pathlike)
    id_path = input_path.relative_to(relative_to)
    output_path = pathtools.to_nice_path(id_path)
    section = pathtools.tld(id_path)
    title = pathtools.to_title(input_path)
    return doc(
        id_path=id_path,
        output_path=output_path,
        input_path=input_path,
        created=file_created,
        modified=file_modified,
        title=title,
        section=section,
        meta={},
        content=content
    )


def from_stub(stub):
    """
    Create a doc dictionary from an stub dictionary.
    This doc dictionary will have an empty "content" field.

    If you want to load a doc from a file stub with an `input_path`,
    use `load_doc` instead.
    """
    return doc(
        id_path=stub.id_path,
        output_path=stub.output_path,
        input_path=stub.input_path,
        created=stub.created,
        modified=stub.modified,
        title=stub.title,
        section=stub.section,
        meta=stub.meta
    )


def to_json(doc):
    """
    Serialize a doc as JSON-serializable data
    """
    return {
        "@type": "doc",
        "id_path": doc.id_path,
        "output_path": doc.output_path,
        "input_path": doc.input_path,
        "created": doc.created.timestamp(),
        "modified": doc.modified.timestamp(),
        "title": doc.title,
        "section": doc.section,
        "content": doc.content,
        # TODO manually serialize meta?
        "meta": doc.meta,
        "templates": doc.templates
    }


def write(doc, output_dir):
    """
    Write a doc to the filesystem.

    Uses `doc.output_path` and `output_dir` to construct the output path.
    """
    write_file_deep(PurePath(output_dir).joinpath(doc.output_path), doc.content)


def uplift_meta(doc):
    """
    Reads "magic" fields in the meta and uplifts their values to doc
    properties.

    We use this to uplift title, created, modified fields in the
    frontmatterm, overriding original or default values on doc.
    """
    return doc._replace(
        title=doc.meta.get("title", doc.title),
        created=to_datetime(doc.meta.get("created", doc.created)),
        modified=to_datetime(doc.meta.get("modified", doc.modified))
    )


def uplifts_meta(func):
    """
    Decorates a simpler doc parsing function so that it will uplift meta items
    after running `func`.
    """
    @wraps(func)
    def wrapped(doc, *args, **kwargs):
        return uplift_meta(func(doc, *args, **kwargs))
    return wrapped


def ext(*exts):
    """
    Create an extension predicate function.
    """
    def has_ext(doc):
        return pathtools.has_ext(doc.id_path, *exts)
    return has_ext


def maps_if_ext(*exts):
    """
    Decorate a doc mapping function so it will only map a doc if the
    doc's `id_path` has one of the extensions listed in the `*ext` args.
    If the doc does not have any of those extensions, it is left
    untouched.
    """
    return maps_if(ext(*exts))


def change_ext(doc, ext):
    """Change the extention on a doc's output_path, returning a new doc."""
    updated_path = PurePath(doc.output_path).with_suffix(ext)
    return doc._replace(output_path=str(updated_path))


class DocException(Exception):
    pass


def annotates_exceptions(func):
    """
    Decorates a mapping function for docs, giving it a more useful
    exception message.
    """
    @wraps(func)
    def map_doc(doc, *args, **kwargs):
        try:
            return func(doc, *args, **kwargs)
        except Exception as e:
            msg = (
                'Error encountered while mapping doc '
                '"{id_path}" with {module}.{func}.'
            ).format(
                id_path=doc.id_path,
                func=func.__qualname__,
                module=func.__module__
            )
            raise DocException(msg) from e
    return map_doc


@annotates_exceptions
def parse_frontmatter(doc):
    meta, content = frontmatter.parse(doc.content)
    return doc._replace(
        meta=meta,
        content=content
    )


def uplifts_frontmatter(func):
    """
    Decorate a doc mapping function so it will `parse_frontmatter` and
    `uplift_meta` before passing the `doc` to `func`.

    You can decorate simpler markup rendering functions with
    `uplifts_frontmatter` so that you don't have to deal with parsing
    and uplifting frontmatter yourself.

    Usage:

        @uplifts_frontmatter
        def set_title(doc, title=""):
            return doc._replace(title=title)
    """
    @wraps(func)
    def wrapped(doc, *args, **kwargs):
        return func(uplift_meta(parse_frontmatter(doc)), *args, **kwargs)
    return wrapped


@uplifts_meta
@annotates_exceptions
def parse_yaml(doc):
    """
    Parse YAML in the doc's content property, placing it in meta
    and replacing content property with an empty string.
    """
    meta = yaml.load(doc.content)
    return doc._replace(
        meta=meta,
        content=""
    )


@uplifts_meta
@annotates_exceptions
def parse_json(doc):
    """
    Parse JSON in the doc's content property, placing it in meta
    and replacing content property with an empty string.
    """
    meta = json.loads(doc.content)
    return doc._replace(
        meta=meta,
        content=""
    )


def _hashstr(s):
    return hashlib.md5(str(s).encode()).hexdigest()


def _cache_path(id_path):
    """
    Read a doc ID path
    """
    return PurePath(_hashstr(id_path)).with_suffix('.pkl')


class DocCache:
    """
    DocCache - allows you to dump or load docs to disk.

    This lets us support loading a larger total number of docs, since
    not all of them need to be in memory at once.

    For convenience, you might want to use `DocCacheDir` instead of `DocCache`,
    because `DocCacheDir` automatically creates a temporary cache directory
    and will clean it up when you're done with it.
    """
    def __init__(self, cache_path):
        self.cache_path = Path(cache_path)

    def dump(self, doc):
        """
        Dump a doc into cache
        """
        doc_cache_path = _cache_path(doc.id_path)
        with open(PurePath(self.cache_path, doc_cache_path), "wb") as f:
            pickle.dump(doc, f)
            return doc

    def load(self, id_path):
        """
        Load a doc from cache by `id_path`
        """
        doc_cache_path = _cache_path(id_path)
        with open(PurePath(self.cache_path, doc_cache_path), "rb") as f:
            return pickle.load(f)

    def dump_each(self, docs):
        for doc in docs:
            self.dump(doc)
            yield doc

    def dump_all(self, docs):
        for doc in docs:
            self.dump(doc)

    def load_all(self):
        for file_path in self.cache_path.glob("*.pkl"):
            with open(file_path, "rb") as f:
                yield pickle.load(f)


class DocCacheDir:
    """
    Cache context manager that
    1. Creates a temporary directory
    2. Passes you an instance of DocCache to work with

    Note this is meant to be used as a context manager, with the `with`
    statement. The context manager will automatically clean up
    the temporary directory when `with` context exits.

    Usage:

        with DocCacheDir() as cache:
            cache.dump(doc)
            ...
            cache.load(some_id_path)
    """
    def __init__(self, docs=_EMPTY_TUPLE):
        self.__temporary_directory = TemporaryDirectory(prefix="lettersmith_")
        self.__cache = DocCache(self.__temporary_directory.name)
        self.__cache.dump_all(docs)

    def __enter__(self):
        return self.__cache

    def __exit__(self, *args):
        self.__temporary_directory.__exit__(*args)
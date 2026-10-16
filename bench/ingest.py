import asyncio, hashlib, pathlib
import cocoindex as coco
import pymupdf
from cocoindex.connectors import localfs
from cocoindex.resources.file import FileLike, PatternFilePathMatcher
from cocoindex.ops.text import RecursiveSplitter

COUNTERS = {'docs': 0, 'pages': 0, 'chunks': 0}
_splitter = RecursiveSplitter()
NL = chr(10)


def _parse(content: bytes):
    doc = pymupdf.open(stream=content, filetype='pdf')
    try:
        n = doc.page_count
        text = NL.join(page.get_text() for page in doc)
    finally:
        doc.close()
    return text, n


@coco.fn.as_async(memo=True)
async def pdf_to_text(content: bytes) -> str:
    text, n = await asyncio.to_thread(_parse, content)
    COUNTERS['pages'] += n
    return text


@coco.fn(memo=True)
async def ingest(file: FileLike, outdir: pathlib.Path) -> None:
    data = await file.read()
    sha = hashlib.sha256(data).hexdigest()
    text = await pdf_to_text(data)
    chunks = _splitter.split(text, chunk_size=2000, chunk_overlap=500, language='markdown')
    COUNTERS['docs'] += 1
    COUNTERS['chunks'] += len(chunks)
    localfs.declare_file(outdir / (sha + '.md'), text, create_parent_dirs=True)


@coco.fn
async def app_main(sourcedir: pathlib.Path, outdir: pathlib.Path) -> None:
    files = localfs.walk_dir(
        sourcedir, recursive=True,
        path_matcher=PatternFilePathMatcher(included_patterns=['**/*.pdf']),
    )
    await coco.mount_each(ingest, files.items(), outdir)


@coco.lifespan
async def _lifespan(builder: coco.EnvironmentBuilder) -> None:
    builder.settings.db_path = pathlib.Path(__file__).parent / 'cocoindex.db'
    yield


def make_app(name: str, sourcedir: pathlib.Path, outdir: pathlib.Path):
    return coco.App(coco.AppConfig(name=name), app_main,
                    sourcedir=sourcedir, outdir=outdir)

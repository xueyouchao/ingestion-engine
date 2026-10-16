import asyncio, pathlib, shutil, sys, time
from fpdf import FPDF
import pymupdf
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import ingest
from ingest import COUNTERS, make_app

HERE = pathlib.Path(__file__).parent
SYNTH = HERE / 'pdf_synth'
REAL = HERE / 'pdf_real'
MD_SYNTH = HERE / 'md_synth'
MD_REAL = HERE / 'md_real'


def wipe_state():
    for p in HERE.glob('cocoindex.db*'):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
    shutil.rmtree(MD_SYNTH, ignore_errors=True)
    shutil.rmtree(MD_REAL, ignore_errors=True)


def md_count(d):
    return len(list(d.glob('*.md'))) if d.exists() else 0


async def run_update(app, label):
    c0 = dict(COUNTERS)
    t0 = time.perf_counter()
    h = app.update()
    await h
    dt = time.perf_counter() - t0
    c1 = dict(COUNTERS)
    stats = None
    try:
        st = h.stats()
        stats = st.total if st else None
    except Exception:
        stats = None
    d_docs = c1['docs'] - c0['docs']
    d_pages = c1['pages'] - c0['pages']
    d_chunks = c1['chunks'] - c0['chunks']
    print('[%-24s] %6.2fs  docs=%-4d pages=%-5d chunks=%-5d | docs/s=%-7.1f pages/s=%-7.1f chunks/s=%-7.1f' % (
        label, dt, d_docs, d_pages, d_chunks,
        d_docs / dt, d_pages / dt, d_chunks / dt))
    if stats:
        print('      engine: starts=%d unchanged=%d adds=%d deletes=%d reproc=%d errors=%d' % (
            stats.num_execution_starts, stats.num_unchanged, stats.num_adds,
            stats.num_deletes, stats.num_reprocesses, stats.num_errors))
    return dt


def write_pdf(path, seed, pages):
    rng_words = ('pipeline retrieval index delta chunk embedding transform target '
                 'state lineage engine parallel memoization fingerprint upsert '
                 'warehouse graph vector semantic schema connector benchmark').split()
    doc = FPDF(); doc.set_auto_page_break(True)
    doc.add_page(); doc.set_font('Helvetica', 'B', 16)
    doc.multi_cell(180, 8, 'Edited study %d for benchmark phase' % seed)
    doc.set_font('Helvetica', size=11)
    import random
    r2 = random.Random(seed)
    for p in range(pages - 1):
        doc.add_page(); doc.set_font('Helvetica', size=11)
        for para in range(8):
            n = r2.randint(60, 90)
            doc.multi_cell(180, 6, ' '.join(r2.choice(rng_words) for _ in range(n)).capitalize() + '.')
            doc.ln(2)
    path.write_bytes(doc.output())


def baseline(corpus):
    t0 = time.perf_counter(); pages = 0; n = 0
    for p in sorted(corpus.glob('*.pdf')):
        doc = pymupdf.open(p)
        for page in doc:
            page.get_text(); pages += 1
        doc.close(); n += 1
    dt = time.perf_counter() - t0
    print('[baseline raw pymupdf    ] %6.2fs  docs=%d pages=%d | pages/s=%.1f  (no cocoindex, no chunk, no write)' % (
        dt, n, pages, pages / dt))
    return dt


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'full'
    if mode == 'smoke':
        app = make_app('Smoke', REAL, MD_REAL)
        await run_update(app, 'smoke real 20')
        print('md files:', md_count(MD_REAL))
        return

    wipe_state()
    print('=== PHASE 1: cold, 20 real arXiv papers (55MB) ===')
    app_real = make_app('PdfBenchReal', REAL, MD_REAL)
    await run_update(app_real, 'cold real-20')
    baseline(REAL)

    print()
    print('=== PHASE 2: cold, 300 synthetic PDFs x 8 pages ===')
    app_s = make_app('PdfBenchSynth', SYNTH, MD_SYNTH)
    await run_update(app_s, 'cold synth-300')
    print('md artifacts:', md_count(MD_SYNTH))
    await run_update(app_s, 'warm synth-300 rerun')

    print('--- edit 20 docs (content change -> new sha) ---')
    for i in range(20):
        write_pdf(SYNTH / ('doc_%05d.pdf' % i), 10000 + i, 8)
    await run_update(app_s, 'edit 20')
    print('md artifacts:', md_count(MD_SYNTH))

    print('--- add 20 new docs ---')
    for i in range(20):
        write_pdf(SYNTH / ('doc_9%03d.pdf' % i), 20000 + i, 8)
    await run_update(app_s, 'add 20')
    print('md artifacts:', md_count(MD_SYNTH))

    print('--- delete 10 docs ---')
    for i in range(10):
        (SYNTH / ('doc_%05d.pdf' % i)).unlink(missing_ok=True)
    await run_update(app_s, 'delete 10')
    print('md artifacts:', md_count(MD_SYNTH))

    print()
    baseline(SYNTH)
    db = HERE / 'cocoindex.db'
    if db.exists():
        print('LMDB state size: %.1f MB' % (db.stat().st_size / 1e6))
    print('total md artifacts: synth=%d real=%d' % (md_count(MD_SYNTH), md_count(MD_REAL)))


asyncio.run(main())

import pathlib, urllib.request
IDS = ['1706.03762','1810.04805','2005.14165','2303.08774','2103.00020','1406.2661',
       '1503.03835','1606.05925','1908.08662','2005.11401','2201.11903','2106.09685',
       '1910.09700','2010.02544','2110.09520','2212.04413','1705.10535','1602.07360',
       '1704.03977','1907.06947']
out = pathlib.Path('pdf_real'); out.mkdir(exist_ok=True)
k = 0
for aid in IDS:
    try:
        req = urllib.request.Request('https://arxiv.org/pdf/' + aid,
              headers={'User-Agent': 'cocoindex-bench/1.0 (contact: local test)'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        if data[:5] == b'%PDF-':
            (out / ('real_%03d.pdf' % k)).write_bytes(data); k += 1
    except Exception as e:
        print('skip', aid, type(e).__name__)
print('fetched', k, 'real pdfs')

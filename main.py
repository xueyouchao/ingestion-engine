"""CocoIndex entry point.

Run it with the CocoIndex CLI:

    cocoindex update main.py

See README.md for the FINEPDFS_* environment variables that configure a run.
"""

import cocoindex as coco

from finepdfs.infrastructure.cocoindex.flow import build_app

app = build_app()

if __name__ == "__main__":
    with coco.runtime():
        app.update_blocking(report_to_stdout=True)

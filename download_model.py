"""One-time download of the multilingual sentiment model (~1.1 GB, free, runs on CPU).

Usage: python download_model.py
After this, the collector automatically uses it instead of the basic VADER lexicon.
"""
import os
import sys

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from app.sentiment import TransformersBackend  # noqa: E402

if __name__ == "__main__":
    print(f"Downloading {TransformersBackend.MODEL} ... (this can take several minutes)")
    try:
        b = TransformersBackend(allow_download=True)
    except Exception as e:
        print(f"ERROR: {e.__class__.__name__}: {e}")
        sys.exit(1)
    for t in ["Acme Trading denied my payout, total scam", "Got paid by Acme Trading today, great service",
              "Acme Trading es una estafa", "Acme Trading pagó rápido, muy recomendado"]:
        print(f"  {b.analyze(t)}  <- {t}")
    print("SUCCESS: model ready. The collector will use it from now on.")

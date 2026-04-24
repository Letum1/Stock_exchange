import os
import sys

PAPER_TRADING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper-trading")

if PAPER_TRADING_DIR not in sys.path:
    sys.path.insert(0, PAPER_TRADING_DIR)

os.chdir(PAPER_TRADING_DIR)

from app import app  # noqa: E402

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

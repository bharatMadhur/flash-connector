import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_PATH = os.path.join(ROOT, "api")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if API_PATH not in sys.path:
    sys.path.insert(0, API_PATH)

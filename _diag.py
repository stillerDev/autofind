import sys
import time


def step(name, fn):
    print("start", name, flush=True)
    started = time.time()
    try:
        result = fn()
        print("ok", name, round(time.time() - started, 2), repr(result)[:200], flush=True)
    except Exception as exc:
        print("ERR", name, repr(exc), flush=True)


import core.ocr.engine as e

step("import numpy", lambda: __import__("numpy"))
step("import cv2", lambda: __import__("cv2"))
step("import onnxruntime", lambda: __import__("onnxruntime"))
step("import shapely.geometry", lambda: __import__("shapely.geometry"))
step("import pyclipper", lambda: __import__("pyclipper"))
step("import rapidocr_onnxruntime", lambda: __import__("rapidocr_onnxruntime"))

step("windows_language_tags", e.windows_language_tags)
step("WindowsOcr.available", lambda: e.WindowsOcr().available())
step("RapidOcrMulti.available", lambda: e.RapidOcrMulti("multi").available())
step("RapidOcr.available", lambda: e.RapidOcr().available())
step("TesseractCli.available", lambda: e.TesseractCli().available())
step("PyTesseract.available", lambda: e.PyTesseractOcr().available())
step("downloaded_models", e.downloaded_models)
step("cjk_coverage", e.cjk_coverage)
step("engine_report", e.engine_report)
print("done", flush=True)

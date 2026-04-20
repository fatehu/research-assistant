from __future__ import annotations

from typing import Dict, Iterable, List, Set


ALLOWED_PACKAGES: Set[str] = {
    "numpy", "pandas", "scipy", "statsmodels",
    "matplotlib", "seaborn", "plotly", "bokeh", "altair", "pygal",
    "scikit-learn", "sklearn", "xgboost", "lightgbm", "catboost",
    "joblib",
    "torch", "torchvision", "torchaudio", "tensorflow", "keras",
    "transformers", "datasets", "accelerate",
    "nltk", "spacy", "gensim", "jieba", "snownlp",
    "pillow", "opencv-python", "opencv-python-headless", "imageio",
    "requests", "httpx", "aiohttp", "urllib3",
    "beautifulsoup4", "bs4", "lxml", "html5lib", "cssselect",
    "pyquery", "parsel",
    "openpyxl", "xlrd", "xlwt", "python-docx", "PyPDF2", "pdfplumber",
    "python-pptx", "csvkit",
    "sqlalchemy", "pymysql", "psycopg2-binary", "redis", "pymongo",
    "tqdm", "loguru", "rich", "typer", "click",
    "pydantic", "python-dotenv", "python-dateutil", "pytz",
    "sympy", "networkx", "igraph",
    "faker", "arrow", "pendulum", "humanize",
    "tabulate", "prettytable", "colorama",
}

SANDBOX_FORBIDDEN_IMPORT_ROOTS: Set[str] = {
    "os",
    "subprocess",
    "socket",
    "pathlib",
    "shutil",
    "tempfile",
    "requests",
    "httpx",
    "urllib",
    "aiohttp",
    "ftplib",
    "telnetlib",
    "paramiko",
    "multiprocessing",
    "threading",
    "ctypes",
    "importlib",
    "pickle",
    "marshal",
}

_PACKAGE_IMPORT_ROOT_OVERRIDES: Dict[str, Set[str]] = {
    "scikit-learn": {"sklearn"},
    "opencv-python": {"cv2"},
    "opencv-python-headless": {"cv2"},
    "pillow": {"PIL"},
    "beautifulsoup4": {"bs4"},
    "python-docx": {"docx"},
    "python-pptx": {"pptx"},
    "python-dateutil": {"dateutil"},
    "python-dotenv": {"dotenv"},
    "psycopg2-binary": {"psycopg2"},
}


def _normalized_import_root(package_name: str) -> Set[str]:
    package = str(package_name or "").strip()
    if not package:
        return set()
    override = _PACKAGE_IMPORT_ROOT_OVERRIDES.get(package)
    if override:
        return {str(item).strip() for item in override if str(item or "").strip()}
    normalized = package.replace("-", "_")
    return {normalized.split(".")[0]}


def _build_allowed_import_roots(packages: Iterable[str]) -> Set[str]:
    roots: Set[str] = {
        "math",
        "random",
        "statistics",
        "datetime",
        "time",
        "warnings",
        "json",
        "re",
        "collections",
        "itertools",
        "functools",
        "typing",
        "decimal",
        "fractions",
        "csv",
    }
    for package in packages:
        roots.update(_normalized_import_root(str(package or "").strip()))
    roots.difference_update(SANDBOX_FORBIDDEN_IMPORT_ROOTS)
    return {item for item in roots if item}


SANDBOX_ALLOWED_IMPORT_ROOTS: Set[str] = _build_allowed_import_roots(ALLOWED_PACKAGES)


def extract_sandbox_blocked_imports(source: str) -> List[str]:
    blocked: List[str] = []
    for line in str(source or "").splitlines():
        stripped = line.strip()
        module_name = ""
        if stripped.startswith("import "):
            module_name = stripped[len("import ") :].split(",")[0].strip().split()[0]
        elif stripped.startswith("from "):
            module_name = stripped[len("from ") :].split()[0].strip()
        if not module_name:
            continue
        root = module_name.split(".")[0]
        if root in SANDBOX_FORBIDDEN_IMPORT_ROOTS and root not in blocked:
            blocked.append(root)
    return blocked

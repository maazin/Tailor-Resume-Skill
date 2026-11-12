"""Shared fixtures. The sample person, employers and posting are made up."""

import copy
import json
import shutil
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures"
SCRIPTS = TESTS.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

HAS_PDFLATEX = shutil.which("pdflatex") is not None
HAS_PDFTOTEXT = shutil.which("pdftotext") is not None
needs_tex = pytest.mark.skipif(not HAS_PDFLATEX, reason="pdflatex not installed")
needs_pdf_tools = pytest.mark.skipif(
    not (HAS_PDFLATEX and HAS_PDFTOTEXT), reason="pdflatex and pdftotext are both needed"
)


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def master_tex() -> str:
    return (FIXTURES / "sample_master.tex").read_text(encoding="utf-8")


@pytest.fixture()
def master(master_tex):
    from latex_io import read_tex_string

    return read_tex_string(master_tex)


@pytest.fixture()
def tailored():
    return load("sample_tailored.json")


@pytest.fixture()
def job():
    return load("sample_job.json")


@pytest.fixture()
def resume(tailored, master):
    from latex_io import merge_tailored

    return merge_tailored(tailored, master)


def rules(findings, *severities) -> set[str]:
    return {f.rule for f in findings if not severities or f.severity in severities}


def _walk(obj, path: str):
    parts = path.split(".")
    for part in parts[:-1]:
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    last = parts[-1]
    return obj, (int(last) if isinstance(obj, list) else last)


def apply_edits(tailored: dict, job: dict, edits: list[dict]) -> tuple[dict, dict]:
    """Apply a planted-violation case to copies of the passing sample."""
    docs = {"tailored": copy.deepcopy(tailored), "job": copy.deepcopy(job)}
    for edit in edits:
        doc = docs[edit["file"]]
        op = edit["op"]
        if op == "replace_everywhere":
            text = json.dumps(doc)
            assert edit["find"] in text
            docs[edit["file"]] = json.loads(text.replace(edit["find"], edit["value"]))
            continue
        if op == "truncate_all":
            for kind in ("workExperience", "personalProjects"):
                for entry in doc[kind]:
                    entry["description"] = entry["description"][: edit["value"]]
            continue
        parent, key = _walk(doc, edit["path"])
        if op == "set":
            parent[key] = edit["value"]
        elif op == "replace":
            assert edit["find"] in parent[key], (edit["find"], parent[key])
            parent[key] = parent[key].replace(edit["find"], edit["value"])
        elif op == "delete":
            del parent[key]
        elif op == "append":
            parent[key].append(edit["value"])
        else:
            raise ValueError(op)
    return docs["tailored"], docs["job"]

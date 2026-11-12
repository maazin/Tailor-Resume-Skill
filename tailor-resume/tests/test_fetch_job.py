"""Job fetching: link parsing, the three API shapes, and failing fast when blocked."""

import time
import urllib.error

import pytest
from conftest import load

import fetch_job as fj


@pytest.mark.parametrize(
    "url, source, api",
    [
        ("https://boards.greenhouse.io/exampleco/jobs/4000001", "greenhouse",
         "https://boards-api.greenhouse.io/v1/boards/exampleco/jobs/4000001?pay_transparency=true"),
        ("https://job-boards.greenhouse.io/exampleco/jobs/4000001?gh_src=x", "greenhouse",
         "https://boards-api.greenhouse.io/v1/boards/exampleco/jobs/4000001?pay_transparency=true"),
        ("https://jobs.lever.co/exampleco/11111111-2222-3333-4444-555555555555/apply", "lever",
         "https://api.lever.co/v0/postings/exampleco/11111111-2222-3333-4444-555555555555"),
        ("https://jobs.eu.lever.co/exampleco/abc", "lever", "https://api.eu.lever.co/v0/postings/exampleco/abc"),
        ("https://jobs.ashbyhq.com/exampleco/aaaaaaaa-bbbb-cccc-dddd-000000000001", "ashby",
         "https://api.ashbyhq.com/posting-api/job-board/exampleco?includeCompensation=true"),
    ],
)
def test_parse_job_url(url, source, api):
    target = fj.parse_job_url(url)
    assert (target["source"], target["api"]) == (source, api)


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Austin/Data-Engineer_R123",
        "https://www.linkedin.com/jobs/view/123456",
        "https://careers.example.com/jobs?gh_jid=4000001",
        "https://boards.greenhouse.io/exampleco",
    ],
)
def test_other_links_ask_for_a_paste(url):
    with pytest.raises(fj.UnsupportedLink) as excinfo:
        fj.parse_job_url(url)
    assert "paste" in str(excinfo.value).lower()


def test_greenhouse_fixture():
    raw = load("greenhouse_job.json")
    job = fj.from_greenhouse(raw, raw["absolute_url"], "exampleco")
    assert fj.validate_job(job) == []
    assert job["title"] == "Data Engineer II - Req 4471"
    assert job["locations"] == ["Austin, TX"]
    assert job["pay"] == {"min": 120000.0, "max": 150000.0, "currency": "USD", "interval": "year"}
    text = job["description_text"]
    assert "<" not in text and "&amp;" not in text and "&lt;" not in text  # unescaped once, tags stripped
    assert "- Experience with Airflow or a similar scheduler" in text
    assert "employer & welcomes" in text


def test_lever_fixture_includes_lists():
    raw = load("lever_posting.json")
    job = fj.from_lever(raw, raw["hostedUrl"], "exampleco")
    assert fj.validate_job(job) == []
    assert job["title"] == "Backend Engineer"
    assert job["locations"] == ["Denver, CO", "Chicago, IL"]
    assert job["workplace_type"] == "hybrid"
    text = job["description_text"]
    for heading in ("What you will do", "What we require", "Nice to have"):
        assert heading in text
    assert "- Experience with PostgreSQL" in text  # only in "lists", not in descriptionPlain
    assert "Experience with PostgreSQL" not in raw["descriptionPlain"]
    assert text.rstrip().endswith("equal opportunity employer.")


def test_ashby_fixture_picks_the_job_by_id():
    raw = load("ashby_board.json")
    job_id = "aaaaaaaa-bbbb-cccc-dddd-000000000001"
    job = fj.from_ashby(raw, job_id, f"https://jobs.ashbyhq.com/exampleco/{job_id}", "exampleco")
    assert fj.validate_job(job) == []
    assert job["title"] == "Platform Engineer"
    assert job["locations"] == ["Seattle, WA", "Portland, OR"]
    assert job["pay"]["min"] == 120000 and job["pay"]["interval"] == "year"
    remote = fj.from_ashby(raw, "aaaaaaaa-bbbb-cccc-dddd-000000000002", "u", "exampleco")
    assert remote["workplace_type"] == "remote" and fj.validate_job(remote) == []
    with pytest.raises(fj.FetchError):
        fj.from_ashby(raw, "not-there", "u", "exampleco")


def test_every_source_writes_the_same_shape():
    shape = set(fj.empty_job("pasted"))
    gh = load("greenhouse_job.json")
    lv = load("lever_posting.json")
    jobs = [
        fj.from_greenhouse(gh, "u", "exampleco"),
        fj.from_lever(lv, "u", "exampleco"),
        fj.from_ashby(load("ashby_board.json"), "aaaaaaaa-bbbb-cccc-dddd-000000000001", "u", "exampleco"),
        fj.pasted_job("x" * 300, "Data Engineer", "Acme", ["Austin, TX"]),
    ]
    assert all(set(j) == shape for j in jobs)


def test_pasted_posting(tmp_path):
    posting = tmp_path / "posting.txt"
    posting.write_text("Data Engineer\n\nWe need Python.\n" + "More text. " * 30, encoding="utf-8")
    out = tmp_path / "job.json"
    code = fj.main(["--paste", str(posting), "--title", "Data Engineer", "--company", "Acme",
                    "--location", "Austin, TX", "-o", str(out)])
    assert code == 0
    import json
    job = json.loads(out.read_text())
    assert job["source"] == "pasted" and job["locations"] == ["Austin, TX"] and job["requirements"] == {}


def test_blocked_connection_fails_fast_with_a_clear_message(monkeypatch):
    def refuse(*args, **kwargs):
        raise urllib.error.URLError("Tunnel connection failed: 403 Forbidden")

    monkeypatch.setattr(fj.urllib.request, "urlopen", refuse)
    start = time.monotonic()
    with pytest.raises(fj.FetchError) as excinfo:
        fj.fetch_job("https://boards.greenhouse.io/exampleco/jobs/4000001")
    assert time.monotonic() - start < 10
    message = str(excinfo.value)
    assert "boards-api.greenhouse.io" in message and "Settings > Capabilities" in message and "Paste" in message


def test_hanging_connection_is_cut_off(monkeypatch):
    def hang(*args, **kwargs):
        time.sleep(30)

    monkeypatch.setattr(fj.urllib.request, "urlopen", hang)
    start = time.monotonic()
    with pytest.raises(fj.FetchError) as excinfo:
        fj.http_get_json("https://api.lever.co/v0/postings/exampleco/abc", timeout=1)
    assert time.monotonic() - start < 10
    assert "blocked" in str(excinfo.value)


def test_default_timeout_keeps_the_whole_fetch_under_ten_seconds():
    assert fj.TIMEOUT_SECONDS + 1 < 10


def test_only_job_board_hosts_are_called():
    with pytest.raises(fj.FetchError):
        fj.http_get_json("https://example.com/whatever")


def test_cli_exit_codes(monkeypatch, tmp_path, capsys):
    assert fj.main(["https://www.linkedin.com/jobs/view/1", "-o", str(tmp_path / "j.json")]) == 2

    def refuse(*args, **kwargs):
        raise urllib.error.URLError("blocked")

    monkeypatch.setattr(fj.urllib.request, "urlopen", refuse)
    assert fj.main(["https://jobs.lever.co/exampleco/abc", "-o", str(tmp_path / "j.json")]) == 3
    assert not (tmp_path / "j.json").exists()

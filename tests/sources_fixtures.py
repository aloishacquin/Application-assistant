"""Fake job board responses (shapes checked against the real public APIs)."""

import html
import json

import httpx

from tests.conftest import offer_text

DESCRIPTION = offer_text("02_tech_no_salary")  # > 200 chars, a real-looking offer
HTML_DESCRIPTION = "<h2>About us</h2><p>We build robots.</p><ul><li>Python</li><li>Docker</li></ul>"

GREENHOUSE = {
    "jobs": [
        {
            "id": 101,
            "title": "Software Engineer",
            "absolute_url": "https://example.com/jobs?gh_jid=101",
            "location": {"name": "Singapore"},
            "company_name": "Merlion Robotics",
            "content": html.escape(f"<p>{DESCRIPTION}</p>"),
        },
        {
            "id": 102,
            "title": "Senior Software Engineer",
            "absolute_url": "https://example.com/jobs?gh_jid=102",
            "location": {"name": "Singapore"},
            "company_name": "Merlion Robotics",
            "content": html.escape(f"<p>{DESCRIPTION} senior</p>"),
        },
        {
            "id": 103,
            "title": "Data Engineer",
            "absolute_url": "https://example.com/jobs?gh_jid=103",
            "location": {"name": "Dublin"},
            "company_name": "Merlion Robotics",
            "content": html.escape("<p>Dublin role</p>"),
        },
        {
            "id": 104,
            "title": "Account Executive",
            "absolute_url": "https://example.com/jobs?gh_jid=104",
            "location": {"name": "Singapore"},
            "company_name": "Merlion Robotics",
            "content": html.escape(f"<p>{DESCRIPTION} sales</p>"),
        },
    ],
    "meta": {"total": 4},
}

LEVER = [
    {
        "id": "lv-1",
        "text": "Backend Developer",
        "hostedUrl": "https://jobs.lever.co/acme/lv-1",
        "categories": {
            "location": "Singapore, Singapore",
            "allLocations": ["Singapore, Singapore", "Remote"],
        },
        "descriptionPlain": "Acme builds payment software. " * 5,
        "lists": [
            {
                "text": "What you'll do",
                "content": "<li>Build APIs in Python</li><li>Run Postgres</li>",
            }
        ],
        "additionalPlain": "Visa sponsorship available.",
    },
]

ASHBY = {
    "apiVersion": "1",
    "jobs": [
        {
            "id": "ab-1",
            "title": "Machine Learning Engineer",
            "jobUrl": "https://jobs.ashbyhq.com/kopi/ab-1",
            "location": "Remote (Asia)",
            "secondaryLocations": [{"location": "Singapore"}],
            "descriptionPlain": "Kopi trains models. " * 15,
            "isListed": True,
            "compensation": {"compensationTierSummary": "SGD 7K – 9K per month"},
        },
        {
            "id": "ab-2",
            "title": "Data Engineer",
            "jobUrl": "https://jobs.ashbyhq.com/kopi/ab-2",
            "location": "Singapore",
            "descriptionPlain": "Hidden. " * 40,
            "isListed": False,
        },
    ],
}

RSS = f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Jobs</title>
<item><title>Python Developer</title><link>https://feed.example/jobs/1</link><guid>job-1</guid>
<description>&lt;p&gt;{html.escape(DESCRIPTION)}&lt;/p&gt;</description></item>
<item><title>Data Analyst</title><link>https://feed.example/jobs/2</link><guid>job-2</guid>
<description>Short.</description></item>
</channel></rss>"""

ATOM = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Jobs</title>
<entry><id>urn:job:9</id><title>Platform Engineer</title><link href="https://atom.example/9"/>
<summary>{html.escape(DESCRIPTION)}</summary></entry></feed>"""

ROUTES = {
    "boards-api.greenhouse.io": lambda: httpx.Response(200, json=GREENHOUSE),
    "api.lever.co": lambda: httpx.Response(200, json=LEVER),
    "api.ashbyhq.com": lambda: httpx.Response(200, json=ASHBY),
    "feed.example": lambda: httpx.Response(200, text=RSS),
    "atom.example": lambda: httpx.Response(200, text=ATOM),
}


def mock_client(overrides: dict | None = None, calls: list | None = None) -> httpx.Client:
    routes = {**ROUTES, **(overrides or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if request.url.host not in routes:
            raise AssertionError(f"unexpected request to {request.url}")
        return routes[request.url.host]()

    return httpx.Client(transport=httpx.MockTransport(handler))


SOURCES_YAML = json.dumps(
    {
        "filters": {
            "locations": ["Singapore"],
            "title_keywords": ["engineer", "developer"],
            "title_excludes": ["senior"],
        },
        "budget": {"daily_usd": 1.0, "max_analyses_per_run": 10},
        "sources": [
            {"type": "greenhouse", "name": "Merlion", "board": "merlion"},
            {"type": "lever", "name": "Acme", "company": "acme"},
        ],
    }
)

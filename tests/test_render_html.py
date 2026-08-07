"""HTML renderer: markup structure, and the escaping traps that only show up visually."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline.config import Settings, get_settings
from pipeline.models import Resume
from pipeline.render.html import render_html

FIXTURE = Path(__file__).parent / "fixtures" / "resume.sample.json"


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def resume_en() -> Resume:
    return Resume.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def resume_zh(resume_en: Resume) -> Resume:
    data = resume_en.model_dump(mode="json", by_alias=True)
    data["lang"] = "zh"
    data["profile"]["name"] = "吳雨諠"
    data["education"][0].update(institution="國立陽明交通大學", degree="碩士", field="語言學")
    data["experiences"][0].update(organization="國泰金控 DDT AI", role="ML/AI 工程師實習生")
    return Resume.model_validate(data)


def body_of(html: str) -> str:
    """Everything after </head>, so CSS text can't satisfy a content assertion.

    Learned the hard way: probing the whole document for "EDUCATION" matched a comment
    in the stylesheet, not the heading.
    """
    return html.split("</head>", 1)[1]


# --- the stylesheet-escaping regression -------------------------------------
# Autoescape protects resume content, but applying it to the inlined stylesheet turns
# every `"` into `&#34;`. That breaks font stacks and every `content:` value — bullet
# markers vanish and the skill-label colon disappears. Invisible in the markup
# structure; only the rendered page shows it. Hence these three tests.


def test_stylesheet_quotes_survive(resume_en: Resume, settings: Settings) -> None:
    css = render_html(resume_en, settings).split("<style>")[1].split("</style>")[0]
    assert "&#34;" not in css and "&quot;" not in css
    assert '"Source Serif 4"' in css


def test_bullet_marker_content_intact(resume_en: Resume, settings: Settings) -> None:
    css = render_html(resume_en, settings).split("<style>")[1].split("</style>")[0]
    assert 'content: "▪"' in css


def test_skill_label_colon_intact(resume_en: Resume, settings: Settings) -> None:
    css = render_html(resume_en, settings).split("<style>")[1].split("</style>")[0]
    assert 'content: ":"' in css


def test_resume_content_is_still_escaped(resume_en: Resume, settings: Settings) -> None:
    """Marking the stylesheet safe must not disable escaping for content.

    The Cloud & Infrastructure label proves ampersands in data still get escaped.
    """
    body = body_of(render_html(resume_en, settings))
    assert "Cloud &amp; Infrastructure" in body
    assert "Cloud & Infrastructure" not in body


def test_html_injection_in_content_is_escaped(settings: Settings) -> None:
    """A Notion row containing markup must not become live HTML on the diff page."""
    data = Resume.model_validate_json(FIXTURE.read_text(encoding="utf-8")).model_dump(
        mode="json", by_alias=True
    )
    data["experiences"][0]["bullets"] = ["<script>alert(1)</script> shipped a feature"]
    body = body_of(render_html(Resume.model_validate(data), settings))
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


# --- structure ---------------------------------------------------------------


def test_sections_in_order(resume_en: Resume, settings: Settings) -> None:
    body = body_of(render_html(resume_en, settings))
    assert re.findall(r'class="section-title">([^<]+)<', body) == [
        "Education",
        "Experience",
        "Projects",
        "Skills",
    ]


def test_empty_sections_omitted(resume_en: Resume, settings: Settings) -> None:
    """The fixture has no publications; the heading must not render bare."""
    assert "Publications" not in body_of(render_html(resume_en, settings))


def test_ongoing_role_first(resume_en: Resume, settings: Settings) -> None:
    body = body_of(render_html(resume_en, settings))
    titles = re.findall(r'class="item-title">([^<]+)<', body)
    experience_titles = titles[2:5]  # after the two education entries
    assert experience_titles[0] == "Cathay Financial Holdings — DDT AI"
    assert "Feb 2026 – Present" in body


def test_all_bullets_rendered(resume_en: Resume, settings: Settings) -> None:
    body = body_of(render_html(resume_en, settings))
    expected = sum(
        len(item.bullets)
        for item in (*resume_en.education, *resume_en.experiences, *resume_en.projects)
    )
    assert len(re.findall(r"<li>", body)) == expected


def test_skill_categories_grouped(resume_en: Resume, settings: Settings) -> None:
    body = body_of(render_html(resume_en, settings))
    rows = re.findall(
        r'class="skill-label">([^<]+)<.*?class="skill-values">([^<]+)<', body, flags=re.S
    )
    assert rows[0] == ("Languages", "Mandarin (Native), English (Fluent)")
    assert ("Certificates", "TOEIC (Listening and Reading 985, Speaking 180, Writing 170)") in rows


def test_missing_contact_fields_skipped(resume_en: Resume, settings: Settings) -> None:
    """Only email is set; empty phone/LinkedIn must not leave stray separators."""
    body = body_of(render_html(resume_en, settings))
    contacts = re.findall(r'class="contact">(.*?)</div>', body, flags=re.S)
    assert len(re.findall(r"<span>", contacts[0])) == 1


# --- language switching ------------------------------------------------------


def test_zh_sets_lang_attribute(resume_zh: Resume, settings: Settings) -> None:
    """print.css hooks all its CJK rules onto body[lang="zh-Hant"]."""
    html = render_html(resume_zh, settings)
    assert '<body lang="zh-Hant"' in html
    assert '<html lang="zh-Hant"' in html


def test_zh_uses_chinese_labels(resume_zh: Resume, settings: Settings) -> None:
    body = body_of(render_html(resume_zh, settings))
    assert re.findall(r'class="section-title">([^<]+)<', body) == [
        "學歷",
        "工作經歷",
        "專案成果",
        "專業技能",
    ]


def test_zh_date_range_uses_wave_dash(resume_zh: Resume, settings: Settings) -> None:
    """CJK typography uses ～ rather than an en dash, which collides with glyphs."""
    body = body_of(render_html(resume_zh, settings))
    assert "2026 年 2 月 ～ 至今" in body


def test_zh_expected_graduation_marked(resume_zh: Resume, settings: Settings) -> None:
    assert "2028（預計）" in body_of(render_html(resume_zh, settings))


def test_undefined_field_raises(resume_en: Resume, settings: Settings) -> None:
    """StrictUndefined: a renamed model field must fail loudly, not render a blank.

    Otherwise a Notion property rename produces a resume that is silently missing a job
    title, and the diff shows it as an intentional deletion.
    """
    from jinja2 import StrictUndefined, Template, UndefinedError

    template = Template("{{ resume.no_such_field }}", undefined=StrictUndefined)
    with pytest.raises(UndefinedError):
        template.render(resume=resume_en)

"""Tests of the web administration panel (``grafeno --web``)."""

from __future__ import annotations

import asyncio
import json
from importlib import resources
from unittest.mock import MagicMock

import pytest

from grafeno import i18n
from grafeno import config as config_module
from grafeno.config import WEB_THEMES, ApiConfig
from grafeno.server import branding, ws
from grafeno.server import web
from grafeno.server.service import ServerService


# ---------------------------------------------------------------------- #
# Launch settings: loopback by default, token when exposed
# ---------------------------------------------------------------------- #
def test_prepare_defaults_to_loopback_without_token(monkeypatch):
    monkeypatch.delenv("GRAFENO_API_TOKEN", raising=False)
    saved = ApiConfig()
    effective, launch = web.prepare(saved)
    assert effective.enabled is True
    assert effective.host == "127.0.0.1"
    assert effective.port == saved.port
    assert launch.token == ""
    assert effective.tokens == ""
    assert saved.enabled is False  # the saved config is never mutated


def test_prepare_wildcard_generates_ephemeral_token(monkeypatch):
    monkeypatch.delenv("GRAFENO_API_TOKEN", raising=False)
    saved = ApiConfig()
    effective, launch = web.prepare(saved, "0.0.0.0", 9100)
    assert effective.host == "0.0.0.0"
    assert effective.port == 9100
    assert len(launch.token) >= 24
    assert effective.resolve_tokens() == {launch.token}
    assert saved.tokens == ""


def test_prepare_exposed_keeps_configured_tokens(monkeypatch):
    monkeypatch.delenv("GRAFENO_API_TOKEN", raising=False)
    effective, launch = web.prepare(ApiConfig(tokens="mine"), "0.0.0.0")
    assert launch.token == ""
    assert effective.resolve_tokens() == {"mine"}


def test_prepare_exposed_keeps_env_token(monkeypatch):
    monkeypatch.setenv("GRAFENO_API_TOKEN", "from-env")
    effective, launch = web.prepare(ApiConfig(), "0.0.0.0")
    assert launch.token == ""
    assert effective.resolve_tokens() == {"from-env"}


@pytest.mark.parametrize(
    ("host", "expected"),
    [("127.0.0.1", True), ("localhost", True), ("::1", True), ("0.0.0.0", False),
     ("192.168.1.10", False), ("myhost", False)],
)
def test_is_loopback(host, expected):
    assert web.is_loopback(host) is expected


def test_panel_urls(monkeypatch):
    monkeypatch.setattr(web, "_lan_address", lambda: "10.0.0.5")
    assert web.panel_urls("127.0.0.1", 8735) == ["http://127.0.0.1:8735/"]
    assert web.panel_urls("::1", 8735) == ["http://[::1]:8735/"]
    assert web.panel_urls("0.0.0.0", 80, "a b") == [
        "http://127.0.0.1:80/?token=a%20b",
        "http://10.0.0.5:80/?token=a%20b",
    ]


# ---------------------------------------------------------------------- #
# Page rendering
# ---------------------------------------------------------------------- #
def test_render_page_injects_translated_strings():
    html = web.render_page()
    assert "__GRAFENO_I18N__" not in html
    assert "__GRAFENO_LANG__" not in html
    assert "__GRAFENO_VERSION__" not in html
    assert '"web.nav.new": "New task"' in html
    assert '"nt.repeat": "Repetitive task"' in html  # TUI texts are reused
    assert '<html lang="en" data-theme="auto">' in html
    i18n.set_language("es")
    html = web.render_page()
    assert '"web.nav.new": "Nueva tarea"' in html
    assert '<html lang="es" data-theme="auto">' in html


def test_render_page_escapes_script_breakers(monkeypatch):
    monkeypatch.setitem(i18n._MESSAGES["en"], "web.ui.evil", "</script><!--")
    html = web.render_page()
    assert "</script><!--" not in html
    assert "\\u003c/script>\\u003c!--" in html


def test_page_strings_exist_in_both_languages():
    """Every key the panel script uses is translated in en and es."""
    import re
    from importlib import resources

    script = resources.files("grafeno.server").joinpath("static/app.js").read_text(encoding="utf-8")
    assert "options.session_cli_default" in script and '["claude", "opencode"]' not in script
    keys = set(re.findall(r'tr\("([a-z_.]+)"', script))
    assert keys
    for key in keys:
        if key.endswith("."):
            continue  # dynamic suffix (state., phase.)
        assert key.startswith(web.UI_PREFIXES), key
        assert key in i18n._MESSAGES["en"], key
        assert key in i18n._MESSAGES["es"], key


def test_new_task_link_immediately_follows_tasks_in_navigation():
    """New task stays visually grouped with Tasks in the header."""
    import re
    from importlib import resources

    html = resources.files("grafeno.server").joinpath("static/index.html").read_text(encoding="utf-8")
    nav = html.split('<nav id="nav">', 1)[1].split("</nav>", 1)[0]
    routes = re.findall(r'data-route="([^"]+)"', nav)
    assert routes == ["tasks", "new", "projects", "sessions", "reports", "settings"]


def test_cli_model_totals_are_in_reports_not_task_footer():
    """CLI/model token totals live in Reports, not in the web task-list footer."""
    script = resources.files("grafeno.server").joinpath("static/app.js").read_text(encoding="utf-8")
    assert '"tasks.tokens.summary"' not in script
    assert "task.tokens_by_agent" not in script
    assert 'tr("tasks.time.summary",' in script
    assert 'tr("web.ui.count",' in script
    assert "d.tokens_by_agent" in script
    assert 'section(tr("reports.section.by_model")' in script
    assert "data.by_model.map" in script


def test_task_column_defaults_to_60vw_and_keeps_the_resized_width():
    """The task column starts at 60% of the viewport and its resized width persists."""
    import re
    from importlib import resources

    static = resources.files("grafeno.server").joinpath("static")
    css = static.joinpath("app.css").read_text(encoding="utf-8")
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    rule = re.search(r"#task-table th\.name\s*\{([^}]*)\}", css)
    assert rule, "missing the #task-table th.name rule"
    declarations = dict(
        (name.strip(), value.strip())
        for name, _, value in (part.partition(":") for part in rule.group(1).split(";"))
        if value.strip()
    )
    # min-width too: with width alone the automatic table layout shrinks the column
    assert declarations.get("width") == "60vw"
    assert declarations.get("min-width") == "60vw"
    assert ".col-resizer" in css
    assert 'TASK_COL_KEY = "grafeno.taskColWidth"' in script
    assert "localStorage.getItem(TASK_COL_KEY)" in script
    assert "localStorage.setItem(TASK_COL_KEY" in script
    assert "th.style.minWidth" in script


def test_task_table_has_lateral_padding():
    """The task table sits in its own padded container inside the scroll area."""
    from importlib import resources

    static = resources.files("grafeno.server").joinpath("static")
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    css = static.joinpath("app.css").read_text(encoding="utf-8")
    assert 'h("div", { class: "task-table-container" }, table)' in script
    assert ".task-table-container { width: max-content; min-width: 100%; padding: 0 16px; }" in css
    # The scroll area and the 60vw task column are untouched.
    assert ".table-wrap { flex: 1; overflow: auto; }" in css
    assert "#task-table th.name { width: 60vw; min-width: 60vw; }" in css


def test_projects_tab_and_preset_new_task_route():
    """The panel has a Projects tab that opens the new-task form with the folder preset."""
    from importlib import resources

    static = resources.files("grafeno.server").joinpath("static")
    html = static.joinpath("index.html").read_text(encoding="utf-8")
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    assert 'href="#/projects" data-route="projects" id="nav-projects"' in html
    assert "projects: projectsView" in script
    assert '"#/new/" + enc(item.workdir)' in script
    assert 'api("GET", "/api/v1/projects")' in script
    assert "build(options, tasks, preset)" in script


def test_projects_favorites_sorting_and_last_worked():
    """Projects: server-side favorites group above the root groups, sortable headers, Last worked."""
    static = resources.files("grafeno.server").joinpath("static")
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    css = static.joinpath("app.css").read_text(encoding="utf-8")
    for fragment in (
        'api("POST", "/api/v1/projects/favorite"', "function compareProjects(", "aria-sort", "aria-pressed",
        "item.last_worked", 'tr("projects.group.favorites")', '"root favorites"',
        "item.workspace === root.path", 'sortHeader("last_worked"',
    ):
        assert fragment in script
    # Favorites live on the server, not in the browser.
    assert "projectFavorites" not in script
    for fragment in (".project-favorite", "#project-table td.when", "th button.sort"):
        assert fragment in css
    for language in ("en", "es"):
        for key in (
            "projects.col.favorite", "projects.col.last_worked", "projects.group.favorites",
            "projects.favorite.add", "projects.favorite.remove", "projects.sort.by",
        ):
            assert key in i18n._MESSAGES[language]


def test_project_actions_have_corner_close_button():
    """Project actions replace Cancel with an accessible corner close button."""
    from importlib import resources

    static = resources.files("grafeno.server").joinpath("static")
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    css = static.joinpath("app.css").read_text(encoding="utf-8")
    choose = script.split("  async choose(item) {", 1)[1].split("  async createProject() {", 1)[0]
    for fragment in (
        "project-actions-header", "project-actions-close", "project-actions-buttons", 'text: "×"',
        'tr("projects.actions.title", { name: item.name })', "text: item.workdir",
        'tr("web.editor.open")', 'tr("cs.open")', 'tr("projects.action.new_task")',
        'close("sessions")', 'close("new")',
    ):
        assert fragment in choose
    assert choose.count('tr("common.close")') == 2
    assert 'tr("common.cancel")' not in choose
    assert "dialog .project-actions-header" in css
    assert "dialog .project-actions-close" in css
    assert "justify-content: space-between" in css
    for language in ("en", "es"):
        assert "common.close" in i18n._MESSAGES[language]


def test_cli_sessions_tab_and_project_action():
    """The panel has a CLI sessions tab, reachable from the project actions."""
    from importlib import resources

    static = resources.files("grafeno.server").joinpath("static")
    html = static.joinpath("index.html").read_text(encoding="utf-8")
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    assert 'href="#/sessions" data-route="sessions" id="nav-sessions"' in html
    assert "sessions: sessionsView" in script
    assert '"#/sessions/" + enc(item.workdir)' in script
    assert 'api("GET", "/api/v1/sessions' in script
    assert 'topics: ["tasks", "sessions"]' in script
    assert "innerHTML" not in script
    # The composer is built once; session changes only toggle `disabled`.
    assert "updateComposer()" in script and "renderComposer" not in script


def test_new_cli_session_uses_searchable_project_selector():
    """Global session creation searches projects and keeps preset folders fixed."""
    from importlib import resources

    from grafeno import i18n

    static = resources.files("grafeno.server").joinpath("static")
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    css = static.joinpath("app.css").read_text(encoding="utf-8")
    method = script.split("  async newSession(preset) {", 1)[1].split("  async openChat() {", 1)[0]
    selector = script.split("function projectSelector(projects) {", 1)[1].split("// ---------------------------------------------------------------- list editors", 1)[0]
    for needle in (
        'api("GET", "/api/v1/projects")',
        "projectSelector(projectData.projects || [])",
        "folder.node",
        "const workdir = folder.value()",
        "{ workdir, name:",
    ):
        assert needle in method
    assert "dirSuggestions(folder)" not in method
    assert "searchSelect({" in selector
    assert 'className: "cs-project-selector"' in selector
    assert "detail: item.workdir" in selector
    assert 'h("select")' not in selector
    assert 'type: "search"' not in selector  # the search field lives inside searchSelect
    assert 'byName || a.workdir.toLowerCase().localeCompare(b.workdir.toLowerCase())' in selector
    assert "return { node: picker.node, value: () => picker.value().trim() };" in selector
    for needle in ('tr("cs.workdir.prompt")', 'tr("cs.error.select_workdir")'):
        assert needle in script
    for language in ("en", "es"):
        assert "cs.workdir.prompt" in i18n._MESSAGES[language]
        assert "cs.error.select_workdir" in i18n._MESSAGES[language]
    assert ".cs-project-selector { width: 100%; }" in css
    assert "grid-template-columns: minmax(160px, 0.4fr)" not in css


def test_new_cli_session_does_not_append_null_task_context():
    """Ordinary session creation omits the task hint instead of appending null."""
    from importlib import resources

    script = resources.files("grafeno.server").joinpath("static/app.js").read_text(encoding="utf-8")
    method = script.split("  async newSession(preset) {", 1)[1].split("  async openChat() {", 1)[0]
    assert '...(preset.task_id ? [h("p", { class: "help", text: tr("cs.context.hint", { name: preset.task_name }) })] : []),' in method
    assert 'preset.task_id ? h("p", { class: "help", text: tr("cs.context.hint", { name: preset.task_name }) }) : null' not in method


def test_cli_session_header_does_not_render_null():
    """The session header filters the optional Stop button instead of passing null to replaceChildren."""
    from importlib import resources

    script = resources.files("grafeno.server").joinpath("static/app.js").read_text(encoding="utf-8")
    method = script.split("  renderHead() {", 2)[2].split("  buildComposer() {", 1)[0]
    assert "this.headNode.replaceChildren(...[" in method
    assert "].filter(Boolean));" in method


def test_task_list_footer_does_not_render_null():
    """Without accumulated time the list footer omits the time line instead of rendering null."""
    from importlib import resources

    script = resources.files("grafeno.server").joinpath("static/app.js").read_text(encoding="utf-8")
    start = script.index("this.summary.replaceChildren(")
    block = script[start:script.index(";", start)]
    assert block.startswith("this.summary.replaceChildren(...[")
    assert "].filter(Boolean)" in block


def test_working_directories_use_the_list_editor():
    """Settings edit the working directories as a list with directory suggestions."""
    from importlib import resources

    script = resources.files("grafeno.server").joinpath("static/app.js").read_text(encoding="utf-8")
    assert 'type: "dir"' in script
    assert "function dirSuggestions(input)" in script
    assert 'workspaces.value.split(",")' not in script


def test_render_page_injects_branding():
    html = web.render_page()
    assert 'data-theme="auto"' in html
    assert "<title>Grafeno - web panel</title>" in html
    assert "/favicon.ico?v=0" in html
    cfg = config_module.load()
    cfg.web.instance_name = "<b>Lab</b>"
    cfg.web.theme = "gray"
    config_module.save(cfg)
    html = web.render_page()
    assert 'data-theme="gray"' in html
    assert "<title>&lt;b&gt;Lab&lt;/b&gt; - Grafeno - web panel</title>" in html
    assert "<b>Lab</b>" not in html
    assert "__GRAFENO_" not in html


def test_web_theme_strings_exist():
    for name in ("auto", *WEB_THEMES):
        assert f"web.theme.{name}" in i18n._MESSAGES["en"]
        assert f"web.theme.{name}" in i18n._MESSAGES["es"]


def test_theme_palettes_present_in_css():
    css = resources.files("grafeno.server").joinpath("static/app.css").read_text(encoding="utf-8")
    for name in ("auto", *WEB_THEMES):
        assert f':root[data-theme="{name}"]' in css
    assert css.count("prefers-color-scheme") == 1


def test_branding_hooks_present_in_js():
    script = resources.files("grafeno.server").joinpath("static/app.js").read_text(encoding="utf-8")
    for needle in ("function applyBranding", "function applyTheme", '"cfg-web"', "favicon_clear"):
        assert needle in script
    assert 'field(tr("web.cfg.favicon")' not in script  # nested <label> would toggle the remove checkbox


def test_server_serves_custom_favicon_and_instance_name():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    cfg = config_module.load()
    cfg.web.instance_name = "Lab"
    cfg.web.favicon = branding.store_favicon(".png", png)
    config_module.save(cfg)

    async def scenario():
        service = ServerService(
            ApiConfig(enabled=True, host="127.0.0.1", port=0, tokens="secret"),
            app=MagicMock(), web=web.WebLaunch(host="127.0.0.1", port=0, token="secret"),
        )
        task = asyncio.create_task(service.run())
        for _ in range(200):
            if service.server is not None:
                break
            await asyncio.sleep(0.01)
        try:
            status, headers, body = await _get(service.port, "/favicon.ico")
            assert status == 200 and headers["content-type"] == "image/png" and body == png
            status, _, body = await _get(service.port, "/api/v1/status?token=secret")
            assert status == 200 and json.loads(body)["instance_name"] == "Lab"
            assert (await ws._status_method(service, {}, None))["instance_name"] == "Lab"
        finally:
            service.stop()
            task.cancel()

    asyncio.run(scenario())


# ---------------------------------------------------------------------- #
# Served page: no auth for the page, auth for the data
# ---------------------------------------------------------------------- #
async def _get(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode("ascii"))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        lines = head.decode("iso-8859-1").strip().split("\r\n")
        headers = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        body = await asyncio.wait_for(reader.readexactly(length), timeout=5.0) if length else b""
    finally:
        writer.close()
        await writer.wait_closed()
    return int(lines[0].split(" ")[1]), headers, body


def test_server_serves_page_without_token_and_protects_api():
    async def scenario():
        app = MagicMock()
        launch = web.WebLaunch(host="127.0.0.1", port=0, token="secret")
        service = ServerService(
            ApiConfig(enabled=True, host="127.0.0.1", port=0, tokens="secret"),
            app=app, web=launch,
        )
        task = asyncio.create_task(service.run())
        for _ in range(200):
            if service.server is not None:
                break
            await asyncio.sleep(0.01)
        try:
            status, headers, body = await _get(service.port, "/")
            assert status == 200
            assert headers["content-type"].startswith("text/html")
            assert headers["referrer-policy"] == "no-referrer"
            assert b"GRAFENO" in body
            assert "script-src 'self'" in headers["content-security-policy"]
            status, headers, body = await _get(service.port, "/assets/app.js")
            assert status == 200
            assert headers["content-type"].startswith("text/javascript")
            assert b"grafeno-boot" in body
            status, headers, _ = await _get(service.port, "/assets/app.css")
            assert status == 200 and headers["content-type"].startswith("text/css")
            status, _, _ = await _get(service.port, "/assets/other.js")
            assert status == 401  # only the known assets bypass auth
            status, _, _ = await _get(service.port, "/favicon.ico")
            assert status == 204
            status, _, _ = await _get(service.port, "/api/v1/tasks")
            assert status == 401
            status, _, _ = await _get(service.port, "/api/v1/tasks?token=secret")
            assert status == 200
        finally:
            service.stop()
            task.cancel()
        messages = [call.args[0] for call in app.notify.call_args_list]
        assert f"Web panel: http://127.0.0.1:{service.port}/?token=secret" in messages

    asyncio.run(scenario())


# ---------------------------------------------------------------------- #
# Command line
# ---------------------------------------------------------------------- #
def _run_main(monkeypatch, argv: list[str]) -> MagicMock:
    from grafeno import app as app_module

    monkeypatch.delenv("GRAFENO_API_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["grafeno", "--noeditor", *argv])
    app_mock = MagicMock()
    monkeypatch.setattr(app_module, "GrafenoApp", app_mock)
    app_module.main()
    return app_mock


def test_main_without_web_keeps_saved_api(monkeypatch):
    app_mock = _run_main(monkeypatch, [])
    assert app_mock.call_args.kwargs == {"web": None, "api_config": None}


def test_main_web_defaults_to_localhost(monkeypatch):
    app_mock = _run_main(monkeypatch, ["--web"])
    kwargs = app_mock.call_args.kwargs
    assert kwargs["api_config"].enabled is True
    assert kwargs["api_config"].host == "127.0.0.1"
    assert kwargs["web"].token == ""


def test_main_web_host_implies_web(monkeypatch):
    app_mock = _run_main(monkeypatch, ["--web-host", "0.0.0.0", "--web-port", "9001"])
    kwargs = app_mock.call_args.kwargs
    assert kwargs["api_config"].host == "0.0.0.0"
    assert kwargs["api_config"].port == 9001
    assert kwargs["web"].token


def test_main_rejects_bad_web_port(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, ["--web-port", "70000"])
    assert exc.value.code == 2


# ---------------------------------------------------------------------- #
# --no-auth
# ---------------------------------------------------------------------- #
def test_prepare_no_auth_ignores_every_token(monkeypatch):
    monkeypatch.setenv("GRAFENO_API_TOKEN", "from-env")
    saved = ApiConfig(tokens="mine")
    effective, launch = web.prepare(saved, "0.0.0.0", no_auth=True)
    assert launch.no_auth is True
    assert launch.token == ""  # no temporary token either
    assert effective.resolve_tokens() == set()
    assert "auth_disabled" not in effective.to_dict()  # never persisted
    assert saved.resolve_tokens() == {"from-env", "mine"}  # saved config untouched


def test_main_no_auth_implies_web(monkeypatch):
    app_mock = _run_main(monkeypatch, ["--no-auth"])
    kwargs = app_mock.call_args.kwargs
    assert kwargs["api_config"].enabled is True
    assert kwargs["api_config"].host == "127.0.0.1"
    assert kwargs["api_config"].auth_disabled is True
    assert kwargs["web"].no_auth is True


def test_server_no_auth_accepts_requests_and_warns(monkeypatch):
    monkeypatch.setenv("GRAFENO_API_TOKEN", "from-env")

    async def scenario():
        app = MagicMock()
        api_cfg, launch = web.prepare(ApiConfig(port=0, tokens="secret"), "0.0.0.0", no_auth=True)
        service = ServerService(api_cfg, app=app, web=launch)
        task = asyncio.create_task(service.run())
        for _ in range(200):
            if service.server is not None:
                break
            await asyncio.sleep(0.01)
        try:
            status, _, _ = await _get(service.port, "/api/v1/tasks")
            assert status == 200
        finally:
            service.stop()
            task.cancel()
        calls = app.notify.call_args_list
        warning = next(call for call in calls if call.args[0] == i18n.t("web.no_auth_exposed"))
        assert warning.kwargs["severity"] == "error"
        assert not any("?token=" in call.args[0] for call in calls)

    asyncio.run(scenario())


def test_schedule_column_and_text_helpers_present():
    """The panel renders the derived schedule instead of the raw scheduled_at."""
    from importlib import resources

    script = resources.files("grafeno.server").joinpath("static/app.js").read_text(encoding="utf-8")
    assert "function scheduleText(" in script
    assert 'tr("tasks.col.scheduled")' in script
    assert "when(task.scheduled_at)" not in script
    css = resources.files("grafeno.server").joinpath("static/app.css").read_text(encoding="utf-8")
    assert "#task-table td.sched.overdue" in css  # must out-rank "#task-table td.when"


def test_cli_session_composer_can_send_with_enter():
    """The session composer has a remembered "send with Enter" option."""
    from importlib import resources

    static = resources.files("grafeno.server").joinpath("static")
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    css = static.joinpath("app.css").read_text(encoding="utf-8")
    assert 'CS_ENTER_KEY = "grafeno.csEnterSends"' in script
    assert "localStorage.getItem(CS_ENTER_KEY)" in script
    assert "localStorage.setItem(CS_ENTER_KEY" in script
    assert 'tr("cs.enter_sends")' in script
    assert "event.isComposing" in script  # IME composition never sends
    assert "event.shiftKey" in script     # Shift+Enter keeps inserting a new line
    assert ".cs-actions" in css


def test_cli_session_composer_keeps_send_next_to_prompt():
    """The Send button stays next to the prompt instead of the checkbox edge."""
    from importlib import resources

    css = resources.files("grafeno.server").joinpath("static/app.css").read_text(encoding="utf-8")
    compose = next(line for line in css.splitlines() if line.startswith(".cs-compose {"))
    actions = next(line for line in css.splitlines() if line.startswith(".cs-actions {"))
    assert "gap: 8px;" in compose
    assert "align-items: flex-start;" in actions
    assert "align-items: flex-end;" not in actions


def test_cli_session_composer_uploads_files_and_sends_their_names():
    """Session files are raw uploads to the session; the prompt never carries their contents."""
    from importlib import resources

    static = resources.files("grafeno.server").joinpath("static")
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    css = static.joinpath("app.css").read_text(encoding="utf-8")
    build = script.split("  buildComposer() {", 1)[1].split("  applyEnterMode() {", 1)[0]
    send = script.split("  async send() {", 1)[1].split("  async action(name) {", 1)[0]
    mobile = css.split("@media (max-width: 760px) {", 1)[1].split(".media-upload", 1)[0]

    assert 'this.files = attachmentsCollector([prompt], tr("cs.attachments_hint"))' in build
    assert "this.uploaded = new Map();" in build
    assert 'class: "cs-editor"' in build
    assert "this.sendButton" in build and "enter.node" in build
    # No base64 and no inlined file contents anywhere in the session flow.
    assert "function sessionPrompt" not in script
    assert "GRAFENO ATTACHMENT" not in script
    assert "CS_ATTACH_MAX_TOTAL" not in script
    assert "this.files.payload(" not in send
    assert 'this.files.uploadTo((file) => sessionPath(this.sessionId, "/attachments?name=" + enc(file.name)), pending)' in send
    assert 'await api("POST", sessionPath(this.sessionId, "/send"), { prompt: text, attachments })' in send
    assert "if (!text && !this.files.count())" in send
    assert send.index("uploadTo(") < send.index("await api(") < send.index('this.prompt.value = "";') < send.index("this.files.clear()")
    assert "this.sending = false; this.updateComposer();" in send
    for language in ("en", "es"):
        assert "cs.attachments_hint" in i18n._MESSAGES[language]
        assert "cs.attachments.sent" in i18n._MESSAGES[language]
    assert ".cs-editor" in css
    assert '.cs-editor input[type="file"]' in css
    assert ".cs-compose { flex-direction: column; }" in mobile
    assert ".cs-actions" in mobile and "flex-direction: row" in mobile
    assert "align-items: center" in mobile


def test_projects_tab_creates_projects():
    """The Projects tab creates folders through POST /api/v1/projects in every mode."""
    from importlib import resources

    from grafeno import projectsetup

    script = resources.files("grafeno.server").joinpath("static/app.js").read_text(encoding="utf-8")
    assert 'api("POST", "/api/v1/projects", payload)' in script
    assert 'tr("projects.create.button")' in script
    assert "data.create && data.create.available" in script
    css = resources.files("grafeno.server").joinpath("static/app.css").read_text(encoding="utf-8")
    assert "[hidden] { display: none !important; }" in css  # hidden .field rows must really hide
    for mode in projectsetup.MODES:
        assert f'"{mode}"' in script
        for lang in ("en", "es"):
            assert f"projects.create.mode.{mode}" in i18n._MESSAGES[lang], (lang, mode)


def test_editor_button_wiring_in_script():
    """Projects and tasks open the web editor; the tab is opened inside the click."""
    from importlib import resources

    script = resources.files("grafeno.server").joinpath("static", "app.js").read_text(encoding="utf-8")
    for needle in (
        '"/api/v1/editor/open"', 'tr("web.editor.open")', "openEditor({ workdir: item.workdir })",
        "openEditor({ task_id: task.id })", 'key: "open_url"', 'window.open("", "_blank")',
    ):
        assert needle in script


def test_task_session_button_wiring_in_script():
    """The task detail offers a CLI session whose first turn carries the task summary."""
    from importlib import resources

    script = resources.files("grafeno.server").joinpath("static", "app.js").read_text(encoding="utf-8")
    for needle in (
        "sessionButton()", 'tr("web.task.new_session")', "task_id: preset.task_id",
        "sessionsView.newSession({ workdir: task.workdir, task_id: task.id", 'tr("cs.context.pending")',
    ):
        assert needle in script


def _search_select_source():
    from importlib import resources

    script = resources.files("grafeno.server").joinpath("static/app.js").read_text(encoding="utf-8")
    body = script.split("function searchSelect(opts) {", 1)[1].split(
        "// ---------------------------------------------------------------- project selector", 1)[0]
    return script, body


def test_search_select_is_a_single_searchable_keyboard_control():
    """searchSelect: one trigger, a search panel, keyboard and mouse, closes on blur."""
    script, body = _search_select_source()
    assert "function foldText(text)" in script
    for needle in (
        'class: "ss-trigger"', 'role: "combobox"', 'role: "listbox"', 'role: "option"',
        '"aria-activedescendant"', '"aria-expanded"',
        '"ArrowDown"', '"ArrowUp"', '"Enter"', '"Escape"',
        "event.stopPropagation()", "searchSelectEscapeAt = Date.now()",
        'root.addEventListener("focusout"', "!root.contains(event.relatedTarget)",
        "onmousedown: (event) => event.preventDefault()",
        "node.remove()", "setItems(nextItems, value)",
        "shown.findIndex((item) => !item.empty)",
    ):
        assert needle in body, needle
    assert "innerHTML" not in body
    assert "<select" not in body and 'h("select"' not in body
    # Escape that closed a panel never closes the surrounding dialog.
    modal = script.split("function modal(options, build) {", 1)[1].split("function confirmDialog(", 1)[0]
    assert "Date.now() - searchSelectEscapeAt < 100" in modal


def test_task_filters_use_search_select_and_keep_the_filter_logic():
    """'All projects' and 'States' are searchSelect controls; visible() is unchanged."""
    script, _ = _search_select_source()
    build = script.split("const tasksView = {", 1)[1].split("  async refresh(quiet) {", 1)[0]
    assert "this.projectPicker = searchSelect({" in build
    assert "const statePicker = searchSelect({" in build
    assert 'emptyLabel: tr("web.ui.all_projects")' in build
    assert 'emptyLabel: tr("web.ui.any_state")' in build
    assert 'searchPlaceholder: tr("web.ui.filter_states")' in build
    assert "search, this.projectPicker.node, done, statePicker.node, date, today, reload" in build
    assert "this.projectSelect" not in script
    assert "this.projectPicker.setItems(" in script
    assert "if (f.project && projectOf(task) !== f.project && task.workdir !== f.project) return false;" in script
    assert "if (f.state && task.state !== f.state) return false;" in script


def test_search_select_arrow_keeps_padding_from_the_right_edge():
    """The down arrow is a themed CSS chevron 12px away from the right border."""
    from importlib import resources

    css = resources.files("grafeno.server").joinpath("static/app.css").read_text(encoding="utf-8")
    assert ".ss-trigger { position: relative;" in css
    assert "padding: 6px 32px 6px 10px" in css
    arrow = css.split(".ss-trigger::after {", 1)[1].split("}", 1)[0]
    assert "right: 12px" in arrow
    assert "var(--muted)" in arrow
    assert ".filters .ss { flex: 1 1 160px; }" in css
    # Direct children only: the panel search field must not inherit the 180px flex-basis.
    assert ".filters > input[type=search]" in css and ".filters input[type=search]" not in css
    assert ".ss-panel {" in css and "z-index: 30" in css


def test_search_select_strings_exist_in_both_languages():
    from grafeno import i18n

    for language in ("en", "es"):
        for key in ("web.ui.filter_states", "web.ui.no_matches"):
            assert i18n._MESSAGES[language][key], (language, key)


def test_roles_editor_effort_uses_model_levels_and_hint():
    static = resources.files("grafeno.server").joinpath("static")
    script = static.joinpath("app.js").read_text(encoding="utf-8")
    css = static.joinpath("app.css").read_text(encoding="utf-8")
    for needle in (
        "function modelLevels(",
        "modelLevels(cliVariants, current.model)",
        'tr("cfg.effort.none")',
        'tr("cfg.effort.pick_model")',
        "effortCell",
        "MODELS_TTL_MS",
    ):
        assert needle in script, needle
    assert ".effort-hint" in css


def test_model_levels_js_matches_python():
    import re
    import shutil
    import subprocess

    from grafeno.drivers import model_levels

    node = shutil.which("node") or pytest.skip("node not installed")
    script = resources.files("grafeno.server").joinpath("static/app.js").read_text(encoding="utf-8")
    fn = re.search(r"function modelLevels\(.*?\n}\n", script, re.S).group(0)
    cases = [
        ({"kimi-code/k3": ["low", "high"]}, "kimi-code/k3"),
        ({"kimi-code/k3": ["low", "high"]}, "k3"),
        ({"kimi-code/k3": ["low", "high"]}, "kimi-code/k3#x"),
        ({"a/m": ["x"], "b/m": ["y"]}, "m"),
        ({"kimi-code/k3": ["low"]}, ""),
        (None, "k3"),
    ]
    code = fn + "\nconsole.log(JSON.stringify(%s.map(([m, n]) => modelLevels(m, n))))" % json.dumps(
        [list(case) for case in cases]
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(out.stdout) == [model_levels(m, n) for m, n in cases]

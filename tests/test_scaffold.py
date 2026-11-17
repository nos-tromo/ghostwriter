"""Text checks of the deployment files: compose, Makefile, nginx, Vite and the env example."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _service(text: str, name: str) -> str:
    """Return one compose service's block.

    The block runs from the ``  <name>:`` line up to the next two-space service
    key, or up to the next top-level key, whichever comes first.

    Args:
        text: The compose file's text.
        name: The service name.

    Returns:
        str: The service's lines, joined as in the file.
    """
    lines = text.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.rstrip() == f"  {name}:")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^(  [A-Za-z0-9_-]+:\s*$|[A-Za-z0-9_-]+:)", lines[i]):
            end = i
            break
    return "".join(lines[start:end])


def _location(conf: str, spec: str) -> str:
    """Return one nginx ``location`` block, from its opening line to its closing brace.

    Args:
        conf: The nginx configuration's text.
        spec: What follows ``location`` on the opening line, e.g. ``= /api/transcribe``.

    Returns:
        str: The block's text.
    """
    start = conf.index(f"location {spec} {{")
    depth = 0
    for i in range(start, len(conf)):
        if conf[i] == "{":
            depth += 1
        elif conf[i] == "}":
            depth -= 1
            if depth == 0:
                return conf[start : i + 1]
    raise AssertionError(f"unclosed location {spec}")


def test_compose_matches_the_contract() -> None:
    """Project name, networks, aliases, volume and environment follow the shared contract."""
    compose = (ROOT / "docker" / "compose.yaml").read_text(encoding="utf-8")
    backend, frontend = _service(compose, "backend"), _service(compose, "frontend")
    assert "name: ghostwriter\n" in compose and "ports:" not in compose and "data-net" not in compose
    assert "      inference-net:\n        aliases:\n          - ghostwriter-backend" in backend
    assert "      workbench-net:\n        aliases:\n          - ghostwriter-backend" in backend
    assert "edge-net" not in backend and "inference-net" not in frontend and "workbench-net" not in frontend
    assert "      edge-net:\n        aliases:\n          - ghostwriter-frontend" in frontend
    assert "      - ghostwriter-data:/var/lib/ghostwriter" in backend
    assert "  ghostwriter-data:\n    external: true\n    name: ghostwriter-data" in compose
    assert "AUDIT_REQUIRE_PURPOSE: ${AUDIT_REQUIRE_PURPOSE-}" in backend and "AUDIT_OUTPUT: container" in backend
    assert "GHOSTWRITER_EDGE_SECRET: ${GHOSTWRITER_EDGE_SECRET:-}" in backend
    assert "WORKBENCH_API_KEY: ${WORKBENCH_API_KEY:-}" in backend
    assert "  workbench-net:\n    external: true\n    name: ${WORKBENCH_NET:-workbench-net}" in compose
    assert '"--factory", "ghostwriter.main:create_app"' in (ROOT / "docker" / "Dockerfile.backend").read_text()


def test_the_service_slice_stops_at_the_next_service() -> None:
    """The helper keeps the backend block apart from its neighbours."""
    compose = (ROOT / "docker" / "compose.yaml").read_text(encoding="utf-8")
    backend = _service(compose, "backend")
    assert backend.startswith("  backend:")
    assert "  frontend:" not in backend and "image: ghostwriter-frontend" not in backend


def test_every_service_is_hardened() -> None:
    """Both services run hardened and read-only; the ownership fix has no network and two capabilities."""
    compose = (ROOT / "docker" / "compose.yaml").read_text(encoding="utf-8")
    hardened = "x-hardened: &hardened\n  security_opt:\n    - no-new-privileges:true\n  cap_drop:\n    - ALL\n"
    assert hardened in compose
    for name in ("backend", "frontend"):
        service = _service(compose, name)
        assert "    <<: *hardened\n" in service and "    read_only: true\n" in service
        assert "      - /tmp:size=16m\n" in service
    fixer = _service(compose, "volume-permissions")
    assert "    <<: *hardened\n" in fixer and "security_opt" not in fixer and "cap_drop" not in fixer
    assert re.findall(r"^    cap_add:\n((?:      - \w+\n)+)", fixer, re.MULTILINE) == [
        "      - CHOWN\n      - DAC_OVERRIDE\n"
    ]
    assert "      - DAC_READ_SEARCH\n" not in fixer
    assert '    user: "0:0"\n' in fixer and "    network_mode: none\n" in fixer and "    read_only: true\n" in fixer
    assert "find /var/lib/ghostwriter -xdev ! -user 10001 -exec chown -h 10001:10001 {} +" in fixer
    backend = _service(compose, "backend")
    assert "      volume-permissions:\n        condition: service_completed_successfully" in backend


def test_make_knows_the_networks_and_the_volume() -> None:
    """The Makefile creates every external network and the volume, and includes the shared targets."""
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "REPO     := ghostwriter" in mk and "NETWORKS := inference-net edge-net workbench-net" in mk
    assert "VOLUMES  := ghostwriter-data" in mk and "include make/common.mk" in mk
    assert "up up-dev: volumes" in mk
    assert "python -m ghostwriter.retention --dry-run $(if $(CHAT_DAYS),--chat-days $(CHAT_DAYS))" in mk
    assert "docker volume rm ghostwriter-data" in mk


def test_nginx_serves_under_ghostwriter_streams_and_allows_the_microphone() -> None:
    """Nginx strips /ghostwriter, streams the chat, sizes uploads and allows the microphone."""
    conf = (ROOT / "frontend" / "nginx" / "default.conf").read_text(encoding="utf-8")
    assert "rewrite ^/ghostwriter(/.+)$ $1;" in conf and "return 302 /ghostwriter/;" in conf
    assert "proxy_max_temp_file_size 0;" in conf and "access_log /var/log/nginx/access.log scrubbed;" in conf
    stream = _location(conf, "~ ^/api/chats/[^/]+/(messages|regenerate)$")
    assert all(
        d in stream
        for d in ("proxy_buffering off;", "proxy_request_buffering off;", "proxy_read_timeout 1h;", "gzip off;")
    )
    assert "client_max_body_size 1m;" in stream
    attachments = _location(conf, "~ ^/api/chats/[^/]+/attachments$")
    assert all(
        d in attachments
        for d in ("client_max_body_size 51m;", "proxy_request_buffering off;", "proxy_read_timeout 10m;")
    )
    transcribe = _location(conf, "= /api/transcribe")
    assert "client_max_body_size 11m;" in transcribe and "proxy_request_buffering off;" in transcribe
    assert "proxy_read_timeout 3m;" in transcribe
    assert "proxy_buffering off;" in _location(conf, "~ ^/api/chats/[^/]+/files/")
    assert "client_max_body_size 1m;" in _location(conf, "/api/")
    for spec in (
        "~ ^/api/chats/[^/]+/(messages|regenerate)$",
        "~ ^/api/chats/[^/]+/attachments$",
        "= /api/transcribe",
        "~ ^/api/chats/[^/]+/files/",
        "/api/",
        "/assets/",
        "/",
    ):
        assert "include /etc/nginx/security-headers.conf;" in _location(conf, spec)
    assert "microphone=(self)" in (ROOT / "frontend" / "nginx" / "security-headers.conf").read_text()


def test_the_spa_lives_under_ghostwriter() -> None:
    """The SPA is built for /ghostwriter/ and proxies its API there in dev."""
    vite = (ROOT / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
    assert "base: '/ghostwriter/'" in vite and "'/ghostwriter/api'" in vite


def test_env_example_tags_follow_the_contract_amendment() -> None:
    """The shared keys carry exactly the tags deploy's env-sync rows ask for."""
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()

    def tag(key: str) -> str:
        return lines[lines.index(key) - 1]

    assert tag("# GHOSTWRITER_EDGE_SECRET=") == "# [secret] [shared: edge-plane]"
    assert tag("# WORKBENCH_API_KEY=") == "# [secret] [shared: workbench GHOSTWRITER_WORKBENCH_KEY]"
    assert tag("OPENAI_API_KEY=") == "# [required] [secret] [shared: vllm-service]"
    assert tag("# WORKBENCH_RETENTION_DAYS=7") == "# [shared: workbench]"
    assert all(len(line) < 80 for line in lines)

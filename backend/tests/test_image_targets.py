import json
import pathlib
import re

import pytest

from panel_core.version import VERSION_KEY_BY_ROLE

REPO = pathlib.Path(__file__).resolve().parents[2]

USE_VERSION_STATUS_HOOK = "frontend/src/hooks/useVersionStatus.ts"

IMAGE_TARGETS = {
    "master": {
        "package": "panel-master",
        "image": "ghcr.io/ivantopgaming/panel-master",
        "compose": "docker-compose.master.yml",
        "env_var": "MASTER_IMAGE",
        "dockerfile": "backend/Dockerfile",
    },
    "worker": {
        "package": "panel-worker",
        "image": "ghcr.io/ivantopgaming/panel-worker",
        "compose": "docker-compose.node.yml",
        "env_var": "WORKER_IMAGE",
        "dockerfile": "backend/Dockerfile.worker",
    },
    "sub": {
        "package": "panel-sub",
        "image": "ghcr.io/ivantopgaming/panel-sub",
        "compose": "docker-compose.sub.yml",
        "env_var": "SUB_IMAGE",
        "dockerfile": "backend/Dockerfile",
    },
    "bot_api": {
        "package": "panel-botapi",
        "image": "ghcr.io/ivantopgaming/panel-bot-api",
        "compose": "docker-compose.bot.yml",
        "env_var": "BOT_API_IMAGE",
        "dockerfile": "backend/Dockerfile",
    },
}

WORKFLOWS = {
    "release.yml": ".github/workflows/release.yml",
    "dev-build.yml": ".github/workflows/dev-build.yml",
}

VERSION_PINNED_ENV_VARS = {
    "master": "MASTER_IMAGE",
    "worker": "WORKER_IMAGE",
    "sub": "SUB_IMAGE",
    "bot_api": "BOT_API_IMAGE",
    "frontend": "FRONTEND_IMAGE",
    "caddy": "CADDY_IMAGE",
    "bot": "BOT_IMAGE",
    "xray_egress": "XRAY_EGRESS_IMAGE",
}

THIRD_PARTY_ENV_VARS_WITHOUT_A_VERSIONS_JSON_ENTRY = {
    "XRAY_IMAGE": "upstream Xray-core image; its tag tracks xray_core_ref, not a semver in versions.json",
    "SOCKET_PROXY_IMAGE": "third-party image (tecnativa/docker-socket-proxy), never built or versioned by this repo",
    "REDIS_IMAGE": "third-party image (redis), never built or versioned by this repo",
}

VERSIONS_JSON_KEYS_WITHOUT_AN_ENV_PIN = {
    "xray_core_ref": "build-time Xray-core git ref compiled into the worker image; it is a ref, not an "
    "image tag, so no .env.example *_IMAGE var pins it",
}

DRIFT_DOC = (
    "The per-role image split spreads one fact across five files: versions.json (the version), the two "
    "Dockerfiles (how it is built), the compose file (which variable names it), .env.example (the pin) "
    "and .github/workflows/release.yml (what CI builds). Nothing outside this guard notices when they "
    "disagree, and the failure mode is silent: a stack comes up on a stale or wrong-role image."
)


def _read(relative):
    assert (REPO / "backend").is_dir() and (REPO / "versions.json").is_file(), (
        f"{REPO} is not the repo root — the parents[2] index drifted, and every assertion below would "
        f"fail for the wrong reason.\n\n{DRIFT_DOC}"
    )
    path = REPO / relative
    assert path.is_file(), f"{relative} does not exist under {REPO}"
    return path.read_text()


def _case_branch(text, service, workflow):
    match = re.search(rf"(?:^|\n)[ \t]*{re.escape(service)}\)\n(.*?);;[ \t]*\n", text, re.S)
    assert match, (
        f"{workflow} has no '{service})' case branch — the case statement's shape changed and this guard "
        f"can no longer find it.\n\n{DRIFT_DOC}"
    )
    return match.group(1)


def test_versions_json_names_exactly_the_four_backend_images():
    data = json.loads(_read("versions.json"))
    for key in IMAGE_TARGETS:
        assert key in data, f"versions.json has no '{key}' version\n\n{DRIFT_DOC}"
    assert "backend" not in data, f"versions.json still has the legacy 'backend' key\n\n{DRIFT_DOC}"


@pytest.mark.parametrize("service", sorted(IMAGE_TARGETS))
def test_each_compose_stack_names_its_own_image_variable(service):
    target = IMAGE_TARGETS[service]
    text = _read(target["compose"])
    assert f"${{{target['env_var']}:?" in text, (
        f"{target['compose']} does not read ${{{target['env_var']}:?...}}. The :? form is deliberate — a "
        f"stack that starts on the wrong image is worse than one that refuses to start.\n\n{DRIFT_DOC}"
    )
    assert "BACKEND_IMAGE" not in text, (
        f"{target['compose']} still reads BACKEND_IMAGE, which no longer exists.\n\n{DRIFT_DOC}"
    )


@pytest.mark.parametrize("service", sorted(IMAGE_TARGETS))
def test_env_example_pins_every_role_image(service):
    target = IMAGE_TARGETS[service]
    text = _read(".env.example")
    assert re.search(rf"^{target['env_var']}={re.escape(target['image'])}:v", text, re.M), (
        f".env.example has no {target['env_var']}={target['image']}:v… pin\n\n{DRIFT_DOC}"
    )
    assert not re.search(r"^BACKEND_IMAGE=", text, re.M), f".env.example still pins BACKEND_IMAGE\n\n{DRIFT_DOC}"


@pytest.mark.parametrize("version_key", sorted(VERSION_PINNED_ENV_VARS))
def test_env_example_pin_tracks_the_versions_json_it_was_bumped_from(version_key):
    versions = json.loads(_read("versions.json"))
    env_var = VERSION_PINNED_ENV_VARS[version_key]
    text = _read(".env.example")
    assert version_key in versions, f"versions.json has no '{version_key}' version\n\n{DRIFT_DOC}"
    expected_version = versions[version_key]
    assert re.search(rf"^{env_var}=\S+:v{re.escape(expected_version)}$", text, re.M), (
        f".env.example's {env_var} pin does not equal 'v' + versions.json's '{version_key}' "
        f"({expected_version}). A pin can go stale in either direction — bump one file and forget the "
        f"other — and this is the only check that would ever notice.\n\n{DRIFT_DOC}"
    )


def test_every_image_var_in_env_example_is_either_version_pinned_or_explicitly_third_party():
    text = _read(".env.example")
    declared = set(re.findall(r"^(\w+_IMAGE)=", text, re.M))
    accounted_for = set(VERSION_PINNED_ENV_VARS.values()) | set(THIRD_PARTY_ENV_VARS_WITHOUT_A_VERSIONS_JSON_ENTRY)
    unaccounted = declared - accounted_for
    assert unaccounted == set(), (
        f".env.example declares {sorted(unaccounted)} but this guard has no opinion on them — add each "
        f"to VERSION_PINNED_ENV_VARS (if versions.json should own its tag) or to "
        f"THIRD_PARTY_ENV_VARS_WITHOUT_A_VERSIONS_JSON_ENTRY (with a reason) so a new *_IMAGE var can't "
        f"silently drift unchecked.\n\n{DRIFT_DOC}"
    )


def test_every_versions_json_key_is_either_env_pinned_or_explicitly_a_non_image_ref():
    versions = json.loads(_read("versions.json"))
    accounted_for = set(VERSION_PINNED_ENV_VARS) | set(VERSIONS_JSON_KEYS_WITHOUT_AN_ENV_PIN)
    unaccounted = set(versions) - accounted_for
    assert unaccounted == set(), (
        f"versions.json declares {sorted(unaccounted)} but no .env.example *_IMAGE var pins it, and this "
        f"guard has no opinion on it either. Add each to VERSION_PINNED_ENV_VARS (if it should own an "
        f".env.example *_IMAGE pin) or to VERSIONS_JSON_KEYS_WITHOUT_AN_ENV_PIN (with a reason, the way "
        f"xray_core_ref is a build-time ref rather than an image) so a new versions.json key can't "
        f"silently drift unchecked.\n\n{DRIFT_DOC}"
    )


@pytest.mark.parametrize("service", sorted(IMAGE_TARGETS))
def test_the_release_workflow_builds_every_role_image(service):
    target = IMAGE_TARGETS[service]
    text = _read(".github/workflows/release.yml")
    assert f'"{service}"' in text, (
        f"release.yml's 'services' detection tuple never names '{service}' — a service missing from that "
        f"tuple never gets noticed as bumped, so a released version of it is never built.\n\n{DRIFT_DOC}"
    )
    assert target["image"] in text, f"release.yml never builds {target['image']}\n\n{DRIFT_DOC}"
    assert "panel-backend" not in text, f"release.yml still builds the retired panel-backend image\n\n{DRIFT_DOC}"


@pytest.mark.parametrize("service", sorted(IMAGE_TARGETS))
def test_the_dev_build_workflow_builds_every_role_image(service):
    target = IMAGE_TARGETS[service]
    text = _read(".github/workflows/dev-build.yml")
    assert re.search(rf"for SERVICE in[^\n]*\b{re.escape(service)}\b", text), (
        f"dev-build.yml's build loop never names the '{service}' service — a service missing from that "
        f"loop is never built as a dev image.\n\n{DRIFT_DOC}"
    )
    assert target["image"] in text, f"dev-build.yml never builds {target['image']}\n\n{DRIFT_DOC}"
    assert "panel-backend" not in text, f"dev-build.yml still builds the retired panel-backend image\n\n{DRIFT_DOC}"


@pytest.mark.parametrize("workflow_name", sorted(WORKFLOWS))
@pytest.mark.parametrize("service", sorted(IMAGE_TARGETS))
def test_the_case_branch_pairs_its_own_package_and_dockerfile_with_its_own_image(service, workflow_name):
    target = IMAGE_TARGETS[service]
    workflow = WORKFLOWS[workflow_name]
    text = _read(workflow)
    body = _case_branch(text, service, workflow)

    assert f'IMAGE="{target["image"]}"' in body, (
        f"{workflow}'s '{service})' case branch does not set IMAGE=\"{target['image']}\"\n\n{DRIFT_DOC}"
    )

    package_match = re.search(r"PANEL_PACKAGE=([\w.-]+)", body)
    actual_package = package_match.group(1) if package_match else None
    if service == "worker":
        assert actual_package is None, (
            f"{workflow}'s 'worker)' case branch passes --build-arg PANEL_PACKAGE={actual_package!r}, but "
            f"the worker's Dockerfile hardcodes --package panel-worker itself and takes no such build-arg."
            f"\n\n{DRIFT_DOC}"
        )
    else:
        assert actual_package == target["package"], (
            f"{workflow}'s '{service})' case branch builds --build-arg PANEL_PACKAGE={actual_package!r} but "
            f"publishes it as the '{target['image']}' image, which must build PANEL_PACKAGE="
            f"{target['package']!r} instead. Nothing else ties the package a case branch builds to the "
            f"image name it is pushed under — a mismatch here builds, boots and answers /healthz just "
            f"fine, it just runs the wrong role's blueprints under the wrong image name, silently."
            f"\n\n{DRIFT_DOC}"
        )

    dockerfile_flag_match = re.search(r'-f\s+([^\s"]+)', body)
    actual_dockerfile = dockerfile_flag_match.group(1) if dockerfile_flag_match else "backend/Dockerfile"
    assert actual_dockerfile == target["dockerfile"], (
        f"{workflow}'s '{service})' case branch builds against {actual_dockerfile}, but must build against "
        f"{target['dockerfile']}\n\n{DRIFT_DOC}"
    )


@pytest.mark.parametrize("workflow_name", sorted(WORKFLOWS))
def test_the_case_statement_fails_loudly_on_an_unrecognized_service(workflow_name):
    workflow = WORKFLOWS[workflow_name]
    text = _read(workflow)
    body = _case_branch(text, "*", workflow)
    assert re.search(r"\bexit\s+[1-9]\d*\b", body), (
        f"{workflow}'s '*)' fallback branch does not exit non-zero. Without it, a ninth service silently "
        f"reuses the previous loop iteration's IMAGE/CONTEXT/BUILD_ARGS and gets built and published under "
        f"the wrong role's image name.\n\n{DRIFT_DOC}"
    )


def test_the_light_dockerfile_requires_its_package_argument():
    text = _read("backend/Dockerfile")
    assert re.search(r"^ARG PANEL_PACKAGE\s*$", text, re.M), (
        "backend/Dockerfile's ARG PANEL_PACKAGE must carry NO default. A default lets a forgotten "
        "--build-arg publish one role's image under another role's name, which no later check catches "
        f"because the image builds and boots perfectly well.\n\n{DRIFT_DOC}"
    )
    assert 'test -n "$PANEL_PACKAGE"' in text, (
        f"backend/Dockerfile does not fail the build when PANEL_PACKAGE is empty\n\n{DRIFT_DOC}"
    )


def test_only_the_worker_image_carries_the_xray_runtime():
    light = _read("backend/Dockerfile")
    worker = _read("backend/Dockerfile.worker")

    for marker in ("xraybin", "grpc_tools.protoc", "XRAY_CORE_REF"):
        assert marker not in light, (
            f"backend/Dockerfile mentions {marker!r}. The three light roles must not carry the Xray "
            f"binary, the protobuf stubs or the toolchain that generates them — dropping those is the "
            f"point of this split.\n\n{DRIFT_DOC}"
        )
        assert marker in worker, f"backend/Dockerfile.worker no longer mentions {marker!r}; the worker needs all three."

    assert "--package panel-worker" in worker, "Dockerfile.worker must sync the panel-worker package"
    assert "--package" in light and "PANEL_PACKAGE" in light


def _frontend_backend_role_order():
    text = _read(USE_VERSION_STATUS_HOOK)
    match = re.search(r"BACKEND_ROLE_ORDER\s*=\s*\[([^\]]*)\]", text)
    assert match, (
        f"{USE_VERSION_STATUS_HOOK} has no 'BACKEND_ROLE_ORDER = [...]' array literal — this guard can "
        f"no longer find it.\n\n{DRIFT_DOC}"
    )
    return re.findall(r"'([^']+)'", match.group(1))


def test_frontend_backend_role_order_matches_the_backend_version_keys():
    backend_roles = set(VERSION_KEY_BY_ROLE.values())
    frontend_roles = set(_frontend_backend_role_order())
    missing_from_frontend = backend_roles - frontend_roles
    unknown_to_backend = frontend_roles - backend_roles
    assert not missing_from_frontend and not unknown_to_backend, (
        f"backend/packages/panel-core/src/panel_core/version.py's VERSION_KEY_BY_ROLE declares "
        f"{sorted(backend_roles)} but {USE_VERSION_STATUS_HOOK}'s BACKEND_ROLE_ORDER declares "
        f"{sorted(frontend_roles)} (missing from frontend: {sorted(missing_from_frontend)}; unknown to "
        f"backend: {sorted(unknown_to_backend)}). Edit BACKEND_ROLE_ORDER in {USE_VERSION_STATUS_HOOK} to "
        f"match — a role missing from the frontend list silently drops that backend's version row from "
        f"System → About, with no TypeScript error and no failing test.\n\n{DRIFT_DOC}"
    )


def test_every_workspace_member_pyproject_is_copied_into_both_builds():
    members = sorted(p.parent.name for p in (REPO / "backend" / "packages").glob("*/pyproject.toml"))
    assert len(members) == 6, f"expected six workspace members, found {members}"
    for dockerfile in ("backend/Dockerfile", "backend/Dockerfile.worker"):
        text = _read(dockerfile)
        missing = [m for m in members if f"packages/{m}/pyproject.toml" not in text]
        assert missing == [], (
            f"{dockerfile} does not COPY {missing}. uv resolves the whole workspace even when syncing a "
            f"single package, so a missing member pyproject fails the build.\n\n{DRIFT_DOC}"
        )

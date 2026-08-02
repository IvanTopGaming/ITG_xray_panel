import json
import pathlib
import re

import pytest

from panel_core.version import VERSION_KEY_BY_ROLE

REPO = pathlib.Path(__file__).resolve().parents[2]

USE_VERSION_STATUS_HOOK = "frontend/packages/ui-core/src/hooks/useVersionStatus.ts"

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
        "package_hardcoded_in_dockerfile": True,
    },
    "sub": {
        "package": "panel-sub",
        "image": "ghcr.io/ivantopgaming/panel-sub",
        "compose": "docker-compose.sub.yml",
        "env_var": "SUB_IMAGE",
        "dockerfile": "backend/Dockerfile.sub",
        "package_hardcoded_in_dockerfile": True,
    },
    "bot_api": {
        "package": "panel-botapi",
        "image": "ghcr.io/ivantopgaming/panel-bot-api",
        "compose": "docker-compose.bot.yml",
        "env_var": "BOT_API_IMAGE",
        "dockerfile": "backend/Dockerfile",
    },
    "cron": {
        "package": "panel-cron",
        "image": "ghcr.io/ivantopgaming/panel-cron",
        "compose": "docker-compose.cron.yml",
        "env_var": "CRON_IMAGE",
        "dockerfile": "backend/Dockerfile",
    },
}

FRONTEND_IMAGE_TARGETS = {
    "frontend_admin": {
        "ui_package": "admin",
        "image": "ghcr.io/ivantopgaming/panel-frontend-admin",
        "compose": "docker-compose.master.yml",
        "env_var": "FRONTEND_ADMIN_IMAGE",
    },
    "frontend_node": {
        "ui_package": "node",
        "image": "ghcr.io/ivantopgaming/panel-frontend-node",
        "compose": "docker-compose.node.yml",
        "env_var": "FRONTEND_NODE_IMAGE",
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
    "cron": "CRON_IMAGE",
    "frontend_admin": "FRONTEND_ADMIN_IMAGE",
    "frontend_node": "FRONTEND_NODE_IMAGE",
    "caddy": "CADDY_IMAGE",
    "bot": "BOT_IMAGE",
    "xray_egress": "XRAY_EGRESS_IMAGE",
}

THIRD_PARTY_ENV_VARS_WITHOUT_A_VERSIONS_JSON_ENTRY = {
    "XRAY_IMAGE": "upstream Xray-core image; its tag tracks xray_core_ref, not a semver in versions.json",
    "SOCKET_PROXY_IMAGE": "third-party image (tecnativa/docker-socket-proxy), never built or versioned by this repo",
    "REDIS_IMAGE": "third-party image (redis), never built or versioned by this repo",
    "POSTGRES_IMAGE": "third-party image (postgres), never built or versioned by this repo. It pins "
    "both the server and the pg-backup sidecar from one variable on purpose: pg_dump refuses to dump "
    "from a server newer than itself, so letting the two drift turns the backup into a silent no-op",
}

VERSIONS_JSON_KEYS_WITHOUT_AN_ENV_PIN = {
    "xray_core_ref": "build-time Xray-core git ref compiled into the worker image; it is a ref, not an "
    "image tag, so no .env.<host>.example *_IMAGE var pins it",
}

ENV_EXAMPLE_BY_COMPOSE = {
    "docker-compose.master.yml": ".env.master.example",
    "docker-compose.node.yml": ".env.node.example",
    "docker-compose.sub.yml": ".env.sub.example",
    "docker-compose.bot.yml": ".env.bot.example",
    "docker-compose.cron.yml": ".env.cron.example",
    "docker-compose.postgres.yml": ".env.data.example",
}

ENV_EXAMPLES = sorted(set(ENV_EXAMPLE_BY_COMPOSE.values()))

DRIFT_DOC = (
    "The per-role image split spreads one fact across five files: versions.json (the version), the two "
    "Dockerfiles (how it is built), the compose file (which variable names it), that host's "
    ".env.<host>.example (the pin) and .github/workflows/release.yml (what CI builds). Nothing outside "
    "this guard notices when they disagree, and the failure mode is silent: a stack comes up on a stale "
    "or wrong-role image."
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


def test_versions_json_names_exactly_the_five_backend_images():
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
    example = ENV_EXAMPLE_BY_COMPOSE[target["compose"]]
    text = _read(example)
    assert re.search(rf"^{target['env_var']}={re.escape(target['image'])}:v", text, re.M), (
        f"{example} has no {target['env_var']}={target['image']}:v… pin, though {target['compose']} — the "
        f"compose file that host runs — demands it\n\n{DRIFT_DOC}"
    )
    for other in ENV_EXAMPLES:
        assert not re.search(r"^BACKEND_IMAGE=", _read(other), re.M), f"{other} still pins BACKEND_IMAGE\n\n{DRIFT_DOC}"


@pytest.mark.parametrize("version_key", sorted(VERSION_PINNED_ENV_VARS))
def test_env_example_pin_tracks_the_versions_json_it_was_bumped_from(version_key):
    versions = json.loads(_read("versions.json"))
    env_var = VERSION_PINNED_ENV_VARS[version_key]
    assert version_key in versions, f"versions.json has no '{version_key}' version\n\n{DRIFT_DOC}"
    expected_version = versions[version_key]
    declaring = [example for example in ENV_EXAMPLES if re.search(rf"^{env_var}=", _read(example), re.M)]
    assert declaring, (
        f"no .env.<host>.example pins {env_var} at all, so versions.json's '{version_key}' reaches no "
        f"deployer. Every image belongs to some host's file.\n\n{DRIFT_DOC}"
    )
    for example in declaring:
        assert re.search(rf"^{env_var}=\S+:v{re.escape(expected_version)}$", _read(example), re.M), (
            f"{example}'s {env_var} pin does not equal 'v' + versions.json's '{version_key}' "
            f"({expected_version}). A pin can go stale in either direction — bump one file and forget the "
            f"other — and since the split, a shared tag like CADDY_IMAGE has four places to go stale "
            f"in. This is the only check that would ever notice.\n\n{DRIFT_DOC}"
        )


def test_every_image_var_in_env_examples_is_either_version_pinned_or_explicitly_third_party():
    declared = set()
    for example in ENV_EXAMPLES:
        declared |= set(re.findall(r"^(\w+_IMAGE)=", _read(example), re.M))
    assert declared, "no *_IMAGE pins found in any .env.<host>.example — this guard would pass vacuously."
    accounted_for = set(VERSION_PINNED_ENV_VARS.values()) | set(THIRD_PARTY_ENV_VARS_WITHOUT_A_VERSIONS_JSON_ENTRY)
    unaccounted = declared - accounted_for
    assert unaccounted == set(), (
        f"the .env.<host>.example files declare {sorted(unaccounted)} but this guard has no opinion on "
        f"them — add each to VERSION_PINNED_ENV_VARS (if versions.json should own its tag) or to "
        f"THIRD_PARTY_ENV_VARS_WITHOUT_A_VERSIONS_JSON_ENTRY (with a reason) so a new *_IMAGE var can't "
        f"silently drift unchecked.\n\n{DRIFT_DOC}"
    )


@pytest.mark.parametrize("env_var", sorted(THIRD_PARTY_ENV_VARS_WITHOUT_A_VERSIONS_JSON_ENTRY))
def test_every_third_party_image_pin_names_a_tag_a_registry_could_serve(env_var):
    declaring = [example for example in ENV_EXAMPLES if re.search(rf"^{env_var}=", _read(example), re.M)]
    assert declaring, (
        f"no .env.<host>.example declares {env_var}, so this guard would pass vacuously. Either the "
        f"variable is gone (drop it from THIRD_PARTY_ENV_VARS_WITHOUT_A_VERSIONS_JSON_ENTRY) or its pin "
        f"was lost.\n\n{DRIFT_DOC}"
    )
    for example in declaring:
        value = re.search(rf"^{env_var}=(\S+)$", _read(example), re.M)
        assert value, f"{example}'s {env_var} has an empty value\n\n{DRIFT_DOC}"
        reference = value.group(1)
        _, separator, tag = reference.rpartition(":")
        assert separator and "/" not in tag, (
            f"{example} pins {env_var}={reference} with no tag, so the deploy silently tracks whatever "
            f":latest happens to be that day. Every image on every host is pinned; this one is not.\n\n"
            f"{DRIFT_DOC}"
        )
        assert not re.fullmatch(r"v?[XYZ](\.[XYZ])*", tag) and not re.search(r"[<>]|CHANGE|TODO", tag), (
            f"{example} pins {env_var}={reference}, which is a placeholder rather than a tag any registry "
            f"can serve — `docker compose up` on that host fails on the pull and the role never starts. "
            f"This is not hypothetical: SOCKET_PROXY_IMAGE carried a literal `vX.Y.Z` from the per-host "
            f"split until the 3.0.0 release, so a node installed by scripts/install.sh could not come up "
            f"at all. It survived because the guard above accounts for third-party vars by NAME and then "
            f"never looks at their VALUE — version-pinned vars are checked against versions.json, and "
            f"these had nothing to be checked against.\n\n{DRIFT_DOC}"
        )


def test_every_versions_json_key_is_either_env_pinned_or_explicitly_a_non_image_ref():
    versions = json.loads(_read("versions.json"))
    accounted_for = set(VERSION_PINNED_ENV_VARS) | set(VERSIONS_JSON_KEYS_WITHOUT_AN_ENV_PIN)
    unaccounted = set(versions) - accounted_for
    assert unaccounted == set(), (
        f"versions.json declares {sorted(unaccounted)} but no .env.example *_IMAGE var pins it, and this "
        f"guard has no opinion on it either. Add each to VERSION_PINNED_ENV_VARS (if it should own an "
        f".env.<host>.example *_IMAGE pin) or to VERSIONS_JSON_KEYS_WITHOUT_AN_ENV_PIN (with a reason, the way "
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
    if target.get("package_hardcoded_in_dockerfile"):
        assert actual_package is None, (
            f"{workflow}'s '{service})' case branch passes --build-arg PANEL_PACKAGE={actual_package!r}, but "
            f"{target['dockerfile']} hardcodes --package {target['package']} itself and takes no such "
            f"build-arg.\n\n{DRIFT_DOC}"
        )
        assert f"--package {target['package']}" in _read(target["dockerfile"]), (
            f"{target['dockerfile']} does not sync --package {target['package']}. It takes no PANEL_PACKAGE "
            f"build-arg, so the package name written into the file is the only thing deciding which role "
            f"the '{target['image']}' image actually runs.\n\n{DRIFT_DOC}"
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


def test_versions_json_names_both_frontend_images():
    data = json.loads(_read("versions.json"))
    for key in FRONTEND_IMAGE_TARGETS:
        assert key in data, f"versions.json has no '{key}' version\n\n{DRIFT_DOC}"
    assert "frontend" not in data, f"versions.json still has the legacy 'frontend' key\n\n{DRIFT_DOC}"


@pytest.mark.parametrize("target_key", sorted(FRONTEND_IMAGE_TARGETS))
def test_each_compose_stack_names_its_own_frontend_image_variable(target_key):
    target = FRONTEND_IMAGE_TARGETS[target_key]
    text = _read(target["compose"])
    assert f"${{{target['env_var']}:?" in text, (
        f"{target['compose']} does not read ${{{target['env_var']}:?...}}. The :? form is deliberate — a "
        f"stack that starts on the wrong image is worse than one that refuses to start.\n\n{DRIFT_DOC}"
    )
    assert "FRONTEND_IMAGE" not in text, (
        f"{target['compose']} still reads the retired single FRONTEND_IMAGE variable.\n\n{DRIFT_DOC}"
    )


@pytest.mark.parametrize("target_key", sorted(FRONTEND_IMAGE_TARGETS))
def test_env_example_pins_every_frontend_image(target_key):
    target = FRONTEND_IMAGE_TARGETS[target_key]
    example = ENV_EXAMPLE_BY_COMPOSE[target["compose"]]
    assert re.search(rf"^{target['env_var']}={re.escape(target['image'])}:v", _read(example), re.M), (
        f"{example} has no {target['env_var']}={target['image']}:v… pin, though {target['compose']} — the "
        f"compose file that host runs — demands it\n\n{DRIFT_DOC}"
    )
    for other in ENV_EXAMPLES:
        assert not re.search(r"^FRONTEND_IMAGE=", _read(other), re.M), (
            f"{other} still pins the retired single FRONTEND_IMAGE\n\n{DRIFT_DOC}"
        )


@pytest.mark.parametrize("target_key", sorted(FRONTEND_IMAGE_TARGETS))
def test_the_release_workflow_builds_every_frontend_image(target_key):
    target = FRONTEND_IMAGE_TARGETS[target_key]
    text = _read(".github/workflows/release.yml")
    assert f'"{target_key}"' in text, (
        f"release.yml's 'services' detection tuple never names '{target_key}' — a service missing from "
        f"that tuple never gets noticed as bumped, so a released version of it is never built.\n\n{DRIFT_DOC}"
    )
    assert target["image"] in text, f"release.yml never builds {target['image']}\n\n{DRIFT_DOC}"
    assert not re.search(r'"frontend"', text), (
        f"release.yml still names the retired single 'frontend' service\n\n{DRIFT_DOC}"
    )


@pytest.mark.parametrize("target_key", sorted(FRONTEND_IMAGE_TARGETS))
def test_the_dev_build_workflow_builds_every_frontend_image(target_key):
    target = FRONTEND_IMAGE_TARGETS[target_key]
    text = _read(".github/workflows/dev-build.yml")
    assert re.search(rf"for SERVICE in[^\n]*\b{re.escape(target_key)}\b", text), (
        f"dev-build.yml's build loop never names the '{target_key}' service — a service missing from that "
        f"loop is never built as a dev image.\n\n{DRIFT_DOC}"
    )
    assert target["image"] in text, f"dev-build.yml never builds {target['image']}\n\n{DRIFT_DOC}"
    assert not re.search(r"for SERVICE in[^\n]*\bfrontend\b", text), (
        f"dev-build.yml's build loop still names the retired single 'frontend' service\n\n{DRIFT_DOC}"
    )


@pytest.mark.parametrize("workflow_name", sorted(WORKFLOWS))
@pytest.mark.parametrize("target_key", sorted(FRONTEND_IMAGE_TARGETS))
def test_the_frontend_case_branch_pairs_its_own_ui_package_with_its_own_image(target_key, workflow_name):
    target = FRONTEND_IMAGE_TARGETS[target_key]
    workflow = WORKFLOWS[workflow_name]
    text = _read(workflow)
    body = _case_branch(text, target_key, workflow)

    assert f'IMAGE="{target["image"]}"' in body, (
        f"{workflow}'s '{target_key})' case branch does not set IMAGE=\"{target['image']}\"\n\n{DRIFT_DOC}"
    )
    assert 'CONTEXT="./frontend"' in body, (
        f"{workflow}'s '{target_key})' case branch does not build against the ./frontend context\n\n{DRIFT_DOC}"
    )

    package_match = re.search(r"UI_PACKAGE=([\w.-]+)", body)
    actual_package = package_match.group(1) if package_match else None
    assert actual_package == target["ui_package"], (
        f"{workflow}'s '{target_key})' case branch builds --build-arg UI_PACKAGE={actual_package!r} but "
        f"publishes it as the '{target['image']}' image, which must build UI_PACKAGE={target['ui_package']!r} "
        f"instead. Nothing else ties the app a case branch builds to the image name it is pushed under — a "
        f"mismatch here builds and boots just fine, it just serves the wrong app under the wrong image name, "
        f"silently.\n\n{DRIFT_DOC}"
    )


def test_the_frontend_dockerfile_requires_its_package_argument():
    text = _read("frontend/Dockerfile")
    assert re.search(r"^ARG UI_PACKAGE\s*$", text, re.M), (
        "frontend/Dockerfile's ARG UI_PACKAGE must carry NO default. A default lets a forgotten --build-arg "
        "publish one app's image under the other app's name, which no later check catches because the "
        f"image builds and boots perfectly well.\n\n{DRIFT_DOC}"
    )
    assert 'test -n "$UI_PACKAGE"' in text, (
        f"frontend/Dockerfile does not fail the build when UI_PACKAGE is empty\n\n{DRIFT_DOC}"
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
    worker = _read("backend/Dockerfile.worker")

    for light_path in ("backend/Dockerfile", "backend/Dockerfile.sub"):
        light = _read(light_path)
        for marker in ("xraybin", "grpc_tools.protoc", "XRAY_CORE_REF"):
            assert marker not in light, (
                f"{light_path} mentions {marker!r}. The three light roles must not carry the Xray "
                f"binary, the protobuf stubs or the toolchain that generates them — dropping those is the "
                f"point of this split.\n\n{DRIFT_DOC}"
            )

    for marker in ("xraybin", "grpc_tools.protoc", "XRAY_CORE_REF"):
        assert marker in worker, f"backend/Dockerfile.worker no longer mentions {marker!r}; the worker needs all three."

    assert "--package panel-worker" in worker, "Dockerfile.worker must sync the panel-worker package"
    light = _read("backend/Dockerfile")
    assert "--package" in light and "PANEL_PACKAGE" in light


def test_the_repo_root_build_context_is_filtered():
    patterns = {line.strip() for line in _read(".dockerignore").splitlines() if line.strip()}
    for required in ("**/node_modules/", "**/dist/", ".git/"):
        assert required in patterns, (
            f"the repo-root .dockerignore does not exclude {required!r}. Every Dockerfile that takes "
            f"--build-context project=. draws from this unfiltered directory, and backend/Dockerfile.sub "
            f"copies all of frontend/ out of it: without this file the host's node_modules is shipped into "
            f"the build and lands on top of the container's own npm ci, which fails loudly on an arch "
            f"mismatch and silently otherwise.\n\n{DRIFT_DOC}"
        )
    assert not re.search(r"^versions\.json$", _read(".dockerignore"), re.M), (
        f"the repo-root .dockerignore excludes versions.json, which every image COPYs from the project "
        f"context to bake its version.\n\n{DRIFT_DOC}"
    )


def test_only_the_sub_image_bakes_a_frontend_bundle():
    sub = _read("backend/Dockerfile.sub")
    assert "@panel/sub-page" in sub and "/app/ui" in sub, (
        "backend/Dockerfile.sub no longer builds @panel/sub-page into /app/ui. That bundle is the "
        "subscription page itself — without it the role boots fine and answers 503 on the page while "
        "still serving configs, which is exactly the failure nobody notices until a user opens the "
        f"link.\n\n{DRIFT_DOC}"
    )
    for other in ("backend/Dockerfile", "backend/Dockerfile.worker"):
        assert "sub-page" not in _read(other), (
            f"{other} builds the sub-page bundle too. Only the sub image serves that page, and a Node "
            f"build stage in the other backend images buys them nothing but build time.\n\n{DRIFT_DOC}"
        )


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


def test_every_workspace_member_pyproject_is_copied_into_every_build():
    members = sorted(p.parent.name for p in (REPO / "backend" / "packages").glob("*/pyproject.toml"))
    assert len(members) == 8, f"expected eight workspace members, found {members}"
    for dockerfile in ("backend/Dockerfile", "backend/Dockerfile.worker", "backend/Dockerfile.sub"):
        text = _read(dockerfile)
        missing = [m for m in members if f"packages/{m}/pyproject.toml" not in text]
        assert missing == [], (
            f"{dockerfile} does not COPY {missing}. uv resolves the whole workspace even when syncing a "
            f"single package, so a missing member pyproject fails the build.\n\n{DRIFT_DOC}"
        )


@pytest.mark.parametrize("target_key", sorted(FRONTEND_IMAGE_TARGETS))
def test_each_frontend_app_bakes_the_version_key_its_image_is_published_under(target_key):
    target = FRONTEND_IMAGE_TARGETS[target_key]
    config = f"frontend/packages/{target['ui_package']}/vite.config.ts"
    text = _read(config)

    assert f"__FRONTEND_VERSION_KEY__: JSON.stringify('{target_key}')" in text, (
        f"{config} does not define __FRONTEND_VERSION_KEY__ as '{target_key}'. That constant is what the "
        f"About card and the update indicator read out of versions.json, and it is the one link in this "
        f"chain no other guard covers: rename the key everywhere else -- versions.json, .env.example, "
        f"both compose files, both workflows -- and miss this define, and the panel renders 'vundefined' "
        f"with typecheck, lint, build and the whole test suite green.\n\n{DRIFT_DOC}"
    )

    other_keys = sorted(set(FRONTEND_IMAGE_TARGETS) - {target_key})
    for other in other_keys:
        assert f"JSON.stringify('{other}')" not in text, (
            f"{config} also defines a constant as '{other}' — the two frontend apps must not bake each "
            f"other's version key, or one image reports the other's version.\n\n{DRIFT_DOC}"
        )

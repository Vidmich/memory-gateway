"""The chart, the alerts and the dashboards, as things that can be wrong (task 18).

Deployment assets rot in a way application code does not: a metric gets renamed, an alert
keeps referring to the old name, and the alert simply never fires again. Nothing fails.
Nobody notices until the night it was supposed to fire.

So the checks here are mostly *joins between two artefacts*: every PromQL expression
against the metric names the application actually registers, every alert against the
runbook it links to, every values file against the chart's own invariants. The parts that
need `helm` itself are marked and skip without it — CI has it, a laptop may not, and the
joins above are the ones that catch real drift anyway.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.metrics import build_metrics

CHART = Path("deploy/helm/memory-gateway")
EXAMPLES = Path("deploy/helm/examples")
DASHBOARDS = Path("deploy/grafana")
RUNBOOKS = Path("docs/runbooks")

#: PromQL identifiers that are functions, keywords or label names rather than metrics.
NOT_A_METRIC = frozenset(
    {
        "sum",
        "rate",
        "min",
        "max",
        "avg",
        "by",
        "clamp_min",
        "clamp_max",
        "histogram_quantile",
        "le",
        "increase",
        "count",
        "without",
        "on",
        "ignoring",
        "and",
        "or",
        "unless",
        "topk",
        "bottomk",
        "absent",
        "up",
        "job",
        "status",
        "gateway",
        "model",
        "outcome",
        "reason",
        "table",
        "format",
        "disposition",
        # Task 102's summarization token counter is labelled by direction (in, out).
        "direction",
        "policy",
        "limit",
        "scope",
        "error_code",
        "strategy",
    }
)

#: Suffixes that are added to a metric's *base* name on the wire. ``prometheus_client``
#: reports a family under its base name — ``proxy_requests`` — while every legal query
#: spells it ``proxy_requests_total`` or, for a histogram, ``..._bucket``.
EXPOSED_SUFFIXES = ("_total", "_bucket", "_sum", "_count")


def registered_metrics() -> set[str]:
    """Every metric name the application actually exposes, from the real registry.

    Built rather than listed, so this cannot drift from ``app/core/metrics.py``.
    """
    metrics = build_metrics(service_name="memory-gateway", version="0.0.0")
    names: set[str] = set()
    for family in metrics.registry.collect():
        names.add(family.name)
        for sample in family.samples:
            names.add(sample.name)
    # `prometheus_client` reports a counter family without its `_total` suffix and the
    # samples with it, so both spellings end up in the set — which is right, because both
    # are legal in a query.
    return names


def metric_names_in(expression: str) -> set[str]:
    """The metric names a PromQL expression selects.

    Label matchers, range selectors and quoted strings are removed first. Without that,
    ``rate(proxy_requests_total{status="5.."}[5m])`` contributes ``status``, ``m`` and a
    fragment of the regex — all of which would have to be excluded by name, which is a list
    that grows every time somebody writes a new query.
    """
    stripped = re.sub(r"\{[^}]*\}", "", expression)
    stripped = re.sub(r"\[[^]]*\]", "", stripped)
    stripped = re.sub(r'"[^"]*"', "", stripped)
    return {
        token
        for token in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", stripped)
        if token not in NOT_A_METRIC and not token.startswith("$")
    }


def resolves(name: str, registered: set[str]) -> bool:
    if name in registered:
        return True
    return any(
        name.endswith(suffix) and name[: -len(suffix)] in registered for suffix in EXPOSED_SUFFIXES
    )


def rendered_rules() -> dict[str, Any]:
    """The PrometheusRule as YAML, with the Helm template syntax stripped.

    Rendering it properly needs `helm`; what is being checked here is the *content* of the
    expressions and annotations, which does not depend on values. So the template
    directives are removed textually and the rest is parsed.
    """
    text = (CHART / "templates/prometheusrule.yaml").read_text(encoding="utf-8")
    # `{{ ... }}` becomes a placeholder; the escaped `{{ $value }}` forms inside annotation
    # strings become plain text.
    text = re.sub(r"\{\{-?\s*`([^`]*)`\s*-?\}\}", r"\1", text)
    text = re.sub(r"\{\{-?.*?-?\}\}", "PLACEHOLDER", text, flags=re.S)
    text = "\n".join(line for line in text.splitlines() if line.strip() != "PLACEHOLDER")
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)
    return parsed


def alerts() -> list[dict[str, Any]]:
    found = []
    for group in rendered_rules()["spec"]["groups"]:
        found.extend(group["rules"])
    return found


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------


def test_there_are_alerts_for_the_things_task_18_names() -> None:
    names = {rule["alert"] for rule in alerts()}

    assert {
        "GatewayErrorRateHigh",
        "GatewayOverheadBudgetBreached",
        "UpstreamFailureRateHigh",
        "RequestLogsDropped",
        "WorkerBacklogGrowing",
        "PartitionRunwayLow",
        "RateLimiterUnavailable",
        "DistillationFailureRateHigh",
    } <= names


@pytest.mark.parametrize("rule", alerts(), ids=lambda rule: str(rule["alert"]))
def test_every_alert_queries_metrics_that_exist(rule: dict[str, Any]) -> None:
    """The join that catches the failure this file exists for: a metric renamed in
    ``app/core/metrics.py`` while an alert keeps querying the old name. The alert does not
    error — it evaluates to nothing, for ever."""
    registered = registered_metrics()
    unknown = {name for name in metric_names_in(rule["expr"]) if not resolves(name, registered)}

    assert not unknown, f"{rule['alert']} queries metrics that are not registered: {unknown}"


@pytest.mark.parametrize("rule", alerts(), ids=lambda rule: str(rule["alert"]))
def test_every_alert_has_a_runbook_that_exists(rule: dict[str, Any]) -> None:
    """Task 18's acceptance criterion, as a test. A `runbook_url` pointing at a file
    nobody wrote is worse than no link: it is a page that begins with a 404."""
    url = rule["annotations"].get("runbook_url")
    assert url, f"{rule['alert']} has no runbook_url"

    assert (RUNBOOKS / url.rsplit("/", 1)[-1]).is_file(), f"{rule['alert']} links to {url}"


@pytest.mark.parametrize("rule", alerts(), ids=lambda rule: str(rule["alert"]))
def test_every_alert_has_a_summary_and_a_severity(rule: dict[str, Any]) -> None:
    assert rule["annotations"].get("summary")
    assert rule["labels"]["severity"] in {"critical", "warning"}
    # `for` on every alert, so a single scrape blip does not page anybody.
    assert rule.get("for")


def test_the_partition_alert_is_critical_and_has_a_week_of_margin() -> None:
    """It is the one alert here where the failure is an INSERT that errors rather than a
    query that is slow, and where the fix has to happen before midnight."""
    rule = next(r for r in alerts() if r["alert"] == "PartitionRunwayLow")

    assert rule["labels"]["severity"] == "critical"
    assert "< 7" in rule["expr"]


def test_the_overhead_alert_is_written_against_the_spec_budget() -> None:
    rule = next(r for r in alerts() if r["alert"] == "GatewayOverheadBudgetBreached")

    assert "gateway_overhead_seconds_bucket" in rule["expr"]
    assert "0.15" in rule["expr"], "SPEC §4.2's 150 ms, in seconds"


def test_every_runbook_is_reachable_from_the_index() -> None:
    index = (RUNBOOKS / "README.md").read_text(encoding="utf-8")

    for runbook in RUNBOOKS.glob("*.md"):
        if runbook.name == "README.md":
            continue
        assert runbook.name in index, f"{runbook.name} is not listed in the index"


# ---------------------------------------------------------------------------
# dashboards
# ---------------------------------------------------------------------------


def dashboards() -> list[tuple[str, dict[str, Any]]]:
    return [
        (path.name, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(DASHBOARDS.glob("*.json"))
    ]


def test_the_four_dashboards_task_18_asks_for_are_committed() -> None:
    assert {name for name, _ in dashboards()} == {
        "service-health.json",
        "tenant-traffic.json",
        "memory-subsystem.json",
        "ingestion-pipeline.json",
    }


@pytest.mark.parametrize(("name", "board"), dashboards(), ids=lambda value: str(value)[:40])
def test_every_dashboard_panel_queries_metrics_that_exist(name: str, board: dict[str, Any]) -> None:
    registered = registered_metrics()
    for panel in board["panels"]:
        for target in panel.get("targets", []):
            unknown = {
                metric
                for metric in metric_names_in(target["expr"])
                if not resolves(metric, registered)
            }
            assert not unknown, f"{name}: {panel['title']} queries {unknown}"


@pytest.mark.parametrize(("name", "board"), dashboards(), ids=lambda value: str(value)[:40])
def test_every_dashboard_has_a_stable_uid_and_a_title(name: str, board: dict[str, Any]) -> None:
    """Provisioned dashboards are matched by uid. A dashboard whose uid changes is a second
    dashboard, and the first one stays behind with everybody's links pointing at it."""
    assert board["uid"].startswith("mg-")
    assert board["title"]
    assert board["panels"]


# ---------------------------------------------------------------------------
# the chart, without helm
# ---------------------------------------------------------------------------


def values(path: Path) -> dict[str, Any]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def test_the_default_values_hold_the_grace_period_invariant() -> None:
    """The number task 18 calls out as easy to get wrong and expensive to discover: a
    60-second completion killed by a 30-second grace period is a dropped response, on every
    deploy, until it is right. The chart enforces it at render time; this asserts the
    default it ships with already satisfies it."""
    defaults = values(CHART / "values.yaml")

    needed = (
        defaults["api"]["drainSeconds"] + defaults["config"]["upstream"]["routingDeadlineSeconds"]
    )
    assert defaults["api"]["terminationGracePeriodSeconds"] >= needed


@pytest.mark.parametrize("example", sorted(EXAMPLES.glob("*.yaml")), ids=lambda p: p.name)
def test_every_example_values_file_holds_it_too(example: Path) -> None:
    defaults = values(CHART / "values.yaml")
    override = values(example)

    drain = override.get("api", {}).get("drainSeconds", defaults["api"]["drainSeconds"])
    deadline = (
        override.get("config", {})
        .get("upstream", {})
        .get("routingDeadlineSeconds", defaults["config"]["upstream"]["routingDeadlineSeconds"])
    )
    grace = override.get("api", {}).get(
        "terminationGracePeriodSeconds", defaults["api"]["terminationGracePeriodSeconds"]
    )
    assert grace >= drain + deadline


def test_the_values_file_carries_no_secrets() -> None:
    """Chart values are stored in the release, printed by `helm get values`, and committed
    to whatever repository holds the environment's overrides. None of those is a place for
    a master key — so the chart takes a Secret's *name* and never its contents."""
    text = (CHART / "values.yaml").read_text(encoding="utf-8")

    for forbidden in ("databaseUrl", "encryptionMasterKey", "jwtSigningKey", "apiKey"):
        assert forbidden not in text
    assert "existingSecret" in text


def test_the_pods_run_unprivileged_on_a_read_only_filesystem() -> None:
    defaults = values(CHART / "values.yaml")

    assert defaults["podSecurityContext"]["runAsNonRoot"] is True
    assert defaults["podSecurityContext"]["runAsUser"] == 10001
    assert defaults["securityContext"]["readOnlyRootFilesystem"] is True
    assert defaults["securityContext"]["allowPrivilegeEscalation"] is False
    assert defaults["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_the_uid_in_the_chart_matches_the_one_in_the_image() -> None:
    """A `runAsUser` that does not exist in the image is a pod that cannot read its own
    virtualenv, and the failure is a permission error nobody expects from a chart value."""
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    uid = values(CHART / "values.yaml")["podSecurityContext"]["runAsUser"]

    assert f"--uid {uid}" in dockerfile
    assert f"USER {uid}" in dockerfile


def test_the_image_ships_no_url_fetching_tools() -> None:
    """The image's whole threat model is about fetching URLs somebody else chose. Shipping
    curl in it gives anything that gets execution a fetcher for free."""
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    commands = [line for line in dockerfile.splitlines() if line.strip().upper().startswith("RUN ")]

    assert not any("apt-get install" in line for line in commands)
    assert not any("curl" in line or "wget" in line for line in commands)


def test_production_defaults_block_private_upstream_addresses() -> None:
    defaults = values(CHART / "values.yaml")

    assert defaults["config"]["upstream"]["privateAddresses"] == "block"


def test_the_api_and_the_workers_are_separate_deployments() -> None:
    """They scale on completely different signals — request rate against queue depth — and
    a shared replica count means one of them is always wrong."""
    defaults = values(CHART / "values.yaml")

    assert "replicaCount" in defaults["api"]
    assert "replicaCount" in defaults["worker"]
    assert defaults["api"]["autoscaling"] != defaults["worker"]["autoscaling"]
    assert defaults["worker"]["autoscaling"]["metricName"] == "jobs_queue_depth"


def test_the_migration_job_is_a_pre_upgrade_hook() -> None:
    job = (CHART / "templates/migration-job.yaml").read_text(encoding="utf-8")

    assert "helm.sh/hook: pre-install,pre-upgrade" in job
    # Kept on failure, or the only account of what went wrong goes with it.
    assert "before-hook-creation" in job
    # The variable is never *set* on an application pod. The deployment mentions it in a
    # comment saying so, which is why this looks for the assignment rather than the name.
    assert "RUN_MIGRATIONS:" not in (CHART / "templates/api-deployment.yaml").read_text(
        encoding="utf-8"
    )
    assert "RUN_MIGRATIONS:" not in (CHART / "templates/worker-deployment.yaml").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# the chart, with helm
# ---------------------------------------------------------------------------

helm_required = pytest.mark.skipif(
    shutil.which("helm") is None, reason="helm is not installed; CI renders the chart"
)

MINIMUM = [
    "--set",
    "config.publicBaseUrl=https://gw.example.com",
    "--set",
    "config.qdrantUrl=http://qdrant:6333",
    "--set",
    "config.s3Endpoint=http://minio:9000",
    "--set",
    "config.s3Bucket=bucket",
    "--set",
    "secrets.existingSecret=memory-gateway",
]


def helm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["helm", *args], capture_output=True, text=True, check=False, timeout=120)


@helm_required
def test_the_chart_lints() -> None:
    result = helm("lint", str(CHART), *MINIMUM)

    assert result.returncode == 0, result.stdout + result.stderr


@helm_required
@pytest.mark.parametrize("example", sorted(EXAMPLES.glob("*.yaml")), ids=lambda p: p.name)
def test_every_example_renders(example: Path) -> None:
    result = helm("template", "release", str(CHART), "-f", str(example))

    assert result.returncode == 0, result.stderr
    documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    kinds = {doc["kind"] for doc in documents}
    assert {"Deployment", "Service", "ConfigMap"} <= kinds


@helm_required
def test_a_default_backend_with_no_address_refuses_to_render() -> None:
    """Task 19's guard, and the reason it is a render failure rather than a comment.

    A default naming a backend the pods cannot reach is a deployment where every newly
    created organization silently has no index — retrieval returns nothing, ``fail_open``
    hides it, and the first report is a customer saying the answers got worse.
    """
    result = helm(
        "template",
        "release",
        str(CHART),
        *MINIMUM,
        "--set",
        "config.defaultVectorBackend=chroma",
    )

    assert result.returncode != 0
    assert "chromaUrl" in result.stderr


@helm_required
def test_configuring_chroma_puts_all_three_of_its_settings_in_the_configmap() -> None:
    """All three or none. A URL without its tenant and database would connect to whatever
    the client's defaults happen to be, which is a different Chroma than the operator
    configured and holds none of this deployment's collections."""
    result = helm(
        "template",
        "release",
        str(CHART),
        *MINIMUM,
        "--set",
        "config.chromaUrl=http://chroma:8000",
    )

    assert result.returncode == 0, result.stderr
    [configmap] = [
        doc for doc in yaml.safe_load_all(result.stdout) if doc and doc["kind"] == "ConfigMap"
    ]
    assert {"CHROMA_URL", "CHROMA_TENANT", "CHROMA_DATABASE"} <= set(configmap["data"])


@helm_required
def test_a_chart_with_no_chroma_url_sets_none_of_its_variables() -> None:
    """A Qdrant-only deployment must not carry an empty ``CHROMA_URL``: the application
    treats an empty string as unset, but a variable that is present and empty is one
    somebody later fills in without noticing the extra is not installed."""
    result = helm("template", "release", str(CHART), *MINIMUM)

    assert result.returncode == 0, result.stderr
    rendered = result.stdout
    assert "CHROMA_URL" not in rendered


@helm_required
def test_a_short_grace_period_refuses_to_render() -> None:
    """The invariant, enforced where getting it wrong is a failed `helm template` rather
    than a support ticket two weeks later."""
    result = helm(
        "template",
        "release",
        str(CHART),
        *MINIMUM,
        "--set",
        "api.terminationGracePeriodSeconds=30",
    )

    assert result.returncode != 0
    assert "terminationGracePeriodSeconds" in result.stderr


@helm_required
def test_the_local_embedder_cannot_be_deployed_to_production() -> None:
    result = helm(
        "template", "release", str(CHART), *MINIMUM, "--set", "config.embedding.provider=hash"
    )

    assert result.returncode != 0
    assert "hash" in result.stderr


@helm_required
def test_the_ssrf_guard_cannot_be_switched_off_in_production() -> None:
    result = helm(
        "template",
        "release",
        str(CHART),
        *MINIMUM,
        "--set",
        "config.upstream.privateAddresses=allow",
    )

    assert result.returncode != 0
    assert "SSRF" in result.stderr


@helm_required
def test_no_secret_value_appears_in_a_rendered_manifest() -> None:
    """Task 18's acceptance criterion: no secret in the image, the chart values, the logs
    or an environment dump. The rendered manifest is where a chart usually leaks one."""
    result = helm("template", "release", str(CHART), "-f", str(EXAMPLES / "production.yaml"))

    assert result.returncode == 0, result.stderr
    for document in yaml.safe_load_all(result.stdout):
        if document and document.get("kind") == "Secret":
            pytest.fail("the chart should never render a Secret; it references one")
    assert "ENCRYPTION_MASTER_KEY" not in result.stdout

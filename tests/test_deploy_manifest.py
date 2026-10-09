from pathlib import Path

import yaml


def _documents() -> list[dict]:
    manifest = Path("deploy/k8s/guardrail.yaml").read_text(encoding="utf-8")
    return list(yaml.safe_load_all(manifest))


def test_shop_deployment_persists_orders_and_runs_restricted():
    docs = _documents()
    volumes = {
        doc["metadata"]["name"]: doc
        for doc in docs
        if doc["kind"] == "PersistentVolumeClaim"
    }
    assert "shop-data" in volumes

    deployment = next(
        doc for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "shop"
    )
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == "agent-guardrail:1.2.0"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["SHOP_DB_PATH"] == "/data/shop.db"
    mounted = {item["name"] for item in container["volumeMounts"]}
    assert {"data", "tmp"} <= mounted


def test_gateway_uses_documented_database_env_and_tls_ingress():
    docs = _documents()
    deployment = next(
        doc for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "gateway"
    )
    env = {
        item["name"]
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert "GATEWAY_DB_PATH" in env
    assert "GUARDRAIL_DB_PATH" not in env

    ingress = next(doc for doc in docs if doc["kind"] == "Ingress")
    assert ingress["spec"]["tls"]

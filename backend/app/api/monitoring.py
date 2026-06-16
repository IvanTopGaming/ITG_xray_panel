import logging

import requests
from flask import Blueprint, jsonify, request

from ..services.metrics_client import default_client
from ..utils import token_required

logger = logging.getLogger("app.monitoring")

bp = Blueprint("monitoring", __name__)


def _container_names() -> dict:
    try:
        import docker

        c = docker.from_env()
        return {ctr.id: ctr.name for ctr in c.containers.list()}
    except Exception:
        logger.debug("metrics: container name lookup failed", exc_info=True)
        return {}


def _enrich(series: list, names: dict) -> list:
    for s in series:
        if s.get("scope") == "container":
            eid = s.get("entity", "")
            s["name"] = next(
                (n for cid, n in names.items() if cid.startswith(eid) or eid.startswith(cid)),
                eid,
            )
    return series


@bp.get("/monitoring/snapshot")
@token_required
def snapshot():
    try:
        data = default_client().get("/api/v1/snapshot")
    except requests.RequestException:
        return jsonify({"error": "metrics agent unreachable"}), 502
    if isinstance(data.get("series"), list):
        data["series"] = _enrich(data["series"], _container_names())
    return jsonify(data)


@bp.get("/monitoring/series")
@token_required
def series():
    try:
        data = default_client().get("/api/v1/series", params=request.args.to_dict())
    except requests.RequestException:
        return jsonify({"error": "metrics agent unreachable"}), 502
    return jsonify(data)


@bp.get("/monitoring/series/raw")
@token_required
def series_raw():
    try:
        data = default_client().get("/api/v1/series/raw", params=request.args.to_dict())
    except requests.RequestException:
        return jsonify({"error": "metrics agent unreachable"}), 502
    return jsonify(data)

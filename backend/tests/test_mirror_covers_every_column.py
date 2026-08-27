from panel_core.models import Balancer, Client, Inbound, NotificationLog, Outbound, ProvisionReceipt, RoutingProfile
from panel_core.services.state_export import MIRROR_EXCLUDED_COLUMNS


def _exported_keys(app, db):
    from panel_core.services.state_export import export_cold_state, export_hot_state

    hot, cold = export_hot_state(), export_cold_state()
    inbound = (hot["inbounds"] or [{}])[0]
    return {
        "Inbound": set(inbound) - {"clients"},
        "Client": set((inbound.get("clients") or [{}])[0]),
        "Outbound": set((cold["outbounds"] or [{}])[0]),
        "RoutingProfile": set((cold["routing_profiles"] or [{}])[0]),
        "Balancer": set((cold["balancers"] or [{}])[0]),
        "ProvisionReceipt": set((cold["receipts"] or [{}])[0]),
        "NotificationLog": set((cold["notification_logs"] or [{}])[0]),
    }


def test_every_model_column_is_either_mirrored_or_listed_as_excluded(app, db, rich_node):
    exported = _exported_keys(app, db)

    models = {
        "Inbound": Inbound,
        "Client": Client,
        "Outbound": Outbound,
        "RoutingProfile": RoutingProfile,
        "Balancer": Balancer,
        "ProvisionReceipt": ProvisionReceipt,
        "NotificationLog": NotificationLog,
    }

    for name, model in models.items():
        columns = {c.name for c in model.__table__.columns}
        missing = columns - exported[name] - MIRROR_EXCLUDED_COLUMNS[name]
        assert not missing, (
            f"{name}: колонки {sorted(missing)} есть в модели, но не едут в зеркало и не записаны "
            f"в исключения. Либо довези их в state_export.py и state_apply.py, либо впиши сюда с "
            f"причиной. Без этого сторожа зеркало гарантированно отстанет от моделей — вопрос времени"
        )

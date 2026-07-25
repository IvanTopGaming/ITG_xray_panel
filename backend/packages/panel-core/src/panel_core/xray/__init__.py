from panel_core.xray.gateway import get_xray_gateway


def generate_config_file(validate: bool = True):
    return get_xray_gateway().apply_config(validate=validate)


def restart_xray_container():
    return get_xray_gateway().restart()


def stream_xray_logs(tail_lines):
    return get_xray_gateway().stream_logs(tail_lines)


def update_geo_db():
    return get_xray_gateway().update_geo()


def _api_add_user_grpc(inbound_tag, client_obj):
    return get_xray_gateway().add_user(inbound_tag, client_obj)


def _api_remove_user_grpc(inbound_tag, email):
    return get_xray_gateway().remove_user(inbound_tag, email)

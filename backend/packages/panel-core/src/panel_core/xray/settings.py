import logging
from panel_core.models import SystemSetting
from panel_core.xray.protocol import (
    DEFAULT_GEOIP_URL,
    DEFAULT_GEOSITE_URL,
    DEFAULT_LOG_LEVEL,
    normalize_geo_data_url,
    normalize_xray_log_level,
)

logger = logging.getLogger(__name__)


def _get_system_setting_value(key, default_value):
    try:
        item = SystemSetting.query.filter_by(key=key).first()
        if item and str(item.value or "").strip():
            return str(item.value).strip()
    except Exception:
        logger.debug("Failed to read system setting '%s', using default", key)
    return default_value


def get_system_settings():
    log_level_raw = _get_system_setting_value("xray_log_level", DEFAULT_LOG_LEVEL)
    geoip_url_raw = _get_system_setting_value("geoip_url", DEFAULT_GEOIP_URL)
    geosite_url_raw = _get_system_setting_value("geosite_url", DEFAULT_GEOSITE_URL)

    try:
        xray_log_level = normalize_xray_log_level(log_level_raw)
    except ValueError:
        xray_log_level = DEFAULT_LOG_LEVEL

    try:
        geoip_url = normalize_geo_data_url(geoip_url_raw, "GeoIP URL")
    except ValueError:
        geoip_url = normalize_geo_data_url(DEFAULT_GEOIP_URL, "GeoIP URL")

    try:
        geosite_url = normalize_geo_data_url(geosite_url_raw, "GeoSite URL")
    except ValueError:
        geosite_url = normalize_geo_data_url(DEFAULT_GEOSITE_URL, "GeoSite URL")

    return {
        "xrayLogLevel": xray_log_level,
        "geoipUrl": geoip_url,
        "geositeUrl": geosite_url,
    }
